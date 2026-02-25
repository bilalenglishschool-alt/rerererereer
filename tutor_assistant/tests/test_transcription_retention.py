from __future__ import annotations

import shutil
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from tutor_assistant.database import SessionLocal
from tutor_assistant.models import TranscriptionJob
from tutor_assistant.time_utils import utcnow
from tutor_assistant.worker import cleanup_transcription_jobs, settings as worker_settings


class TranscriptionRetentionCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._job_ids: list[str] = []
        self._job_dirs: set[Path] = set()

    def tearDown(self) -> None:
        with SessionLocal() as db:
            for job_id in self._job_ids:
                job = db.query(TranscriptionJob).filter(TranscriptionJob.id == UUID(job_id)).first()
                if job:
                    db.delete(job)
            db.commit()

        for job_dir in self._job_dirs:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _create_job(
        self,
        *,
        status: str,
        created_days_ago: int,
        processed_days_ago: int | None,
    ) -> str:
        job_uuid = uuid4()
        now = utcnow()
        job_dir = (worker_settings.storage_path / "transcriptions" / str(job_uuid)).resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
        source_path = job_dir / "source.webm"
        source_path.write_bytes(b"audio")
        transcript_path = job_dir / "transcript.txt"
        transcript_path.write_text("text", encoding="utf-8")

        processed_at = None
        if processed_days_ago is not None:
            processed_at = now - timedelta(days=processed_days_ago)

        with SessionLocal() as db:
            db.add(
                TranscriptionJob(
                    id=job_uuid,
                    source_path=str(source_path),
                    transcript_path=str(transcript_path),
                    transcript_text="some transcript",
                    status=status,
                    processing_attempts=1,
                    processing_error=None if status == "done" else "error",
                    created_at=now - timedelta(days=created_days_ago),
                    processed_at=processed_at,
                )
            )
            db.commit()

        self._job_ids.append(str(job_uuid))
        self._job_dirs.add(job_dir)
        return str(job_uuid)

    def test_cleanup_removes_only_stale_done_and_failed_jobs(self) -> None:
        stale_done_id = self._create_job(
            status="done",
            created_days_ago=30,
            processed_days_ago=30,
        )
        stale_failed_id = self._create_job(
            status="failed",
            created_days_ago=20,
            processed_days_ago=None,
        )
        recent_done_id = self._create_job(
            status="done",
            created_days_ago=1,
            processed_days_ago=1,
        )
        queued_old_id = self._create_job(
            status="queued",
            created_days_ago=40,
            processed_days_ago=None,
        )

        deleted_count = cleanup_transcription_jobs(retention_days=14, batch_size=100)
        self.assertEqual(deleted_count, 2)

        with SessionLocal() as db:
            stale_done = db.query(TranscriptionJob).filter(TranscriptionJob.id == UUID(stale_done_id)).first()
            stale_failed = db.query(TranscriptionJob).filter(
                TranscriptionJob.id == UUID(stale_failed_id)
            ).first()
            recent_done = db.query(TranscriptionJob).filter(TranscriptionJob.id == UUID(recent_done_id)).first()
            queued_old = db.query(TranscriptionJob).filter(TranscriptionJob.id == UUID(queued_old_id)).first()

        self.assertIsNone(stale_done)
        self.assertIsNone(stale_failed)
        self.assertIsNotNone(recent_done)
        self.assertIsNotNone(queued_old)

        stale_done_dir = worker_settings.storage_path / "transcriptions" / stale_done_id
        stale_failed_dir = worker_settings.storage_path / "transcriptions" / stale_failed_id
        recent_done_dir = worker_settings.storage_path / "transcriptions" / recent_done_id
        queued_old_dir = worker_settings.storage_path / "transcriptions" / queued_old_id

        self.assertFalse(stale_done_dir.exists())
        self.assertFalse(stale_failed_dir.exists())
        self.assertTrue(recent_done_dir.exists())
        self.assertTrue(queued_old_dir.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
