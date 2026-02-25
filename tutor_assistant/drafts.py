from __future__ import annotations

import logging
import time
from functools import lru_cache
from pathlib import Path

TRANSCRIPTION_FAILED_TEXT = "(transcription failed)"


@lru_cache(maxsize=4)
def _get_whisper_model(model_name: str, cache_root: str):
    from faster_whisper import WhisperModel

    return WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        download_root=cache_root,
    )


def transcribe_audio(
    audio_path: Path,
    model_name: str,
    cache_dir: Path,
    logger: logging.Logger | None = None,
) -> str:
    log = logger or logging.getLogger(__name__)

    if not audio_path.exists() or not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    effective_model = (model_name or "base").strip() or "base"
    cache_root = cache_dir.expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    log.info("transcribing... lesson_audio=%s model=%s", audio_path, effective_model)
    started_at = time.monotonic()

    model = _get_whisper_model(effective_model, str(cache_root))
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=1,
        vad_filter=True,
    )

    chunks: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if text:
            chunks.append(text)

    transcript = " ".join(chunks).strip()
    elapsed = time.monotonic() - started_at

    log.info(
        "done transcribing. duration=%.2fs language=%s chars=%s",
        elapsed,
        getattr(info, "language", "unknown"),
        len(transcript),
    )

    return transcript or TRANSCRIPTION_FAILED_TEXT


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _as_text_lines(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            text = _as_text(item)
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines).strip()
    return ""


def generate_draft(transcript: str, provider: str) -> dict[str, str]:
    provider = (provider or "none").lower()
    transcript = transcript.strip() or TRANSCRIPTION_FAILED_TEXT
    transcript_preview = transcript[:500]

    if provider == "none":
        return {
            "summary": (
                "Кратко: разобрали материал по теме урока.\n"
                f"Источник: {transcript[:120]}"
            ),
            "difficulties": "Пока заглушка: подключите LLM для реальных трудностей.",
            "homework": "Пока заглушка: повторить тему урока и решить 3 задачи по аналогии.",
        }

    if provider == "openai":
        try:
            from .llm_client import generate_lesson_draft

            raw = generate_lesson_draft(transcript)
            summary = _as_text(raw.get("summary")) if isinstance(raw, dict) else ""
            difficulties = _as_text_lines(raw.get("difficulties")) if isinstance(raw, dict) else ""
            homework = _as_text_lines(raw.get("homework")) if isinstance(raw, dict) else ""

            if summary:
                return {
                    "summary": summary,
                    "difficulties": difficulties or "Не выделены отдельные трудности.",
                    "homework": homework or "Домашка не указана моделью.",
                }
        except Exception:
            pass

        # Fallback если LLM недоступен/не настроен.
        return {
            "summary": (
                "Авто-summary недоступен, использую fallback.\n"
                f"Транскрипт (первые 500 символов): {transcript_preview}"
            ),
            "difficulties": "Не удалось получить сложности автоматически.",
            "homework": "Повторить материал урока и прислать 3 примера по теме.",
        }

    # Unknown provider fallback.
    return {
        "summary": f"Провайдер {provider} не поддерживается, fallback summary.\n{transcript_preview}",
        "difficulties": "Провайдер LLM не поддерживается.",
        "homework": "Повторить материал урока.",
    }
