from __future__ import annotations

import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from tutor_assistant.database import SessionLocal
from tutor_assistant.models import Lesson, Student, TranscriptionJob, Tutor, TutorStudent
from tutor_assistant.time_utils import utcnow
from tutor_assistant.worker import MAX_ATTEMPTS, handle_task_failure


class _DummyRedis:
    def pipeline(self, transaction=True):  # noqa: ANN001, ARG002
        return self

    def incr(self, key):  # noqa: ANN001, ARG002
        return 1

    def zadd(self, key, mapping):  # noqa: ANN001, ARG002
        return 1

    def zremrangebyscore(self, key, min_score, max_score):  # noqa: ANN001, ARG002
        return 0

    def execute(self):
        return []


class WorkerRetryPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._seed = int(time.time() * 1000) % 1000000000
        self._tutor_tg_ids: list[int] = []
        self._student_tg_ids: list[int] = []

    def tearDown(self) -> None:
        self._cleanup_db()

    def _cleanup_db(self) -> None:
        with SessionLocal() as db:
            db.query(TranscriptionJob).delete(synchronize_session=False)
            for tutor_tg_id in self._tutor_tg_ids:
                tutor = db.query(Tutor).filter(Tutor.tg_user_id == tutor_tg_id).first()
                if not tutor:
                    continue

                db.query(Lesson).filter(Lesson.tutor_id == tutor.id).delete(
                    synchronize_session=False
                )
                db.query(TutorStudent).filter(TutorStudent.tutor_id == tutor.id).delete(
                    synchronize_session=False
                )
                db.delete(tutor)

            if self._student_tg_ids:
                db.query(Student).filter(Student.tg_user_id.in_(self._student_tg_ids)).delete(
                    synchronize_session=False
                )

            db.commit()

    def _create_lesson_with_attempts(self, attempts: int, suffix: int) -> str:
        tutor_tg_id = 770000000 + suffix
        student_tg_id = 780000000 + suffix
        self._tutor_tg_ids.append(tutor_tg_id)
        self._student_tg_ids.append(student_tg_id)

        with SessionLocal() as db:
            tutor = Tutor(
                tg_user_id=tutor_tg_id,
                tg_username=f"worker_tutor_{suffix}",
                full_name="Worker Tutor",
            )
            student = Student(
                name="Worker Student",
                tg_user_id=student_tg_id,
                tg_username=f"worker_student_{suffix}",
            )
            db.add_all([tutor, student])
            db.flush()
            db.add(TutorStudent(tutor_id=tutor.id, student_id=student.id, is_active=True))

            lesson_id = str(uuid4())
            lesson = Lesson(
                id=lesson_id,
                tutor_id=tutor.id,
                student_id=student.id,
                token=f"worker-token-{suffix}",
                status="processing",
                processing_status="processing",
                processing_attempts=attempts,
                started_at=utcnow(),
            )
            db.add(lesson)
            db.commit()
            return lesson_id

    def _create_transcription_job_with_attempts(self, attempts: int) -> str:
        job_uuid = uuid4()
        with SessionLocal() as db:
            job = TranscriptionJob(
                id=job_uuid,
                source_path=f"/tmp/{job_uuid}.webm",
                status="processing",
                processing_attempts=attempts,
            )
            db.add(job)
            db.commit()
        return str(job_uuid)

    def test_handle_failure_requeues_when_attempts_below_max(self) -> None:
        lesson_id = self._create_lesson_with_attempts(
            attempts=MAX_ATTEMPTS - 1,
            suffix=self._seed % 1000000,
        )
        redis_client = _DummyRedis()

        with patch("tutor_assistant.worker.requeue_task") as mocked_requeue:
            with patch("tutor_assistant.worker.dead_letter_task") as mocked_dead_letter:
                with patch("tutor_assistant.worker.time.sleep", return_value=None):
                    result = handle_task_failure(
                        redis_client=redis_client,
                        raw_task="raw-task",
                        lesson_id=lesson_id,
                        task_type="generate_artifacts",
                        exc=RuntimeError("transient failure"),
                    )

        self.assertEqual(result, "requeued")
        mocked_requeue.assert_called_once()
        mocked_dead_letter.assert_not_called()

        with SessionLocal() as db:
            lesson = db.query(Lesson).filter(Lesson.id == lesson_id).first()
            self.assertIsNotNone(lesson)
            self.assertEqual(lesson.processing_status, "failed")
            self.assertIn("transient failure", lesson.processing_error or "")

    def test_handle_failure_dead_letters_when_attempts_reached(self) -> None:
        lesson_id = self._create_lesson_with_attempts(
            attempts=MAX_ATTEMPTS,
            suffix=(self._seed + 1) % 1000000,
        )
        redis_client = _DummyRedis()

        with patch("tutor_assistant.worker.requeue_task") as mocked_requeue:
            with patch("tutor_assistant.worker.dead_letter_task") as mocked_dead_letter:
                with patch("tutor_assistant.worker.time.sleep", return_value=None):
                    result = handle_task_failure(
                        redis_client=redis_client,
                        raw_task="raw-task",
                        lesson_id=lesson_id,
                        task_type="generate_artifacts",
                        exc=RuntimeError("permanent failure"),
                    )

        self.assertEqual(result, "dead_letter")
        mocked_requeue.assert_not_called()
        mocked_dead_letter.assert_called_once()

        call_kwargs = mocked_dead_letter.call_args.kwargs
        self.assertEqual(call_kwargs.get("lesson_id"), lesson_id)
        self.assertEqual(call_kwargs.get("task_type"), "generate_artifacts")
        self.assertIn("max attempts reached", str(call_kwargs.get("reason")))

    def test_transcription_job_dead_letters_when_attempts_reached(self) -> None:
        from tutor_assistant.queue import TASK_TRANSCRIBE_JOB

        job_id = self._create_transcription_job_with_attempts(attempts=MAX_ATTEMPTS)
        redis_client = _DummyRedis()

        with patch("tutor_assistant.worker.requeue_task") as mocked_requeue:
            with patch("tutor_assistant.worker.dead_letter_task") as mocked_dead_letter:
                with patch("tutor_assistant.worker.time.sleep", return_value=None):
                    result = handle_task_failure(
                        redis_client=redis_client,
                        raw_task="raw-task",
                        lesson_id=job_id,
                        task_type=TASK_TRANSCRIBE_JOB,
                        exc=RuntimeError("transcription failed"),
                    )

        self.assertEqual(result, "dead_letter")
        mocked_requeue.assert_not_called()
        mocked_dead_letter.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
