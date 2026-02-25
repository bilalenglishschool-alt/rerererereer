from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from tutor_assistant.backend import app
from tutor_assistant.database import SessionLocal
from tutor_assistant.models import TranscriptionJob
from tutor_assistant.queue import LESSON_QUEUE_NAME, TASK_TRANSCRIBE_JOB
from tutor_assistant.worker import process_transcription_job


class _DummyRedis:
    class _DummyPipeline:
        def __init__(self, redis: "_DummyRedis") -> None:
            self._redis = redis
            self._ops: list[tuple[str, str, int | None]] = []

        def incr(self, key: str) -> "_DummyRedis._DummyPipeline":
            self._ops.append(("incr", key, None))
            return self

        def expire(self, key: str, ttl_seconds: int) -> "_DummyRedis._DummyPipeline":
            self._ops.append(("expire", key, ttl_seconds))
            return self

        def execute(self) -> list[int]:
            results: list[int] = []
            for op_name, key, ttl_seconds in self._ops:
                if op_name == "incr":
                    results.append(self._redis.incr(key))
                elif op_name == "expire":
                    assert ttl_seconds is not None
                    results.append(self._redis.expire(key, ttl_seconds))
            self._ops.clear()
            return results

    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []
        self.closed = False
        self._counters: dict[str, int] = {}
        self._ttls: dict[str, int] = {}

    def lpush(self, key: str, payload: str) -> int:
        self.pushed.append((key, payload))
        return len(self.pushed)

    def pipeline(self, transaction=True):  # noqa: ANN001, ARG002
        return _DummyRedis._DummyPipeline(self)

    def incr(self, key: str) -> int:
        value = int(self._counters.get(key, 0)) + 1
        self._counters[key] = value
        return value

    def expire(self, key: str, ttl_seconds: int) -> int:
        self._ttls[key] = int(ttl_seconds)
        return 1

    def ttl(self, key: str) -> int:
        return int(self._ttls.get(key, -1))

    def close(self) -> None:
        self.closed = True


class TranscriptionFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._job_ids: list[str] = []

    def tearDown(self) -> None:
        self._cleanup_jobs()

    def _cleanup_jobs(self) -> None:
        dirs_to_remove: set[Path] = set()
        with SessionLocal() as db:
            for job_id in self._job_ids:
                try:
                    job_uuid = UUID(job_id)
                except ValueError:
                    continue
                job = db.query(TranscriptionJob).filter(TranscriptionJob.id == job_uuid).first()
                if not job:
                    continue
                if job.source_path:
                    dirs_to_remove.add(Path(job.source_path).resolve().parent)
                if job.transcript_path:
                    dirs_to_remove.add(Path(job.transcript_path).resolve().parent)
                db.delete(job)
            db.commit()

        for job_dir in dirs_to_remove:
            shutil.rmtree(job_dir, ignore_errors=True)

    def test_create_job_enqueues_task_and_worker_marks_done(self) -> None:
        from unittest.mock import patch

        redis_stub = _DummyRedis()
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.webm", b"fake-audio-bytes", "audio/webm")},
                )
                self.assertEqual(response.status_code, 202)
                payload = response.json()
                job_id = payload["job_id"]
                self._job_ids.append(job_id)

                self.assertEqual(payload["status"], "queued")
                self.assertTrue(redis_stub.closed)
                self.assertEqual(len(redis_stub.pushed), 1)

                queue_name, raw_task = redis_stub.pushed[0]
                self.assertEqual(queue_name, LESSON_QUEUE_NAME)
                task = json.loads(raw_task)
                self.assertEqual(task["task_type"], TASK_TRANSCRIBE_JOB)
                self.assertEqual(task["lesson_id"], job_id)

                status_response = client.get(f"/api/transcribe/jobs/{job_id}")
                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(status_response.json()["status"], "queued")

        with patch("tutor_assistant.worker.transcribe_audio", return_value="ready transcript"):
            process_transcription_job(job_id)

        with TestClient(app) as client:
            done_response = client.get(f"/api/transcribe/jobs/{job_id}")
            self.assertEqual(done_response.status_code, 200)
            done_payload = done_response.json()
            self.assertEqual(done_payload["status"], "done")
            self.assertEqual(done_payload["transcript_text"], "ready transcript")

        with SessionLocal() as db:
            job = db.query(TranscriptionJob).filter(TranscriptionJob.id == UUID(job_id)).first()
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "done")
            self.assertIsNotNone(job.transcript_path)
            self.assertTrue(Path(job.transcript_path or "").exists())

    def test_retry_failed_job_puts_it_back_in_queue(self) -> None:
        from unittest.mock import patch

        redis_stub = _DummyRedis()
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.webm", b"fake-audio-bytes", "audio/webm")},
                )
                self.assertEqual(response.status_code, 202)
                job_id = response.json()["job_id"]
                self._job_ids.append(job_id)

                with SessionLocal() as db:
                    job = db.query(TranscriptionJob).filter(TranscriptionJob.id == UUID(job_id)).first()
                    self.assertIsNotNone(job)
                    job.status = "failed"
                    job.processing_error = "forced failure from test"
                    db.commit()

                retry_response = client.post(f"/api/transcribe/jobs/{job_id}/retry")
                self.assertEqual(retry_response.status_code, 200)
                retry_payload = retry_response.json()
                self.assertEqual(retry_payload["status"], "queued")
                self.assertTrue(retry_payload["queued"])

                self.assertEqual(len(redis_stub.pushed), 2)
                _, raw_task = redis_stub.pushed[-1]
                task = json.loads(raw_task)
                self.assertEqual(task["task_type"], TASK_TRANSCRIBE_JOB)
                self.assertEqual(task["lesson_id"], job_id)

    def test_rejects_unsupported_extension(self) -> None:
        from unittest.mock import patch

        with patch("tutor_assistant.backend.enforce_transcription_rate_limit", return_value=None):
            with TestClient(app) as client:
                response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.txt", b"fake-audio-bytes", "audio/webm")},
                )

        self.assertEqual(response.status_code, 415)
        self.assertIn("Unsupported file extension", response.json().get("detail", ""))

    def test_rejects_unsupported_content_type(self) -> None:
        from unittest.mock import patch

        with patch("tutor_assistant.backend.enforce_transcription_rate_limit", return_value=None):
            with TestClient(app) as client:
                response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.webm", b"fake-audio-bytes", "application/json")},
                )

        self.assertEqual(response.status_code, 415)
        self.assertIn("Unsupported content_type", response.json().get("detail", ""))

    def test_rejects_too_large_file(self) -> None:
        from unittest.mock import patch

        with patch("tutor_assistant.backend.enforce_transcription_rate_limit", return_value=None):
            with patch("tutor_assistant.backend.TRANSCRIPTION_MAX_UPLOAD_BYTES", 10):
                with TestClient(app) as client:
                    response = client.post(
                        "/api/transcribe/jobs",
                        files={"audio": ("sample.webm", b"12345678901", "audio/webm")},
                    )

        self.assertEqual(response.status_code, 413)
        self.assertIn("File too large", response.json().get("detail", ""))

    def test_rate_limit_blocks_excess_requests(self) -> None:
        from unittest.mock import patch

        redis_stub = _DummyRedis()
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with patch("tutor_assistant.backend.TRANSCRIPTION_RATE_LIMIT_PER_MINUTE", 1):
                with TestClient(app) as client:
                    first = client.post(
                        "/api/transcribe/jobs",
                        files={"audio": ("sample.webm", b"first-audio", "audio/webm")},
                    )
                    self.assertEqual(first.status_code, 202)
                    first_job_id = first.json()["job_id"]
                    self._job_ids.append(first_job_id)

                    second = client.post(
                        "/api/transcribe/jobs",
                        files={"audio": ("sample.webm", b"second-audio", "audio/webm")},
                    )
                    self.assertEqual(second.status_code, 429)
                    self.assertIn("Rate limit exceeded", second.json().get("detail", ""))

    def test_list_jobs_returns_recent_first(self) -> None:
        from unittest.mock import patch

        redis_stub = _DummyRedis()
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                first_response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.webm", b"first-audio", "audio/webm")},
                )
                self.assertEqual(first_response.status_code, 202)
                first_job_id = first_response.json()["job_id"]
                self._job_ids.append(first_job_id)

                second_response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.webm", b"second-audio", "audio/webm")},
                )
                self.assertEqual(second_response.status_code, 202)
                second_job_id = second_response.json()["job_id"]
                self._job_ids.append(second_job_id)

                list_response = client.get("/api/transcribe/jobs?limit=20")
                self.assertEqual(list_response.status_code, 200)
                payload = list_response.json()
                self.assertIn("items", payload)
                items = payload["items"]
                self.assertTrue(items)

                by_id = {item["job_id"]: idx for idx, item in enumerate(items)}
                self.assertIn(first_job_id, by_id)
                self.assertIn(second_job_id, by_id)
                self.assertLess(by_id[second_job_id], by_id[first_job_id])
                self.assertNotIn("transcript_text", items[0])

    def test_download_transcript_returns_409_when_not_ready(self) -> None:
        from unittest.mock import patch

        redis_stub = _DummyRedis()
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                create_response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.webm", b"audio-bytes", "audio/webm")},
                )
                self.assertEqual(create_response.status_code, 202)
                job_id = create_response.json()["job_id"]
                self._job_ids.append(job_id)

                download_response = client.get(f"/api/transcribe/jobs/{job_id}/transcript")
                self.assertEqual(download_response.status_code, 409)
                self.assertIn("Transcript is not ready yet", download_response.json().get("detail", ""))

    def test_download_transcript_returns_text_when_ready(self) -> None:
        from unittest.mock import patch

        redis_stub = _DummyRedis()
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                create_response = client.post(
                    "/api/transcribe/jobs",
                    files={"audio": ("sample.webm", b"audio-bytes", "audio/webm")},
                )
                self.assertEqual(create_response.status_code, 202)
                job_id = create_response.json()["job_id"]
                self._job_ids.append(job_id)

                with SessionLocal() as db:
                    job = db.query(TranscriptionJob).filter(TranscriptionJob.id == UUID(job_id)).first()
                    self.assertIsNotNone(job)
                    job.status = "done"
                    job.transcript_text = "Transcript content from test"
                    db.commit()

                download_response = client.get(f"/api/transcribe/jobs/{job_id}/transcript")
                self.assertEqual(download_response.status_code, 200)
                self.assertEqual(download_response.text, "Transcript content from test")
                self.assertIn("text/plain", download_response.headers.get("content-type", ""))
                self.assertIn(
                    f'transcript-{job_id}.txt',
                    download_response.headers.get("content-disposition", ""),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
