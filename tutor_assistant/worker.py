from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import update

from .config import get_settings
from .database import SessionLocal, init_db
from .drafts import TRANSCRIPTION_FAILED_TEXT, generate_draft, transcribe_audio
from .models import Artifact, Lesson, LessonChunk
from .queue import (
    TASK_GENERATE_ARTIFACTS,
    TASK_PROCESS_AUDIO,
    ack_task,
    acquire_lesson_lock,
    get_redis_client,
    parse_task,
    release_lesson_lock,
    requeue_task,
    reserve_task,
    restore_inflight_tasks,
)
from .storage import merge_chunks, write_transcript_file
from .telegram_api import send_draft_to_tutor

logger = logging.getLogger(__name__)
settings = get_settings()

MAX_ATTEMPTS = 3


def upsert_artifact(
    db,
    lesson_id: str,
    kind: str,
    path: str | None = None,
    content: str | None = None,
) -> None:
    artifact = (
        db.query(Artifact)
        .filter(Artifact.lesson_id == lesson_id, Artifact.kind == kind)
        .first()
    )
    if artifact:
        artifact.path = path
        artifact.content = content
        return

    db.add(
        Artifact(
            lesson_id=lesson_id,
            kind=kind,
            path=path,
            content=content,
        )
    )


def process_audio_lesson(lesson_id: str) -> None:
    with SessionLocal() as db:
        lesson = (
            db.query(Lesson)
            .filter(Lesson.id == lesson_id)
            .with_for_update()
            .first()
        )
        if not lesson:
            logger.warning("Lesson %s was not found", lesson_id)
            return

        if lesson.processing_status == "done":
            logger.info("Lesson %s already processed, skipping", lesson_id)
            return

        lesson.processing_status = "processing"
        lesson.processing_attempts = (lesson.processing_attempts or 0) + 1
        lesson.processing_error = None
        db.commit()

    with SessionLocal() as db:
        lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
        if not lesson:
            logger.warning("Lesson %s disappeared during processing", lesson_id)
            return

        chunks = (
            db.query(LessonChunk)
            .filter(
                LessonChunk.lesson_id == lesson.id,
                LessonChunk.path.isnot(None),
            )
            .order_by(LessonChunk.seq.asc())
            .all()
        )
        if not chunks:
            lesson.processing_status = "failed"
            lesson.processing_error = "No chunks uploaded"
            db.commit()
            raise RuntimeError(f"No chunks uploaded for lesson {lesson.id}")

        chunk_paths = [Path(chunk.path) for chunk in chunks]
        recording_path = merge_chunks(settings, lesson_id=lesson.id, chunk_paths=chunk_paths)

        transcript = TRANSCRIPTION_FAILED_TEXT
        transcript_error: str | None = None
        if not recording_path.exists():
            transcript_error = f"Audio file not found: {recording_path}"
            logger.error("Lesson %s transcription failed: %s", lesson.id, transcript_error)
        else:
            try:
                transcript = transcribe_audio(
                    audio_path=recording_path,
                    model_name=settings.whisper_model,
                    cache_dir=settings.storage_path / "whisper-cache",
                    logger=logger,
                )
            except Exception as exc:  # noqa: BLE001
                transcript_error = str(exc)
                transcript = TRANSCRIPTION_FAILED_TEXT
                logger.exception("Lesson %s transcription failed", lesson.id)

        draft = generate_draft(transcript, settings.llm_provider)

        transcript_path = write_transcript_file(settings, lesson_id=lesson.id, transcript=transcript)

        lesson.transcript_text = transcript
        lesson.draft_summary = draft["summary"]
        lesson.draft_difficulties = draft["difficulties"]
        lesson.draft_homework = draft["homework"]
        lesson.processed_at = datetime.utcnow()
        lesson.processing_status = "done"
        lesson.processing_error = (
            f"Transcription failed: {transcript_error}" if transcript_error else None
        )

        upsert_artifact(db, lesson.id, "recording", str(recording_path))
        upsert_artifact(db, lesson.id, "transcript", str(transcript_path))

        should_notify = False
        tutor_chat_id: int | None = None
        student_name = lesson.student.name if lesson.student else "ученик"

        if settings.bot_token and lesson.tutor:
            updated = db.execute(
                update(Lesson)
                .where(Lesson.id == lesson.id, Lesson.draft_sent_to_tutor.is_(False))
                .values(draft_sent_to_tutor=True)
            )
            should_notify = (updated.rowcount or 0) > 0
            if should_notify:
                tutor_chat_id = lesson.tutor.tg_user_id

        db.commit()

    if should_notify and tutor_chat_id is not None:
        try:
            asyncio.run(
                send_draft_to_tutor(
                    bot_token=settings.bot_token,
                    tutor_chat_id=tutor_chat_id,
                    lesson_id=lesson_id,
                    student_name=student_name,
                    summary=draft["summary"],
                    difficulties=draft["difficulties"],
                    homework=draft["homework"],
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to notify tutor for lesson %s", lesson_id)
            with SessionLocal() as db:
                lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
                if lesson:
                    lesson.processing_error = (
                        f"Draft generated, but tutor notify failed: {exc}"
                    )
                    db.commit()


def process_generate_artifacts(lesson_id: str) -> None:
    with SessionLocal() as db:
        lesson = (
            db.query(Lesson)
            .filter(Lesson.id == lesson_id)
            .with_for_update()
            .first()
        )
        if not lesson:
            logger.warning("Lesson %s was not found", lesson_id)
            return

        if lesson.status == "sent":
            logger.info("Lesson %s already sent, skipping", lesson_id)
            return

        if lesson.status == "draft_ready":
            logger.info("Lesson %s already has draft_ready status, skipping", lesson_id)
            return

        if lesson.status != "processing":
            raise RuntimeError(
                f"Lesson {lesson.id} has invalid status for generate_artifacts: {lesson.status}"
            )

        lesson.processing_status = "processing"
        lesson.processing_attempts = (lesson.processing_attempts or 0) + 1
        lesson.processing_error = None
        db.commit()

    with SessionLocal() as db:
        lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
        if not lesson:
            logger.warning("Lesson %s disappeared during generate_artifacts", lesson_id)
            return

        chunks = (
            db.query(LessonChunk)
            .filter(LessonChunk.lesson_id == lesson.id)
            .order_by(LessonChunk.seq.asc())
            .all()
        )
        chunk_texts = [
            (chunk.content or "").strip()
            for chunk in chunks
            if (chunk.content or "").strip()
        ]
        if not chunk_texts:
            lesson.processing_status = "failed"
            lesson.processing_error = "No text chunks uploaded"
            db.commit()
            raise RuntimeError(f"No text chunks uploaded for lesson {lesson.id}")

        transcript = "\n".join(chunk_texts)
        draft = generate_draft(transcript, settings.llm_provider)
        summary = (draft.get("summary") or "").strip()
        difficulties = (draft.get("difficulties") or "").strip()
        homework = (draft.get("homework") or "").strip()

        if not summary:
            summary = transcript

        lesson.transcript_text = transcript
        lesson.draft_summary = summary
        lesson.draft_difficulties = difficulties
        lesson.draft_homework = homework
        lesson.status = "draft_ready"
        lesson.processed_at = datetime.utcnow()
        lesson.processing_status = "done"
        lesson.processing_error = None

        upsert_artifact(db, lesson.id, "summary", content=summary)
        upsert_artifact(db, lesson.id, "difficulties", content=difficulties)
        upsert_artifact(db, lesson.id, "homework", content=homework)
        db.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()

    redis_client = get_redis_client(settings)
    restored = restore_inflight_tasks(redis_client)
    if restored:
        logger.warning("Restored %s in-flight task(s) back to queue", restored)

    while True:
        raw_task = reserve_task(redis_client, timeout_seconds=5)
        if not raw_task:
            continue

        task_type, lesson_id = parse_task(raw_task)
        if not lesson_id:
            ack_task(redis_client, raw_task)
            continue
        if task_type not in {TASK_GENERATE_ARTIFACTS, TASK_PROCESS_AUDIO}:
            logger.error("Unknown task_type=%s for lesson %s, dropping task", task_type, lesson_id)
            ack_task(redis_client, raw_task)
            continue

        lock_acquired = acquire_lesson_lock(redis_client, lesson_id=lesson_id)
        if not lock_acquired:
            logger.info("Lesson %s lock is busy, requeue task", lesson_id)
            requeue_task(redis_client, raw_task)
            time.sleep(1)
            continue

        try:
            if task_type == TASK_GENERATE_ARTIFACTS:
                process_generate_artifacts(lesson_id)
            else:
                process_audio_lesson(lesson_id)
            ack_task(redis_client, raw_task)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Worker failed for lesson %s", lesson_id)
            attempts = 0
            with SessionLocal() as db:
                lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
                if lesson:
                    attempts = lesson.processing_attempts or 0
                    lesson.processing_status = "failed"
                    lesson.processing_error = str(exc)
                    db.commit()

            if attempts >= MAX_ATTEMPTS:
                logger.error("Lesson %s reached max attempts, acking task", lesson_id)
                ack_task(redis_client, raw_task)
            else:
                time.sleep(1)
                requeue_task(redis_client, raw_task)
        finally:
            release_lesson_lock(redis_client, lesson_id=lesson_id)


if __name__ == "__main__":
    main()
