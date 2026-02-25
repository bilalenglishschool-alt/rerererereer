from __future__ import annotations

import logging
import time
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
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


def serialize_transcription_job(job: TranscriptionJob) -> dict:
    return {
        "job_id": str(job.id),
        "status": job.status,
        "processing_attempts": int(job.processing_attempts or 0),
        "processing_error": job.processing_error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "processed_at": job.processed_at.isoformat() if job.processed_at else None,
        "transcript_text": job.transcript_text,
    }


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
    redis_client = get_redis_client(settings)
    try:
        now_ts = int(time.time())
        ten_minutes_ago = now_ts - 600

        processed_total = int(redis_client.get(WORKER_METRIC_TASKS_PROCESSED_KEY) or 0)
        failed_total = int(redis_client.get(WORKER_METRIC_TASKS_FAILED_KEY) or 0)
        failures_last_10m = int(
            redis_client.zcount(WORKER_FAILURE_EVENTS_ZSET_KEY, ten_minutes_ago, now_ts)
        )

        return {
            "tasks_processed_total": processed_total,
            "task_failures_total": failed_total,
            "worker_errors_last_10m": failures_last_10m,
            "queue_depth": int(redis_client.llen(LESSON_QUEUE_NAME)),
            "processing_depth": int(redis_client.llen(LESSON_PROCESSING_QUEUE_NAME)),
            "dead_letter_depth": int(redis_client.llen(LESSON_DEAD_LETTER_QUEUE_NAME)),
        }
    finally:
        redis_client.close()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return "<h3>Tutor Assistant backend is running</h3>"


@app.get("/transcribe", response_class=HTMLResponse)
def transcribe_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("transcribe.html", {"request": request})


@app.post("/api/transcribe/jobs", status_code=202)
async def create_transcription_job(
    audio: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    payload = await audio.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty audio file")

    job_uuid = uuid4()
    suffix = Path(audio.filename or "").suffix.lower() if audio.filename else ".webm"
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
