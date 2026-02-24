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
    llm_provider: str
    llm_api_key: str
    whisper_model: str
    database_url: str
    redis_url: str


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

    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        storage_path=storage_path,
        llm_provider=os.getenv("LLM_PROVIDER", "none").strip().lower() or "none",
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        whisper_model=os.getenv("WHISPER_MODEL", "base").strip() or "base",
        database_url=database_url,
        redis_url=redis_url,
    )
