from __future__ import annotations

from redis import Redis

from .config import Settings

LESSON_QUEUE_NAME = "lesson_tasks"
LESSON_PROCESSING_QUEUE_NAME = "lesson_tasks:processing"
LESSON_LOCK_PREFIX = "lesson_task:lock:"


def get_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def enqueue_process_lesson(redis_client: Redis, lesson_id: str) -> None:
    redis_client.lpush(LESSON_QUEUE_NAME, lesson_id)


def reserve_task(redis_client: Redis, timeout_seconds: int = 5) -> str | None:
    return redis_client.brpoplpush(
        LESSON_QUEUE_NAME,
        LESSON_PROCESSING_QUEUE_NAME,
        timeout=timeout_seconds,
    )


def ack_task(redis_client: Redis, raw_task: str) -> None:
    redis_client.lrem(LESSON_PROCESSING_QUEUE_NAME, 1, raw_task)


def requeue_task(redis_client: Redis, raw_task: str) -> None:
    pipeline = redis_client.pipeline(transaction=True)
    pipeline.lrem(LESSON_PROCESSING_QUEUE_NAME, 1, raw_task)
    pipeline.rpush(LESSON_QUEUE_NAME, raw_task)
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
