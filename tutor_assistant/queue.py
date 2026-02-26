from __future__ import annotations

import json
import time
from typing import Any

from redis import Redis

from .config import Settings
from .time_utils import utcnow

LESSON_QUEUE_NAME = "lesson_tasks"
LESSON_PROCESSING_QUEUE_NAME = "lesson_tasks:processing"
LESSON_DEAD_LETTER_QUEUE_NAME = "lesson_tasks:dead"
LESSON_LOCK_PREFIX = "lesson_task:lock:"
TASK_PROCESS_AUDIO = "process_audio_lesson"
TASK_GENERATE_ARTIFACTS = "generate_artifacts"
TASK_TRANSCRIBE_JOB = "transcribe_job"
WORKER_METRIC_TASKS_PROCESSED_KEY = "lesson_metrics:tasks_processed_total"
WORKER_METRIC_TASKS_FAILED_KEY = "lesson_metrics:task_failures_total"
WORKER_FAILURE_EVENTS_ZSET_KEY = "lesson_metrics:worker_failures"
WORKER_METRIC_QUEUE_LATENCY_LAST_MS_KEY = "lesson_metrics:queue_latency_ms_last"
WORKER_METRIC_QUEUE_LATENCY_MAX_MS_KEY = "lesson_metrics:queue_latency_ms_max"
WORKER_METRIC_QUEUE_LATENCY_SUM_MS_KEY = "lesson_metrics:queue_latency_ms_sum"
WORKER_METRIC_QUEUE_LATENCY_SAMPLES_KEY = "lesson_metrics:queue_latency_ms_samples"
WORKER_METRIC_PROCESSING_DURATION_LAST_MS_KEY = "lesson_metrics:processing_duration_ms_last"
WORKER_METRIC_PROCESSING_DURATION_MAX_MS_KEY = "lesson_metrics:processing_duration_ms_max"
WORKER_METRIC_PROCESSING_DURATION_SUM_MS_KEY = "lesson_metrics:processing_duration_ms_sum"
WORKER_METRIC_PROCESSING_DURATION_SAMPLES_KEY = "lesson_metrics:processing_duration_ms_samples"
WORKER_METRIC_HEARTBEAT_TS_KEY = "lesson_metrics:worker_heartbeat_ts"
WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY = "lesson_metrics:dead_letter_requeued_total"


def get_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def now_epoch_ms() -> int:
    return int(time.time() * 1000)


def build_task_payload(lesson_id: str, task_type: str, enqueued_at: int | None = None) -> str:
    payload = {
        "task_type": str(task_type or TASK_PROCESS_AUDIO),
        "lesson_id": str(lesson_id or "").strip(),
        "enqueued_at": int(enqueued_at or now_epoch_ms()),
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def enqueue_process_lesson(
    redis_client: Redis,
    lesson_id: str,
    task_type: str = TASK_PROCESS_AUDIO,
) -> None:
    payload = build_task_payload(lesson_id=lesson_id, task_type=task_type)
    redis_client.lpush(LESSON_QUEUE_NAME, payload)


def parse_task_payload(raw_task: str) -> tuple[str, str, int | None]:
    task = str(raw_task).strip()
    if not task:
        return "", "", None

    try:
        data = json.loads(task)
    except json.JSONDecodeError:
        return TASK_PROCESS_AUDIO, task, None

    if not isinstance(data, dict):
        return TASK_PROCESS_AUDIO, task, None

    lesson_id = str(data.get("lesson_id", "")).strip()
    task_type = str(data.get("task_type", "")).strip() or TASK_PROCESS_AUDIO
    enqueued_at_raw = data.get("enqueued_at")
    enqueued_at: int | None = None
    if isinstance(enqueued_at_raw, (int, float)):
        enqueued_at = int(enqueued_at_raw)
    elif isinstance(enqueued_at_raw, str):
        raw_value = enqueued_at_raw.strip()
        if raw_value:
            try:
                enqueued_at = int(float(raw_value))
            except ValueError:
                enqueued_at = None

    if enqueued_at is not None and enqueued_at <= 0:
        enqueued_at = None

    return task_type, lesson_id, enqueued_at


def parse_task(raw_task: str) -> tuple[str, str]:
    task_type, lesson_id, _enqueued_at = parse_task_payload(raw_task)
    return task_type, lesson_id


def reserve_task(redis_client: Redis, timeout_seconds: int = 5) -> str | None:
    return redis_client.brpoplpush(
        LESSON_QUEUE_NAME,
        LESSON_PROCESSING_QUEUE_NAME,
        timeout=timeout_seconds,
    )


def ack_task(redis_client: Redis, raw_task: str) -> None:
    redis_client.lrem(LESSON_PROCESSING_QUEUE_NAME, 1, raw_task)


def requeue_task(redis_client: Redis, raw_task: str) -> None:
    task_type, lesson_id, _enqueued_at = parse_task_payload(raw_task)
    refreshed_task = raw_task
    if lesson_id:
        refreshed_task = build_task_payload(lesson_id=lesson_id, task_type=task_type)

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.lrem(LESSON_PROCESSING_QUEUE_NAME, 1, raw_task)
    pipeline.rpush(LESSON_QUEUE_NAME, refreshed_task)
    pipeline.execute()


def dead_letter_task(
    redis_client: Redis,
    raw_task: str,
    reason: str,
    task_type: str = "",
    lesson_id: str = "",
) -> None:
    payload: dict[str, Any] = {
        "raw_task": str(raw_task),
        "reason": str(reason),
        "task_type": str(task_type or ""),
        "lesson_id": str(lesson_id or ""),
        "failed_at": utcnow().isoformat(),
    }

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.lrem(LESSON_PROCESSING_QUEUE_NAME, 1, raw_task)
    pipeline.lpush(
        LESSON_DEAD_LETTER_QUEUE_NAME,
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
    )
    pipeline.execute()


def restore_inflight_tasks(redis_client: Redis) -> int:
    restored = 0
    while True:
        moved = redis_client.rpoplpush(LESSON_PROCESSING_QUEUE_NAME, LESSON_QUEUE_NAME)
        if moved is None:
            break
        restored += 1
    return restored


def acquire_lesson_lock(redis_client: Redis, lesson_id: str, ttl_seconds: int = 900) -> bool:
    key = f"{LESSON_LOCK_PREFIX}{lesson_id}"
    return bool(redis_client.set(key, "1", nx=True, ex=ttl_seconds))


def release_lesson_lock(redis_client: Redis, lesson_id: str) -> None:
    key = f"{LESSON_LOCK_PREFIX}{lesson_id}"
    redis_client.delete(key)
