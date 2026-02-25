from __future__ import annotations

import logging
import time
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db, init_db
from .models import Artifact, Lesson, LessonChunk, TranscriptionJob
from .queue import (
    LESSON_DEAD_LETTER_QUEUE_NAME,
    LESSON_PROCESSING_QUEUE_NAME,
    LESSON_QUEUE_NAME,
    TASK_TRANSCRIBE_JOB,
    WORKER_FAILURE_EVENTS_ZSET_KEY,
    WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY,
    WORKER_METRIC_TASKS_FAILED_KEY,
    WORKER_METRIC_TASKS_PROCESSED_KEY,
    enqueue_process_lesson,
    get_redis_client,
)
from .storage import write_chunk, write_transcription_source
from .time_utils import utcnow

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(title="Tutor Assistant MVP")

TRANSCRIPTION_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
TRANSCRIPTION_RATE_LIMIT_PER_MINUTE = 6
TRANSCRIPTION_RATE_LIMIT_WINDOW_SECONDS = 60
TRANSCRIPTION_ALLOWED_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
    ".webm",
}
TRANSCRIPTION_ALLOWED_CONTENT_TYPES = {
    "audio/aac",
    "audio/flac",
    "audio/m4a",
    "audio/mp3",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/x-wav",
    "video/webm",
}

WORKER_PROMETHEUS_METRICS: tuple[tuple[str, str, str, str], ...] = (
    (
        "tasks_processed_total",
        "tutor_assistant_worker_tasks_processed_total",
        "counter",
        "Total number of processed worker tasks.",
    ),
    (
        "task_failures_total",
        "tutor_assistant_worker_task_failures_total",
        "counter",
        "Total number of failed worker tasks.",
    ),
    (
        "worker_errors_last_10m",
        "tutor_assistant_worker_errors_last_10m",
        "gauge",
        "Number of worker failures in the last 10 minutes.",
    ),
    (
        "queue_depth",
        "tutor_assistant_worker_queue_depth",
        "gauge",
        "Current task queue depth.",
    ),
    (
        "processing_depth",
        "tutor_assistant_worker_processing_depth",
        "gauge",
        "Current in-flight processing queue depth.",
    ),
    (
        "dead_letter_depth",
        "tutor_assistant_worker_dead_letter_depth",
        "gauge",
        "Current dead-letter queue depth.",
    ),
    (
        "queue_latency_ms_last",
        "tutor_assistant_worker_queue_latency_ms_last",
        "gauge",
        "Last observed queue latency in milliseconds.",
    ),
    (
        "queue_latency_ms_max",
        "tutor_assistant_worker_queue_latency_ms_max",
        "gauge",
        "Maximum observed queue latency in milliseconds.",
    ),
    (
        "queue_latency_ms_avg",
        "tutor_assistant_worker_queue_latency_ms_avg",
        "gauge",
        "Average observed queue latency in milliseconds.",
    ),
    (
        "processing_duration_ms_last",
        "tutor_assistant_worker_processing_duration_ms_last",
        "gauge",
        "Last observed processing duration in milliseconds.",
    ),
    (
        "processing_duration_ms_max",
        "tutor_assistant_worker_processing_duration_ms_max",
        "gauge",
        "Maximum observed processing duration in milliseconds.",
    ),
    (
        "processing_duration_ms_avg",
        "tutor_assistant_worker_processing_duration_ms_avg",
        "gauge",
        "Average observed processing duration in milliseconds.",
    ),
)


def _extract_webhook_meta(update: object) -> tuple[object, str, object]:
    if not isinstance(update, dict):
        return None, "unknown", None

    update_id = update.get("update_id")
    event_type = "unknown"
    from_user_id = None

    for key in (
        "message",
        "edited_message",
        "callback_query",
        "my_chat_member",
        "chat_member",
        "channel_post",
    ):
        if key not in update:
            continue

        event_type = key
        payload = update.get(key)
        if isinstance(payload, dict):
            actor = payload.get("from")
            if isinstance(actor, dict):
                from_user_id = actor.get("id")
        break

    return update_id, event_type, from_user_id


@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    update_id, event_type, from_user_id = _extract_webhook_meta(update)
    logger.info(
        "TG webhook update_id=%s event=%s from_user_id=%s",
        update_id,
        event_type,
        from_user_id,
    )

    try:
        from tutor_assistant.bot import process_update
        await process_update(update)
    except Exception:  # noqa: BLE001
        logger.exception("webhook error")

    return {"ok": True}
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    logger.info("DB initialized")


def get_lesson_by_token(db: Session, lesson_id: str, token: str) -> Lesson | None:
    return (
        db.query(Lesson)
        .filter(Lesson.id == lesson_id, Lesson.token == token)
        .first()
    )


def count_lesson_chunks(db: Session, lesson_id: str) -> int:
    return (
        db.query(LessonChunk)
        .filter(
            LessonChunk.lesson_id == lesson_id,
            LessonChunk.path.isnot(None),
        )
        .count()
    )


def enqueue_lesson_job(lesson_id: str) -> None:
    redis_client = get_redis_client(settings)
    try:
        enqueue_process_lesson(redis_client, lesson_id)
    finally:
        redis_client.close()


def enqueue_transcription_job(job_id: str) -> None:
    redis_client = get_redis_client(settings)
    try:
        enqueue_process_lesson(redis_client, lesson_id=job_id, task_type=TASK_TRANSCRIBE_JOB)
    finally:
        redis_client.close()


def parse_job_id(job_id: str) -> UUID:
    try:
        return UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="job_id must be UUID") from exc


def serialize_transcription_job(job: TranscriptionJob, include_text: bool = True) -> dict:
    payload = {
        "job_id": str(job.id),
        "status": job.status,
        "processing_attempts": int(job.processing_attempts or 0),
        "processing_error": job.processing_error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "processed_at": job.processed_at.isoformat() if job.processed_at else None,
        "has_transcript": bool((job.transcript_text or "").strip() or job.transcript_path),
    }
    if include_text:
        payload["transcript_text"] = job.transcript_text
    return payload


def get_request_client_id(request: Request) -> str:
    x_forwarded_for = (request.headers.get("x-forwarded-for") or "").strip()
    if x_forwarded_for:
        first_ip = x_forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


def enforce_transcription_rate_limit(request: Request) -> None:
    redis_client = None
    try:
        redis_client = get_redis_client(settings)
        client_id = get_request_client_id(request)
        now_ts = int(time.time())
        window_start = now_ts - (now_ts % TRANSCRIPTION_RATE_LIMIT_WINDOW_SECONDS)
        redis_key = f"transcription_rate:{client_id}:{window_start}"

        pipeline = redis_client.pipeline(transaction=True)
        pipeline.incr(redis_key)
        pipeline.expire(redis_key, TRANSCRIPTION_RATE_LIMIT_WINDOW_SECONDS + 5)
        values = pipeline.execute()

        current_count = int(values[0]) if values else 0
        if current_count <= TRANSCRIPTION_RATE_LIMIT_PER_MINUTE:
            return

        ttl = int(redis_client.ttl(redis_key) or TRANSCRIPTION_RATE_LIMIT_WINDOW_SECONDS)
        raise HTTPException(
            status_code=429,
            detail=(
                "Rate limit exceeded for transcription uploads. "
                f"Try again in {ttl} seconds."
            ),
        )
    except HTTPException:
        raise
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rate-limit check failed")
        raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc
    finally:
        if redis_client is not None:
            redis_client.close()


def format_megabytes(size_bytes: int) -> str:
    mb_value = size_bytes / (1024 * 1024)
    if mb_value.is_integer():
        return str(int(mb_value))
    return f"{mb_value:.1f}"


def validate_transcription_upload_meta(audio: UploadFile) -> str:
    filename = (audio.filename or "").strip()
    suffix = Path(filename).suffix.lower()
    content_type = (audio.content_type or "").split(";", 1)[0].strip().lower()

    if suffix and suffix not in TRANSCRIPTION_ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(TRANSCRIPTION_ALLOWED_EXTENSIONS))
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension: {suffix}. Allowed: {allowed}",
        )

    if content_type and not (
        content_type in TRANSCRIPTION_ALLOWED_CONTENT_TYPES
        or content_type.startswith("audio/")
    ):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content_type: {content_type}",
        )

    if suffix:
        return suffix
    if content_type == "video/webm":
        return ".webm"
    return ".wav"


async def read_upload_limited(audio: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total_size = 0

    while True:
        chunk = await audio.read(1024 * 1024)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_bytes:
            max_mb = format_megabytes(max_bytes)
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max size: {max_mb} MB",
            )
        chunks.append(chunk)

    payload = b"".join(chunks)
    if not payload:
        raise HTTPException(status_code=400, detail="Empty audio file")
    return payload


def collect_worker_metrics(redis_client) -> dict[str, int | float]:
    now_ts = int(time.time())
    ten_minutes_ago = now_ts - 600

    queue_latency_sum = int(redis_client.get(WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY) or 0)
    queue_latency_samples = int(redis_client.get(WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY) or 0)
    processing_duration_sum = int(redis_client.get(WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY) or 0)
    processing_duration_samples = int(redis_client.get(WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY) or 0)

    return {
        "tasks_processed_total": int(redis_client.get(WORKER_METRIC_TASKS_PROCESSED_KEY) or 0),
        "task_failures_total": int(redis_client.get(WORKER_METRIC_TASKS_FAILED_KEY) or 0),
        "worker_errors_last_10m": int(
            redis_client.zcount(WORKER_FAILURE_EVENTS_ZSET_KEY, ten_minutes_ago, now_ts)
        ),
        "queue_depth": int(redis_client.llen(LESSON_QUEUE_NAME)),
        "processing_depth": int(redis_client.llen(LESSON_PROCESSING_QUEUE_NAME)),
        "dead_letter_depth": int(redis_client.llen(LESSON_DEAD_LETTER_QUEUE_NAME)),
        "queue_latency_ms_last": int(redis_client.get(WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY) or 0),
        "queue_latency_ms_max": int(redis_client.get(WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY) or 0),
        "queue_latency_ms_avg": (
            round(queue_latency_sum / queue_latency_samples, 2) if queue_latency_samples else 0.0
        ),
        "processing_duration_ms_last": int(
            redis_client.get(WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY) or 0
        ),
        "processing_duration_ms_max": int(
            redis_client.get(WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY) or 0
        ),
        "processing_duration_ms_avg": (
            round(processing_duration_sum / processing_duration_samples, 2)
            if processing_duration_samples
            else 0.0
        ),
    }


def render_worker_metrics_prometheus(metrics: dict[str, int | float]) -> str:
    lines: list[str] = []

    for field_name, metric_name, metric_type, metric_help in WORKER_PROMETHEUS_METRICS:
        raw_value = metrics.get(field_name, 0)
        if isinstance(raw_value, bool):
            metric_value = 1 if raw_value else 0
        elif isinstance(raw_value, (int, float)):
            metric_value = raw_value
        else:
            metric_value = 0

        lines.append(f"# HELP {metric_name} {metric_help}")
        lines.append(f"# TYPE {metric_name} {metric_type}")
        lines.append(f"{metric_name} {metric_value}")

    lines.append("")
    return "\n".join(lines)


def evaluate_worker_alerts(metrics: dict[str, int | float]) -> list[str]:
    alerts: list[str] = []
    error_threshold = int(settings.worker_alert_errors_last_10m_threshold)
    dead_letter_threshold = int(settings.worker_alert_dead_letter_threshold)
    queue_depth_threshold = int(settings.worker_alert_queue_depth_threshold)

    errors_last_10m = int(metrics.get("worker_errors_last_10m", 0))
    dead_letter_depth = int(metrics.get("dead_letter_depth", 0))
    queue_depth = int(metrics.get("queue_depth", 0))

    if errors_last_10m > error_threshold:
        alerts.append(
            "worker_errors_last_10m exceeded threshold: "
            f"{errors_last_10m} > {error_threshold}"
        )
    if dead_letter_depth > dead_letter_threshold:
        alerts.append(
            "dead_letter_depth exceeded threshold: "
            f"{dead_letter_depth} > {dead_letter_threshold}"
        )
    if queue_depth > queue_depth_threshold:
        alerts.append(
            "queue_depth exceeded threshold: "
            f"{queue_depth} > {queue_depth_threshold}"
        )

    return alerts


def load_worker_metrics_or_503() -> dict[str, int | float]:
    redis_client = None
    try:
        redis_client = get_redis_client(settings)
        return collect_worker_metrics(redis_client)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Failed to read worker metrics: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Failed to read worker metrics: {exc}") from exc
    finally:
        if redis_client is not None:
            redis_client.close()


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    postgres_ok = False
    redis_ok = False
    details: dict[str, str] = {}

    try:
        db.execute(text("SELECT 1"))
        postgres_ok = True
    except Exception as exc:  # noqa: BLE001
        details["postgres"] = str(exc)

    redis_client = get_redis_client(settings)
    try:
        redis_ok = bool(redis_client.ping())
    except Exception as exc:  # noqa: BLE001
        details["redis"] = str(exc)
    finally:
        redis_client.close()

    status = "ok" if postgres_ok and redis_ok else "degraded"
    return {
        "status": status,
        "postgres": postgres_ok,
        "redis": redis_ok,
        "details": details,
    }


@app.get("/metrics/worker")
def worker_metrics() -> dict:
    return load_worker_metrics_or_503()


@app.get("/metrics/worker/prometheus", response_class=PlainTextResponse)
def worker_metrics_prometheus() -> PlainTextResponse:
    metrics = load_worker_metrics_or_503()
    payload = render_worker_metrics_prometheus(metrics)
    return PlainTextResponse(
        content=payload,
        media_type="text/plain; version=0.0.4",
    )


@app.get("/alerts/worker")
def worker_alerts() -> dict:
    metrics = load_worker_metrics_or_503()
    alerts = evaluate_worker_alerts(metrics)
    return {
        "status": "alert" if alerts else "ok",
        "alerts": alerts,
        "thresholds": {
            "worker_errors_last_10m": int(settings.worker_alert_errors_last_10m_threshold),
            "dead_letter_depth": int(settings.worker_alert_dead_letter_threshold),
            "queue_depth": int(settings.worker_alert_queue_depth_threshold),
        },
        "metrics": metrics,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return "<h3>Tutor Assistant backend is running</h3>"


@app.get("/transcribe", response_class=HTMLResponse)
def transcribe_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("transcribe.html", {"request": request})


@app.post("/api/transcribe/jobs", status_code=202)
async def create_transcription_job(
    request: Request,
    audio: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    enforce_transcription_rate_limit(request)
    suffix = validate_transcription_upload_meta(audio)
    payload = await read_upload_limited(audio, TRANSCRIPTION_MAX_UPLOAD_BYTES)

    job_uuid = uuid4()
    source_path = write_transcription_source(
        settings=settings,
        job_id=str(job_uuid),
        payload=payload,
        suffix=suffix or ".webm",
    )

    job = TranscriptionJob(
        id=job_uuid,
        source_path=str(source_path),
        status="queued",
        processing_attempts=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        enqueue_transcription_job(str(job.id))
    except RedisError as exc:
        job.status = "failed"
        job.processing_error = f"Failed to enqueue task: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.processing_error = f"Failed to enqueue task: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc

    return serialize_transcription_job(job)


@app.get("/api/transcribe/jobs")
def list_transcription_jobs(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    jobs = (
        db.query(TranscriptionJob)
        .order_by(TranscriptionJob.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "items": [serialize_transcription_job(job, include_text=False) for job in jobs],
        "count": len(jobs),
    }


@app.get("/api/transcribe/jobs/{job_id}")
def get_transcription_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job_uuid = parse_job_id(job_id)
    job = db.query(TranscriptionJob).filter(TranscriptionJob.id == job_uuid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")
    return serialize_transcription_job(job)


@app.post("/api/transcribe/jobs/{job_id}/retry")
def retry_transcription_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job_uuid = parse_job_id(job_id)
    job = db.query(TranscriptionJob).filter(TranscriptionJob.id == job_uuid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    if job.status == "done":
        payload = serialize_transcription_job(job)
        payload["queued"] = False
        return payload

    if job.status not in {"failed", "queued"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry in status={job.status}",
        )

    if job.status == "failed":
        job.status = "queued"
        job.processing_error = None
        job.processed_at = None
        db.commit()

    try:
        enqueue_transcription_job(str(job.id))
    except RedisError as exc:
        job.status = "failed"
        job.processing_error = f"Failed to enqueue task: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.processing_error = f"Failed to enqueue task: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc

    payload = serialize_transcription_job(job)
    payload["queued"] = True
    return payload


@app.get("/api/transcribe/jobs/{job_id}/transcript")
def download_transcription_transcript(
    job_id: str,
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    job_uuid = parse_job_id(job_id)
    job = db.query(TranscriptionJob).filter(TranscriptionJob.id == job_uuid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    transcript_text = (job.transcript_text or "").strip()
    if not transcript_text and job.transcript_path:
        transcript_path = Path(job.transcript_path)
        if transcript_path.exists() and transcript_path.is_file():
            transcript_text = transcript_path.read_text(encoding="utf-8")

    if not transcript_text:
        raise HTTPException(status_code=409, detail="Transcript is not ready yet")

    safe_job_id = str(job.id)
    return PlainTextResponse(
        content=transcript_text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="transcript-{safe_job_id}.txt"'},
    )


@app.get("/lesson/{lesson_id}", response_class=HTMLResponse)
def lesson_page(
    request: Request,
    lesson_id: str,
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    lesson = get_lesson_by_token(db, lesson_id, token)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found or token invalid")

    student_name = lesson.student.name if lesson.student else "ученик"
    return templates.TemplateResponse(
        "lesson.html",
        {
            "request": request,
            "lesson_id": lesson_id,
            "token": token,
            "student_name": student_name,
        },
    )


@app.post("/api/lessons/{lesson_id}/start")
def lesson_start(
    lesson_id: str,
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    lesson = get_lesson_by_token(db, lesson_id, token)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found or token invalid")

    if lesson.status == "finished":
        raise HTTPException(status_code=400, detail="Lesson already finished")

    if lesson.status == "created":
        lesson.status = "recording"
        lesson.started_at = lesson.started_at or utcnow()
        db.commit()

    return {"status": lesson.status}


@app.post("/api/lessons/{lesson_id}/chunks")
async def lesson_chunk(
    lesson_id: str,
    token: str = Query(...),
    seq: int = Form(...),
    chunk: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    if seq < 0:
        raise HTTPException(status_code=400, detail="seq must be >= 0")

    lesson = get_lesson_by_token(db, lesson_id, token)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found or token invalid")

    if lesson.status == "finished":
        raise HTTPException(status_code=409, detail="Lesson already finished")

    existing_chunk = (
        db.query(LessonChunk)
        .filter(LessonChunk.lesson_id == lesson.id, LessonChunk.seq == seq)
        .first()
    )
    if existing_chunk:
        return {"status": "duplicate", "seq": seq}

    payload = await chunk.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty chunk")

    path = write_chunk(settings, lesson_id=lesson.id, seq=seq, payload=payload)

    db.add(
        LessonChunk(
            lesson_id=lesson.id,
            seq=seq,
            path=str(path),
            size_bytes=len(payload),
        )
    )
    db.add(
        Artifact(
            lesson_id=lesson.id,
            kind="chunk",
            path=str(path),
        )
    )
    db.commit()

    return {"status": "ok", "seq": seq, "size": len(payload)}


@app.post("/api/lessons/{lesson_id}/finish")
async def lesson_finish(
    lesson_id: str,
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    lesson = get_lesson_by_token(db, lesson_id, token)
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found or token invalid")

    chunk_count = count_lesson_chunks(db, lesson.id)
    if chunk_count == 0:
        raise HTTPException(status_code=400, detail="No chunks uploaded")

    should_enqueue = False

    if lesson.status != "finished":
        lesson.status = "finished"
        lesson.finished_at = utcnow()
        lesson.processing_status = "queued"
        lesson.processing_error = None
        lesson.processed_at = None
        should_enqueue = True
    elif lesson.processing_status in {"failed", "pending"}:
        lesson.processing_status = "queued"
        lesson.processing_error = None
        should_enqueue = True

    db.commit()

    if should_enqueue:
        try:
            enqueue_lesson_job(lesson.id)
        except RedisError as exc:
            lesson.processing_status = "failed"
            lesson.processing_error = f"Failed to enqueue task: {exc}"
            db.commit()
            raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc
        except Exception as exc:  # noqa: BLE001
            lesson.processing_status = "failed"
            lesson.processing_error = f"Failed to enqueue task: {exc}"
            db.commit()
            raise HTTPException(status_code=503, detail="Queue unavailable, try again") from exc

    return {
        "status": "finished",
        "lesson_id": lesson.id,
        "processing_status": lesson.processing_status,
        "queued": should_enqueue,
    }
