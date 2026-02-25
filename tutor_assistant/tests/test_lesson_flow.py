from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from aiogram import Bot

from tutor_assistant.bot import process_update
from tutor_assistant.database import SessionLocal, engine
from tutor_assistant.models import Artifact, Invite, Lesson, LessonChunk, Student, Tutor, TutorStudent
from tutor_assistant.worker import process_generate_artifacts


class LessonFlowCommandsTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        engine.dispose()
        super().tearDownClass()

    def setUp(self) -> None:
        self._seed = int(time.time() * 1000) % 1000000000
        self._update_id = 800000000 + self._seed
        self._tutor_ids: list[int] = []
        self._student_ids: list[int] = []
        self.sent_messages: list[dict[str, object]] = []

        async def fake_bot_call(bot_self, method, request_timeout=None):  # noqa: ANN001, ARG001
            method_name = method.__class__.__name__
            if method_name == "SendMessage":
                self.sent_messages.append(
                    {
                        "chat_id": getattr(method, "chat_id", None),
                        "text": getattr(method, "text", "") or "",
                    }
                )
            return None

        self._bot_call_patcher = patch.object(Bot, "__call__", new=fake_bot_call)
        self._bot_call_patcher.start()

    def tearDown(self) -> None:
        self._bot_call_patcher.stop()
        self._cleanup_db()

    def _cleanup_db(self) -> None:
        with SessionLocal() as db:
            for tutor_tg_id in self._tutor_ids:
                tutor = db.query(Tutor).filter(Tutor.tg_user_id == tutor_tg_id).first()
                if not tutor:
                    continue

                lesson_rows = db.query(Lesson.id).filter(Lesson.tutor_id == tutor.id).all()
                lesson_ids = [row[0] for row in lesson_rows]

                if lesson_ids:
                    db.query(Artifact).filter(Artifact.lesson_id.in_(lesson_ids)).delete(
                        synchronize_session=False
                    )
                    db.query(LessonChunk).filter(LessonChunk.lesson_id.in_(lesson_ids)).delete(
                        synchronize_session=False
                    )
                    db.query(Lesson).filter(Lesson.id.in_(lesson_ids)).delete(
                        synchronize_session=False
                    )

                db.query(Invite).filter(Invite.tutor_id == tutor.id).delete(
                    synchronize_session=False
                )
                db.query(TutorStudent).filter(TutorStudent.tutor_id == tutor.id).delete(
                    synchronize_session=False
                )
                db.delete(tutor)

            if self._student_ids:
                db.query(Student).filter(Student.tg_user_id.in_(self._student_ids)).delete(
                    synchronize_session=False
                )

            db.commit()

    def _mk_update(self, user_id: int, username: str, text: str) -> dict:
        self._update_id += 1
        return {
            "update_id": self._update_id,
            "message": {
                "message_id": self._update_id,
                "date": int(time.time()),
                "chat": {
                    "id": user_id,
                    "type": "private",
                    "username": username,
                },
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": username,
                    "username": username,
                },
                "text": text,
            },
        }

    async def _send(self, user_id: int, username: str, text: str) -> None:
        await process_update(self._mk_update(user_id=user_id, username=username, text=text))

    def _messages_for(self, chat_id: int) -> list[str]:
        return [
            str(item.get("text") or "")
            for item in self.sent_messages
            if item.get("chat_id") == chat_id
        ]

    def _create_tutor_and_student_ids(self, suffix: int) -> tuple[int, int]:
        tutor_id = 710000000 + suffix
        student_id = 720000000 + suffix
        self._tutor_ids.append(tutor_id)
        self._student_ids.append(student_id)
        return tutor_id, student_id

    def _get_student_uuid(self, tutor_tg_id: int, student_tg_id: int) -> str:
        with SessionLocal() as db:
            tutor = db.query(Tutor).filter(Tutor.tg_user_id == tutor_tg_id).first()
            self.assertIsNotNone(tutor)

            student = (
                db.query(Student)
                .join(TutorStudent, TutorStudent.student_id == Student.id)
                .filter(
                    TutorStudent.tutor_id == tutor.id,
                    Student.tg_user_id == student_tg_id,
                )
                .first()
            )
            self.assertIsNotNone(student)
            return str(student.id)

    def _get_latest_lesson(self, tutor_tg_id: int) -> Lesson:
        with SessionLocal() as db:
            tutor = db.query(Tutor).filter(Tutor.tg_user_id == tutor_tg_id).first()
            self.assertIsNotNone(tutor)
            lesson = (
                db.query(Lesson)
                .filter(Lesson.tutor_id == tutor.id)
                .order_by(Lesson.created_at.desc())
                .first()
            )
            self.assertIsNotNone(lesson)
            return lesson

    async def test_text_lesson_lifecycle_to_sent(self) -> None:
        tutor_tg_id, student_tg_id = self._create_tutor_and_student_ids(self._seed % 1000000)
        tutor_username = f"tutor_{self._seed}"

        await self._send(tutor_tg_id, tutor_username, "/start")
        await self._send(
            tutor_tg_id,
            tutor_username,
            f"/add_student Student {self._seed} | {student_tg_id}",
        )

        student_uuid = self._get_student_uuid(tutor_tg_id=tutor_tg_id, student_tg_id=student_tg_id)

        await self._send(tutor_tg_id, tutor_username, f"/lesson_start {student_uuid}")
        await self._send(tutor_tg_id, tutor_username, "/lesson_add Разобрали Present Perfect.")
        await self._send(tutor_tg_id, tutor_username, "/lesson_add Сделали 10 упражнений.")

        with patch(
            "tutor_assistant.worker.generate_draft",
            return_value={
                "summary": "Custom summary from test.",
                "difficulties": "Custom difficulties from test.",
                "homework": "Custom homework from test.",
            },
        ):
            with patch(
                "tutor_assistant.bot.enqueue_generate_artifacts_task",
                side_effect=process_generate_artifacts,
            ):
                await self._send(tutor_tg_id, tutor_username, "/lesson_finish")

        lesson = self._get_latest_lesson(tutor_tg_id=tutor_tg_id)
        self.assertEqual(lesson.status, "draft_ready")
        self.assertEqual(lesson.processing_status, "done")

        await self._send(tutor_tg_id, tutor_username, "/lesson_send")

        with SessionLocal() as db:
            lesson = db.query(Lesson).filter(Lesson.id == lesson.id).first()
            self.assertIsNotNone(lesson)
            self.assertEqual(lesson.status, "sent")
            self.assertEqual(lesson.processing_status, "done")
            self.assertIsNotNone(lesson.sent_at)
            self.assertEqual(lesson.draft_summary, "Custom summary from test.")
            self.assertEqual(lesson.draft_difficulties, "Custom difficulties from test.")
            self.assertEqual(lesson.draft_homework, "Custom homework from test.")

            artifacts = db.query(Artifact).filter(Artifact.lesson_id == lesson.id).all()
            artifact_kinds = sorted(item.kind for item in artifacts)
            self.assertEqual(artifact_kinds, ["difficulties", "homework", "summary"])

        student_messages = self._messages_for(student_tg_id)
        self.assertTrue(student_messages, "student should receive lesson summary")
        self.assertIn("Custom summary from test.", student_messages[-1])

    async def test_lesson_finish_requires_chunks(self) -> None:
        tutor_tg_id, student_tg_id = self._create_tutor_and_student_ids((self._seed + 1) % 1000000)
        tutor_username = f"tutor_{self._seed + 1}"

        await self._send(tutor_tg_id, tutor_username, "/start")
        await self._send(
            tutor_tg_id,
            tutor_username,
            f"/add_student Student {self._seed + 1} | {student_tg_id}",
        )

        student_uuid = self._get_student_uuid(tutor_tg_id=tutor_tg_id, student_tg_id=student_tg_id)
        await self._send(tutor_tg_id, tutor_username, f"/lesson_start {student_uuid}")
        await self._send(tutor_tg_id, tutor_username, "/lesson_finish")

        lesson = self._get_latest_lesson(tutor_tg_id=tutor_tg_id)
        self.assertEqual(lesson.status, "in_progress")
        self.assertEqual(lesson.processing_status, "pending")

        tutor_messages = self._messages_for(tutor_tg_id)
        self.assertTrue(
            any("Нельзя завершить урок без /lesson_add." in text for text in tutor_messages)
        )

    async def test_cannot_start_second_active_lesson(self) -> None:
        tutor_tg_id, student1_tg_id = self._create_tutor_and_student_ids((self._seed + 2) % 1000000)
        student2_tg_id = 720500000 + (self._seed % 1000000)
        self._student_ids.append(student2_tg_id)
        tutor_username = f"tutor_{self._seed + 2}"

        await self._send(tutor_tg_id, tutor_username, "/start")
        await self._send(
            tutor_tg_id,
            tutor_username,
            f"/add_student Student A {self._seed} | {student1_tg_id}",
        )
        await self._send(
            tutor_tg_id,
            tutor_username,
            f"/add_student Student B {self._seed} | {student2_tg_id}",
        )

        student1_uuid = self._get_student_uuid(tutor_tg_id=tutor_tg_id, student_tg_id=student1_tg_id)
        student2_uuid = self._get_student_uuid(tutor_tg_id=tutor_tg_id, student_tg_id=student2_tg_id)

        await self._send(tutor_tg_id, tutor_username, f"/lesson_start {student1_uuid}")
        await self._send(tutor_tg_id, tutor_username, f"/lesson_start {student2_uuid}")

        with SessionLocal() as db:
            tutor = db.query(Tutor).filter(Tutor.tg_user_id == tutor_tg_id).first()
            self.assertIsNotNone(tutor)
            in_progress_count = (
                db.query(Lesson)
                .filter(
                    Lesson.tutor_id == tutor.id,
                    Lesson.status == "in_progress",
                )
                .count()
            )
            self.assertEqual(in_progress_count, 1)

        tutor_messages = self._messages_for(tutor_tg_id)
        self.assertTrue(
            any("У вас уже есть активный урок." in text for text in tutor_messages)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
