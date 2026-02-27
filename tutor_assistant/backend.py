from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
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
    TASK_GENERATE_ARTIFACTS,
    TASK_PROCESS_AUDIO,
    TASK_TRANSCRIBE_JOB,
    WORKER_FAILURE_EVENTS_ZSET_KEY,
    WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY,
    WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY,
    WORKER_METRIC_HEARTBEAT_TS_KEY,
    WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY,
    WORKER_METRIC_DEAD_LETTER_REQUEUED_EVENTS_ZSET_KEY,
    WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY,
    WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY,
    WORKER_METRIC_TASKS_FAILED_KEY,
    WORKER_METRIC_TASKS_PROCESSED_KEY,
    build_task_payload,
    enqueue_process_lesson,
    get_redis_client,
    parse_task_payload,
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
        "dead_letter_requeued_total",
        "tutor_assistant_worker_dead_letter_requeued_total",
        "counter",
        "Total number of dead-letter tasks requeued back to lesson_tasks.",
    ),
    (
        "dead_letter_requeued_last_10m",
        "tutor_assistant_worker_dead_letter_requeued_last_10m",
        "gauge",
        "Number of dead-letter requeue operations in the last 10 minutes.",
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
        "transcribe_queue_depth",
        "tutor_assistant_worker_transcribe_queue_depth",
        "gauge",
        "Current transcribe_job queue depth.",
    ),
    (
        "transcribe_processing_depth",
        "tutor_assistant_worker_transcribe_processing_depth",
        "gauge",
        "Current in-flight transcribe_job processing depth.",
    ),
    (
        "transcribe_oldest_queue_age_seconds",
        "tutor_assistant_worker_transcribe_oldest_queue_age_seconds",
        "gauge",
        "Age in seconds of the oldest enqueued transcribe_job task.",
    ),
    (
        "transcribe_oldest_processing_age_seconds",
        "tutor_assistant_worker_transcribe_oldest_processing_age_seconds",
        "gauge",
        "Age in seconds of the oldest in-flight transcribe_job task.",
    ),
    (
        "transcribe_dead_letter_depth",
        "tutor_assistant_worker_transcribe_dead_letter_depth",
        "gauge",
        "Current dead-letter depth for transcribe_job tasks.",
    ),
    (
        "transcribe_oldest_dead_letter_age_seconds",
        "tutor_assistant_worker_transcribe_oldest_dead_letter_age_seconds",
        "gauge",
        "Age in seconds of the oldest dead-letter transcribe_job task.",
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
    (
        "worker_heartbeat_ts",
        "tutor_assistant_worker_heartbeat_timestamp_seconds",
        "gauge",
        "Unix timestamp of the latest worker heartbeat.",
    ),
    (
        "worker_heartbeat_age_seconds",
        "tutor_assistant_worker_heartbeat_age_seconds",
        "gauge",
        "Age of latest worker heartbeat in seconds.",
    ),
)
WORKER_KNOWN_TASK_TYPES: tuple[str, ...] = (
    TASK_PROCESS_AUDIO,
    TASK_GENERATE_ARTIFACTS,
    TASK_TRANSCRIBE_JOB,
)
WorkerMetricByType = dict[str, int | float]
WorkerMetricValue = int | float | WorkerMetricByType
WorkerMetricsPayload = dict[str, WorkerMetricValue]


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


def collect_task_type_counters(redis_client, base_key: str) -> dict[str, int]:
    return {
        task_type: int(redis_client.get(f"{base_key}:{task_type}") or 0)
        for task_type in WORKER_KNOWN_TASK_TYPES
    }


def collect_task_type_timing_metrics(
    redis_client,
    *,
    last_key: str,
    max_key: str,
    sum_key: str,
    samples_key: str,
) -> tuple[dict[str, int], dict[str, int], dict[str, float]]:
    last_values: dict[str, int] = {}
    max_values: dict[str, int] = {}
    avg_values: dict[str, float] = {}

    for task_type in WORKER_KNOWN_TASK_TYPES:
        task_last = int(redis_client.get(f"{last_key}:{task_type}") or 0)
        task_max = int(redis_client.get(f"{max_key}:{task_type}") or 0)
        task_sum = int(redis_client.get(f"{sum_key}:{task_type}") or 0)
        task_samples = int(redis_client.get(f"{samples_key}:{task_type}") or 0)

        last_values[task_type] = task_last
        max_values[task_type] = task_max
        avg_values[task_type] = round(task_sum / task_samples, 2) if task_samples else 0.0

    return last_values, max_values, avg_values


def collect_queue_task_stats(
    redis_client,
    queue_name: str,
) -> tuple[dict[str, int], dict[str, int]]:
    counts = {task_type: 0 for task_type in WORKER_KNOWN_TASK_TYPES}
    oldest_enqueued_ms = {task_type: 0 for task_type in WORKER_KNOWN_TASK_TYPES}
    raw_tasks = redis_client.lrange(queue_name, 0, -1) or []

    for raw_task in raw_tasks:
        task_type, _lesson_id, enqueued_at = parse_task_payload(str(raw_task))
        if task_type in counts:
            counts[task_type] += 1
            if enqueued_at is not None and enqueued_at > 0:
                current_oldest = oldest_enqueued_ms.get(task_type, 0)
                if current_oldest <= 0 or enqueued_at < current_oldest:
                    oldest_enqueued_ms[task_type] = int(enqueued_at)

    return counts, oldest_enqueued_ms


def parse_dead_letter_failed_at_epoch(failed_at_raw: str) -> int:
    normalized = str(failed_at_raw or "").strip()
    if not normalized:
        return 0

    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return 0

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def parse_dead_letter_item(raw_item: str) -> dict[str, str]:
    payload = {
        "raw_item": str(raw_item),
        "raw_task": "",
        "task_type": "",
        "lesson_id": "",
        "reason": "",
        "failed_at": "",
    }

    try:
        parsed_item = json.loads(str(raw_item))
    except json.JSONDecodeError:
        return payload

    if not isinstance(parsed_item, dict):
        return payload

    payload["raw_task"] = str(parsed_item.get("raw_task", "")).strip()
    payload["task_type"] = str(parsed_item.get("task_type", "")).strip()
    payload["lesson_id"] = str(parsed_item.get("lesson_id", "")).strip()
    payload["reason"] = str(parsed_item.get("reason", "")).strip()
    payload["failed_at"] = str(parsed_item.get("failed_at", "")).strip()

    if payload["raw_task"] and (not payload["task_type"] or not payload["lesson_id"]):
        task_type, lesson_id, _enqueued_at = parse_task_payload(payload["raw_task"])
        if not payload["task_type"]:
            payload["task_type"] = task_type
        if not payload["lesson_id"]:
            payload["lesson_id"] = lesson_id

    return payload


def collect_dead_letter_task_stats(redis_client) -> tuple[dict[str, int], dict[str, int]]:
    counts = {task_type: 0 for task_type in WORKER_KNOWN_TASK_TYPES}
    oldest_failed_at_ts = {task_type: 0 for task_type in WORKER_KNOWN_TASK_TYPES}
    raw_items = redis_client.lrange(LESSON_DEAD_LETTER_QUEUE_NAME, 0, -1) or []

    for raw_item in raw_items:
        parsed_item = parse_dead_letter_item(str(raw_item))
        task_type = parsed_item.get("task_type", "")
        failed_at_epoch = parse_dead_letter_failed_at_epoch(parsed_item.get("failed_at", ""))

        if task_type not in counts:
            continue

        counts[task_type] += 1
        if failed_at_epoch > 0:
            current_oldest = oldest_failed_at_ts.get(task_type, 0)
            if current_oldest <= 0 or failed_at_epoch < current_oldest:
                oldest_failed_at_ts[task_type] = failed_at_epoch

    return counts, oldest_failed_at_ts


def list_dead_letter_items(
    redis_client,
    *,
    limit: int,
    task_type: str | None = None,
    lesson_id: str | None = None,
) -> list[dict[str, str]]:
    requested_task_type = str(task_type or "").strip()
    requested_lesson_id = str(lesson_id or "").strip()
    raw_items = redis_client.lrange(LESSON_DEAD_LETTER_QUEUE_NAME, 0, -1) or []

    items: list[dict[str, str]] = []
    for raw_item in raw_items:
        parsed = parse_dead_letter_item(str(raw_item))
        current_task_type = parsed.get("task_type", "")
        current_lesson_id = parsed.get("lesson_id", "")
        raw_task = parsed.get("raw_task", "")

        if not raw_task:
            continue

        if requested_task_type and current_task_type != requested_task_type:
            continue
        if requested_lesson_id and current_lesson_id != requested_lesson_id:
            continue

        items.append(parsed)
        if len(items) >= limit:
            break

    return items


def requeue_dead_letter_items(
    redis_client,
    *,
    limit: int,
    task_type: str | None = None,
    lesson_id: str | None = None,
) -> tuple[int, list[dict[str, str]]]:
    requested_task_type = str(task_type or "").strip()
    requested_lesson_id = str(lesson_id or "").strip()
    raw_items = redis_client.lrange(LESSON_DEAD_LETTER_QUEUE_NAME, 0, -1) or []

    selected: list[dict[str, str]] = []
    for raw_item in reversed(raw_items):
        parsed = parse_dead_letter_item(str(raw_item))
        current_task_type = parsed.get("task_type", "")
        current_lesson_id = parsed.get("lesson_id", "")
        raw_task = parsed.get("raw_task", "")
        if not raw_task:
            continue

        if requested_task_type and current_task_type != requested_task_type:
            continue
        if requested_lesson_id and current_lesson_id != requested_lesson_id:
            continue

        selected.append(parsed)
        if len(selected) >= limit:
            break

    moved = 0
    moved_items: list[dict[str, str]] = []
    for item in selected:
        removed = int(redis_client.lrem(LESSON_DEAD_LETTER_QUEUE_NAME, -1, item["raw_item"]) or 0)
        if removed <= 0:
            continue

        queued_raw_task = str(item.get("raw_task", "")).strip()
        queue_task_type, queue_lesson_id, _queue_enqueued_at = parse_task_payload(queued_raw_task)
        if queue_lesson_id:
            queued_raw_task = build_task_payload(
                lesson_id=queue_lesson_id,
                task_type=queue_task_type,
            )

        redis_client.rpush(LESSON_QUEUE_NAME, queued_raw_task)
        redis_client.incr(WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY)
        if queue_task_type in WORKER_KNOWN_TASK_TYPES:
            redis_client.incr(f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{queue_task_type}")
        now_ts = int(time.time())
        event_member = f"{time.time_ns()}:{queue_task_type}:{queue_lesson_id or '-'}"
        redis_client.zadd(WORKER_METRIC_DEAD_LETTER_REQUEUED_EVENTS_ZSET_KEY, {event_member: now_ts})
        redis_client.zremrangebyscore(
            WORKER_METRIC_DEAD_LETTER_REQUEUED_EVENTS_ZSET_KEY,
            0,
            now_ts - 86400,
        )
        moved += 1
        moved_items.append(item)

    return moved, moved_items


def collect_worker_metrics(redis_client) -> WorkerMetricsPayload:
    now_ts = int(time.time())
    ten_minutes_ago = now_ts - 600

    queue_latency_sum = int(redis_client.get(WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY) or 0)
    queue_latency_samples = int(redis_client.get(WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY) or 0)
    processing_duration_sum = int(redis_client.get(WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY) or 0)
    processing_duration_samples = int(redis_client.get(WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY) or 0)
    heartbeat_ts = int(redis_client.get(WORKER_METRIC_HEARTBEAT_TS_KEY) or 0)
    heartbeat_age_seconds = max(0, now_ts - heartbeat_ts) if heartbeat_ts > 0 else -1
    tasks_processed_by_type = collect_task_type_counters(
        redis_client,
        WORKER_METRIC_TASKS_PROCESSED_KEY,
    )
    task_failures_by_type = collect_task_type_counters(
        redis_client,
        WORKER_METRIC_TASKS_FAILED_KEY,
    )
    (
        queue_latency_last_by_type,
        queue_latency_max_by_type,
        queue_latency_avg_by_type,
    ) = collect_task_type_timing_metrics(
        redis_client,
        last_key=WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY,
        max_key=WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY,
        sum_key=WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY,
        samples_key=WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY,
    )
    (
        processing_duration_last_by_type,
        processing_duration_max_by_type,
        processing_duration_avg_by_type,
    ) = collect_task_type_timing_metrics(
        redis_client,
        last_key=WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY,
        max_key=WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY,
        sum_key=WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY,
        samples_key=WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY,
    )
    queue_depth_by_type, queue_oldest_enqueued_ms_by_type = collect_queue_task_stats(
        redis_client,
        LESSON_QUEUE_NAME,
    )
    processing_depth_by_type, processing_oldest_enqueued_ms_by_type = collect_queue_task_stats(
        redis_client,
        LESSON_PROCESSING_QUEUE_NAME,
    )
    dead_letter_depth_by_type, dead_letter_oldest_failed_at_ts_by_type = collect_dead_letter_task_stats(
        redis_client
    )
    transcribe_oldest_queue_enqueued_ms = int(
        queue_oldest_enqueued_ms_by_type.get(TASK_TRANSCRIBE_JOB, 0)
    )
    transcribe_oldest_queue_age_seconds = (
        max(0, int((now_ts * 1000 - transcribe_oldest_queue_enqueued_ms) / 1000))
        if transcribe_oldest_queue_enqueued_ms > 0
        else -1
    )
    transcribe_oldest_processing_enqueued_ms = int(
        processing_oldest_enqueued_ms_by_type.get(TASK_TRANSCRIBE_JOB, 0)
    )
    transcribe_oldest_processing_age_seconds = (
        max(0, int((now_ts * 1000 - transcribe_oldest_processing_enqueued_ms) / 1000))
        if transcribe_oldest_processing_enqueued_ms > 0
        else -1
    )
    transcribe_oldest_dead_letter_failed_at_ts = int(
        dead_letter_oldest_failed_at_ts_by_type.get(TASK_TRANSCRIBE_JOB, 0)
    )
    transcribe_oldest_dead_letter_age_seconds = (
        max(0, now_ts - transcribe_oldest_dead_letter_failed_at_ts)
        if transcribe_oldest_dead_letter_failed_at_ts > 0
        else -1
    )

    return {
        "tasks_processed_total": int(redis_client.get(WORKER_METRIC_TASKS_PROCESSED_KEY) or 0),
        "task_failures_total": int(redis_client.get(WORKER_METRIC_TASKS_FAILED_KEY) or 0),
        "dead_letter_requeued_total": int(
            redis_client.get(WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY) or 0
        ),
        "dead_letter_requeued_last_10m": int(
            redis_client.zcount(
                WORKER_METRIC_DEAD_LETTER_REQUEUED_EVENTS_ZSET_KEY,
                ten_minutes_ago,
                now_ts,
            )
        ),
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
        "worker_heartbeat_ts": heartbeat_ts,
        "worker_heartbeat_age_seconds": heartbeat_age_seconds,
        "transcribe_queue_depth": int(queue_depth_by_type.get(TASK_TRANSCRIBE_JOB, 0)),
        "transcribe_processing_depth": int(processing_depth_by_type.get(TASK_TRANSCRIBE_JOB, 0)),
        "transcribe_oldest_queue_age_seconds": transcribe_oldest_queue_age_seconds,
        "transcribe_oldest_processing_age_seconds": transcribe_oldest_processing_age_seconds,
        "transcribe_dead_letter_depth": int(dead_letter_depth_by_type.get(TASK_TRANSCRIBE_JOB, 0)),
        "transcribe_oldest_dead_letter_age_seconds": transcribe_oldest_dead_letter_age_seconds,
        "tasks_processed_by_type": tasks_processed_by_type,
        "task_failures_by_type": task_failures_by_type,
        "dead_letter_requeued_by_type": collect_task_type_counters(
            redis_client,
            WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY,
        ),
        "queue_depth_by_type": queue_depth_by_type,
        "processing_depth_by_type": processing_depth_by_type,
        "dead_letter_depth_by_type": dead_letter_depth_by_type,
        "queue_latency_ms_last_by_type": queue_latency_last_by_type,
        "queue_latency_ms_max_by_type": queue_latency_max_by_type,
        "queue_latency_ms_avg_by_type": queue_latency_avg_by_type,
        "processing_duration_ms_last_by_type": processing_duration_last_by_type,
        "processing_duration_ms_max_by_type": processing_duration_max_by_type,
        "processing_duration_ms_avg_by_type": processing_duration_avg_by_type,
    }


def get_metrics_by_task_type(metrics: WorkerMetricsPayload, field_name: str) -> WorkerMetricByType:
    raw_value = metrics.get(field_name, {})
    if not isinstance(raw_value, dict):
        return {}

    payload: WorkerMetricByType = {}
    for task_type in WORKER_KNOWN_TASK_TYPES:
        task_value = raw_value.get(task_type, 0)
        if isinstance(task_value, bool):
            payload[task_type] = 1 if task_value else 0
        elif isinstance(task_value, (int, float)):
            payload[task_type] = task_value
        else:
            payload[task_type] = 0
    return payload


def append_prometheus_metric_by_task_type(
    lines: list[str],
    *,
    metric_name: str,
    metric_type: str,
    metric_help: str,
    values: WorkerMetricByType,
) -> None:
    lines.append(f"# HELP {metric_name} {metric_help}")
    lines.append(f"# TYPE {metric_name} {metric_type}")
    for task_type in WORKER_KNOWN_TASK_TYPES:
        lines.append(f'{metric_name}{{task_type="{task_type}"}} {values.get(task_type, 0)}')


def render_worker_metrics_prometheus(metrics: WorkerMetricsPayload) -> str:
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

    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_tasks_processed_by_type_total",
        metric_type="counter",
        metric_help="Total number of processed worker tasks by task type.",
        values=get_metrics_by_task_type(metrics, "tasks_processed_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_task_failures_by_type_total",
        metric_type="counter",
        metric_help="Total number of failed worker tasks by task type.",
        values=get_metrics_by_task_type(metrics, "task_failures_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_dead_letter_requeued_by_type_total",
        metric_type="counter",
        metric_help="Total number of dead-letter tasks requeued by task type.",
        values=get_metrics_by_task_type(metrics, "dead_letter_requeued_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_queue_depth_by_type",
        metric_type="gauge",
        metric_help="Current queue depth by task type.",
        values=get_metrics_by_task_type(metrics, "queue_depth_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_processing_depth_by_type",
        metric_type="gauge",
        metric_help="Current in-flight processing depth by task type.",
        values=get_metrics_by_task_type(metrics, "processing_depth_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_dead_letter_depth_by_type",
        metric_type="gauge",
        metric_help="Current dead-letter depth by task type.",
        values=get_metrics_by_task_type(metrics, "dead_letter_depth_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_queue_latency_ms_last_by_type",
        metric_type="gauge",
        metric_help="Last observed queue latency in milliseconds by task type.",
        values=get_metrics_by_task_type(metrics, "queue_latency_ms_last_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_queue_latency_ms_max_by_type",
        metric_type="gauge",
        metric_help="Maximum observed queue latency in milliseconds by task type.",
        values=get_metrics_by_task_type(metrics, "queue_latency_ms_max_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_queue_latency_ms_avg_by_type",
        metric_type="gauge",
        metric_help="Average observed queue latency in milliseconds by task type.",
        values=get_metrics_by_task_type(metrics, "queue_latency_ms_avg_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_processing_duration_ms_last_by_type",
        metric_type="gauge",
        metric_help="Last observed processing duration in milliseconds by task type.",
        values=get_metrics_by_task_type(metrics, "processing_duration_ms_last_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_processing_duration_ms_max_by_type",
        metric_type="gauge",
        metric_help="Maximum observed processing duration in milliseconds by task type.",
        values=get_metrics_by_task_type(metrics, "processing_duration_ms_max_by_type"),
    )
    append_prometheus_metric_by_task_type(
        lines,
        metric_name="tutor_assistant_worker_processing_duration_ms_avg_by_type",
        metric_type="gauge",
        metric_help="Average observed processing duration in milliseconds by task type.",
        values=get_metrics_by_task_type(metrics, "processing_duration_ms_avg_by_type"),
    )

    lines.append("")
    return "\n".join(lines)


def evaluate_worker_alerts(metrics: WorkerMetricsPayload) -> list[str]:
    alerts: list[str] = []
    error_threshold = int(settings.worker_alert_errors_last_10m_threshold)
    dead_letter_threshold = int(settings.worker_alert_dead_letter_threshold)
    queue_depth_threshold = int(settings.worker_alert_queue_depth_threshold)
    transcribe_queue_depth_threshold = int(settings.worker_alert_transcribe_queue_depth_threshold)
    transcribe_oldest_queue_age_seconds_threshold = int(
        settings.worker_alert_transcribe_oldest_queue_age_seconds_threshold
    )
    transcribe_oldest_processing_age_seconds_threshold = int(
        settings.worker_alert_transcribe_oldest_processing_age_seconds_threshold
    )
    transcribe_dead_letter_threshold = int(settings.worker_alert_transcribe_dead_letter_threshold)
    transcribe_oldest_dead_letter_age_seconds_threshold = int(
        settings.worker_alert_transcribe_oldest_dead_letter_age_seconds_threshold
    )
    dead_letter_requeued_last_10m_threshold = int(
        settings.worker_alert_dead_letter_requeued_last_10m_threshold
    )
    heartbeat_age_threshold = int(settings.worker_alert_heartbeat_age_seconds_threshold)

    errors_last_10m = int(metrics.get("worker_errors_last_10m", 0))
    dead_letter_requeued_last_10m = int(metrics.get("dead_letter_requeued_last_10m", 0))
    dead_letter_depth = int(metrics.get("dead_letter_depth", 0))
    queue_depth = int(metrics.get("queue_depth", 0))
    transcribe_queue_depth = int(metrics.get("transcribe_queue_depth", 0))
    transcribe_oldest_queue_age_seconds = int(metrics.get("transcribe_oldest_queue_age_seconds", -1))
    transcribe_oldest_processing_age_seconds = int(
        metrics.get("transcribe_oldest_processing_age_seconds", -1)
    )
    transcribe_dead_letter_depth = int(metrics.get("transcribe_dead_letter_depth", 0))
    transcribe_oldest_dead_letter_age_seconds = int(
        metrics.get("transcribe_oldest_dead_letter_age_seconds", -1)
    )
    heartbeat_age_seconds = int(metrics.get("worker_heartbeat_age_seconds", -1))

    if errors_last_10m > error_threshold:
        alerts.append(
            "worker_errors_last_10m exceeded threshold: "
            f"{errors_last_10m} > {error_threshold}"
        )
    if dead_letter_requeued_last_10m > dead_letter_requeued_last_10m_threshold:
        alerts.append(
            "dead_letter_requeued_last_10m exceeded threshold: "
            f"{dead_letter_requeued_last_10m} > {dead_letter_requeued_last_10m_threshold}"
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
    if transcribe_queue_depth > transcribe_queue_depth_threshold:
        alerts.append(
            "transcribe_queue_depth exceeded threshold: "
            f"{transcribe_queue_depth} > {transcribe_queue_depth_threshold}"
        )
    if (
        transcribe_oldest_queue_age_seconds >= 0
        and transcribe_oldest_queue_age_seconds > transcribe_oldest_queue_age_seconds_threshold
    ):
        alerts.append(
            "transcribe_oldest_queue_age_seconds exceeded threshold: "
            f"{transcribe_oldest_queue_age_seconds} > "
            f"{transcribe_oldest_queue_age_seconds_threshold}"
        )
    if (
        transcribe_oldest_processing_age_seconds >= 0
        and transcribe_oldest_processing_age_seconds
        > transcribe_oldest_processing_age_seconds_threshold
    ):
        alerts.append(
            "transcribe_oldest_processing_age_seconds exceeded threshold: "
            f"{transcribe_oldest_processing_age_seconds} > "
            f"{transcribe_oldest_processing_age_seconds_threshold}"
        )
    if transcribe_dead_letter_depth > transcribe_dead_letter_threshold:
        alerts.append(
            "transcribe_dead_letter_depth exceeded threshold: "
            f"{transcribe_dead_letter_depth} > {transcribe_dead_letter_threshold}"
        )
    if (
        transcribe_oldest_dead_letter_age_seconds >= 0
        and transcribe_oldest_dead_letter_age_seconds
        > transcribe_oldest_dead_letter_age_seconds_threshold
    ):
        alerts.append(
            "transcribe_oldest_dead_letter_age_seconds exceeded threshold: "
            f"{transcribe_oldest_dead_letter_age_seconds} > "
            f"{transcribe_oldest_dead_letter_age_seconds_threshold}"
        )
    if heartbeat_age_seconds < 0:
        alerts.append("worker heartbeat missing")
    elif heartbeat_age_seconds > heartbeat_age_threshold:
        alerts.append(
            "worker_heartbeat_age_seconds exceeded threshold: "
            f"{heartbeat_age_seconds} > {heartbeat_age_threshold}"
        )

    return alerts


def load_worker_metrics_or_503() -> WorkerMetricsPayload:
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


def require_ops_token(
    x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
) -> None:
    expected_token = str(settings.ops_api_token or "").strip()
    if not expected_token:
        return

    if str(x_ops_token or "").strip() != expected_token:
        raise HTTPException(status_code=401, detail="Invalid ops token")


def validate_task_type_filter(task_type: str | None) -> str | None:
    normalized = str(task_type or "").strip()
    if not normalized:
        return None

    if normalized not in WORKER_KNOWN_TASK_TYPES:
        allowed_values = ", ".join(WORKER_KNOWN_TASK_TYPES)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid task_type. Allowed values: {allowed_values}",
        )

    return normalized


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
            "dead_letter_requeued_last_10m": int(
                settings.worker_alert_dead_letter_requeued_last_10m_threshold
            ),
            "dead_letter_depth": int(settings.worker_alert_dead_letter_threshold),
            "queue_depth": int(settings.worker_alert_queue_depth_threshold),
            "transcribe_queue_depth": int(settings.worker_alert_transcribe_queue_depth_threshold),
            "transcribe_oldest_queue_age_seconds": int(
                settings.worker_alert_transcribe_oldest_queue_age_seconds_threshold
            ),
            "transcribe_oldest_processing_age_seconds": int(
                settings.worker_alert_transcribe_oldest_processing_age_seconds_threshold
            ),
            "transcribe_dead_letter_depth": int(settings.worker_alert_transcribe_dead_letter_threshold),
            "transcribe_oldest_dead_letter_age_seconds": int(
                settings.worker_alert_transcribe_oldest_dead_letter_age_seconds_threshold
            ),
            "worker_heartbeat_age_seconds": int(
                settings.worker_alert_heartbeat_age_seconds_threshold
            ),
        },
        "metrics": metrics,
    }


@app.get("/ops/worker/dead-letter")
def worker_dead_letter_list(
    limit: int = Query(20, ge=1, le=200),
    task_type: str | None = Query(None),
    lesson_id: str | None = Query(None),
    _ops_guard: None = Depends(require_ops_token),
) -> dict:
    validated_task_type = validate_task_type_filter(task_type)

    redis_client = None
    try:
        redis_client = get_redis_client(settings)
        items = list_dead_letter_items(
            redis_client,
            limit=limit,
            task_type=validated_task_type,
            lesson_id=lesson_id,
        )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Failed to read dead-letter queue: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Failed to read dead-letter queue: {exc}") from exc
    finally:
        if redis_client is not None:
            redis_client.close()

    return {
        "count": len(items),
        "items": [
            {
                "task_type": item.get("task_type", ""),
                "lesson_id": item.get("lesson_id", ""),
                "reason": item.get("reason", ""),
                "failed_at": item.get("failed_at", ""),
            }
            for item in items
        ],
    }


@app.post("/ops/worker/dead-letter/requeue")
def worker_dead_letter_requeue(
    limit: int = Query(20, ge=1, le=200),
    task_type: str | None = Query(None),
    lesson_id: str | None = Query(None),
    _ops_guard: None = Depends(require_ops_token),
) -> dict:
    validated_task_type = validate_task_type_filter(task_type)

    redis_client = None
    try:
        redis_client = get_redis_client(settings)
        moved, moved_items = requeue_dead_letter_items(
            redis_client,
            limit=limit,
            task_type=validated_task_type,
            lesson_id=lesson_id,
        )
    except RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Failed to requeue dead-letter tasks: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Failed to requeue dead-letter tasks: {exc}") from exc
    finally:
        if redis_client is not None:
            redis_client.close()

    return {
        "status": "ok",
        "moved": moved,
        "items": [
            {
                "task_type": item.get("task_type", ""),
                "lesson_id": item.get("lesson_id", ""),
                "reason": item.get("reason", ""),
                "failed_at": item.get("failed_at", ""),
            }
            for item in moved_items
        ],
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

    if job.status not in {"failed", "queued", "canceled"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry in status={job.status}",
        )

    if job.status in {"failed", "canceled"}:
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


@app.post("/api/transcribe/jobs/{job_id}/cancel")
def cancel_transcription_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job_uuid = parse_job_id(job_id)
    job = db.query(TranscriptionJob).filter(TranscriptionJob.id == job_uuid).first()
    if not job:
        raise HTTPException(status_code=404, detail="Transcription job not found")

    if job.status == "canceled":
        payload = serialize_transcription_job(job)
        payload["canceled"] = False
        return payload

    if job.status in {"done", "failed"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel in status={job.status}",
        )

    job.status = "canceled"
    job.processing_error = "Canceled by user"
    job.processed_at = utcnow()
    db.commit()
    db.refresh(job)

    payload = serialize_transcription_job(job)
    payload["canceled"] = True
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
