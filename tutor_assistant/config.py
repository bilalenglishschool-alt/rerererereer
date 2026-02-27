from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    base_url: str
    storage_path: Path
    ops_api_token: str
    llm_provider: str
    llm_api_key: str
    whisper_model: str
    database_url: str
    redis_url: str
    transcription_retention_days: int
    transcription_cleanup_interval_seconds: int
    worker_alert_errors_last_10m_threshold: int
    worker_alert_dead_letter_threshold: int
    worker_alert_queue_depth_threshold: int
    worker_alert_transcribe_queue_depth_threshold: int
    worker_alert_transcribe_oldest_queue_age_seconds_threshold: int
    worker_alert_transcribe_oldest_processing_age_seconds_threshold: int
    worker_alert_transcribe_dead_letter_threshold: int
    worker_alert_transcribe_oldest_dead_letter_age_seconds_threshold: int
    worker_alert_dead_letter_requeued_last_10m_threshold: int
    worker_alert_heartbeat_age_seconds_threshold: int


def _parse_positive_int(raw_value: str, default: int, minimum: int) -> int:
    try:
        parsed = int((raw_value or "").strip())
    except (TypeError, ValueError):
        return default
    if parsed < minimum:
        return minimum
    return parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    storage_path = Path(os.getenv("STORAGE_PATH", "/data")).expanduser().resolve()
    storage_path.mkdir(parents=True, exist_ok=True)

    postgres_db = os.getenv("POSTGRES_DB", "tutor_assistant").strip()
    postgres_user = os.getenv("POSTGRES_USER", "tutor_assistant").strip()
    postgres_password = os.getenv("POSTGRES_PASSWORD", "tutor_assistant").strip()

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        database_url = (
            f"postgresql+psycopg://{postgres_user}:{postgres_password}@postgres:5432/{postgres_db}"
        )

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0").strip()
    transcription_retention_days = _parse_positive_int(
        os.getenv("TRANSCRIPTION_RETENTION_DAYS", "14"),
        default=14,
        minimum=1,
    )
    transcription_cleanup_interval_seconds = _parse_positive_int(
        os.getenv("TRANSCRIPTION_CLEANUP_INTERVAL_SECONDS", "600"),
        default=600,
        minimum=30,
    )
    worker_alert_errors_last_10m_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_ERRORS_LAST_10M_THRESHOLD", "0"),
        default=0,
        minimum=0,
    )
    worker_alert_dead_letter_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_DEAD_LETTER_THRESHOLD", "0"),
        default=0,
        minimum=0,
    )
    worker_alert_queue_depth_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_QUEUE_DEPTH_THRESHOLD", "20"),
        default=20,
        minimum=0,
    )
    worker_alert_transcribe_queue_depth_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_TRANSCRIBE_QUEUE_DEPTH_THRESHOLD", "10"),
        default=10,
        minimum=0,
    )
    worker_alert_transcribe_oldest_queue_age_seconds_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_TRANSCRIBE_OLDEST_QUEUE_AGE_SECONDS_THRESHOLD", "300"),
        default=300,
        minimum=0,
    )
    worker_alert_transcribe_oldest_processing_age_seconds_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_TRANSCRIBE_OLDEST_PROCESSING_AGE_SECONDS_THRESHOLD", "600"),
        default=600,
        minimum=0,
    )
    worker_alert_transcribe_dead_letter_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_TRANSCRIBE_DEAD_LETTER_THRESHOLD", "0"),
        default=0,
        minimum=0,
    )
    worker_alert_transcribe_oldest_dead_letter_age_seconds_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_TRANSCRIBE_OLDEST_DEAD_LETTER_AGE_SECONDS_THRESHOLD", "600"),
        default=600,
        minimum=0,
    )
    worker_alert_dead_letter_requeued_last_10m_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_DEAD_LETTER_REQUEUED_LAST_10M_THRESHOLD", "20"),
        default=20,
        minimum=0,
    )
    worker_alert_heartbeat_age_seconds_threshold = _parse_positive_int(
        os.getenv("WORKER_ALERT_HEARTBEAT_AGE_SECONDS_THRESHOLD", "120"),
        default=120,
        minimum=0,
    )

    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        storage_path=storage_path,
        ops_api_token=os.getenv("OPS_API_TOKEN", "").strip(),
        llm_provider=os.getenv("LLM_PROVIDER", "none").strip().lower() or "none",
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        whisper_model=os.getenv("WHISPER_MODEL", "base").strip() or "base",
        database_url=database_url,
        redis_url=redis_url,
        transcription_retention_days=transcription_retention_days,
        transcription_cleanup_interval_seconds=transcription_cleanup_interval_seconds,
        worker_alert_errors_last_10m_threshold=worker_alert_errors_last_10m_threshold,
        worker_alert_dead_letter_threshold=worker_alert_dead_letter_threshold,
        worker_alert_queue_depth_threshold=worker_alert_queue_depth_threshold,
        worker_alert_transcribe_queue_depth_threshold=worker_alert_transcribe_queue_depth_threshold,
        worker_alert_transcribe_oldest_queue_age_seconds_threshold=(
            worker_alert_transcribe_oldest_queue_age_seconds_threshold
        ),
        worker_alert_transcribe_oldest_processing_age_seconds_threshold=(
            worker_alert_transcribe_oldest_processing_age_seconds_threshold
        ),
        worker_alert_transcribe_dead_letter_threshold=worker_alert_transcribe_dead_letter_threshold,
        worker_alert_transcribe_oldest_dead_letter_age_seconds_threshold=(
            worker_alert_transcribe_oldest_dead_letter_age_seconds_threshold
        ),
        worker_alert_dead_letter_requeued_last_10m_threshold=(
            worker_alert_dead_letter_requeued_last_10m_threshold
        ),
        worker_alert_heartbeat_age_seconds_threshold=worker_alert_heartbeat_age_seconds_threshold,
    )
