from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .config import Settings


def lesson_base_dir(settings: Settings, lesson_id: str) -> Path:
    lesson_dir = settings.storage_path / "lessons" / lesson_id
    lesson_dir.mkdir(parents=True, exist_ok=True)
    return lesson_dir


def lesson_chunks_dir(settings: Settings, lesson_id: str) -> Path:
    chunks_dir = lesson_base_dir(settings, lesson_id) / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    return chunks_dir


def write_chunk(settings: Settings, lesson_id: str, seq: int, payload: bytes) -> Path:
    path = lesson_chunks_dir(settings, lesson_id) / f"{seq:08d}.webm"
    path.write_bytes(payload)
    return path


def merge_chunks(settings: Settings, lesson_id: str, chunk_paths: Iterable[Path]) -> Path:
    output_path = lesson_base_dir(settings, lesson_id) / "recording.webm"

    with output_path.open("wb") as out_file:
        for chunk_path in chunk_paths:
            if not chunk_path.exists():
                continue
            with chunk_path.open("rb") as chunk_file:
                out_file.write(chunk_file.read())

    return output_path


def write_transcript_file(settings: Settings, lesson_id: str, transcript: str) -> Path:
    transcript_path = lesson_base_dir(settings, lesson_id) / "transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    return transcript_path
