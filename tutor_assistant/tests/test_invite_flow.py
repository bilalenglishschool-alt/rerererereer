from __future__ import annotations

import time
import unittest
from datetime import timedelta
from types import SimpleNamespace

from tutor_assistant.bot import claim_invite
from tutor_assistant.database import SessionLocal
from tutor_assistant.models import Invite, Student, Tutor, TutorStudent
from tutor_assistant.time_utils import utcnow


class InviteFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._seed = int(time.time() * 1000) % 1000000000
        self._tutor_tg_ids: list[int] = []
        self._student_tg_ids: list[int] = []

    def tearDown(self) -> None:
        self._cleanup_db()

    def _cleanup_db(self) -> None:
        with SessionLocal() as db:
            for tutor_tg_id in self._tutor_tg_ids:
                tutor = db.query(Tutor).filter(Tutor.tg_user_id == tutor_tg_id).first()
                if not tutor:
                    continue

                db.query(Invite).filter(Invite.tutor_id == tutor.id).delete(
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

    def _create_tutor(self, suffix: int) -> Tutor:
        tutor_tg_id = 750000000 + suffix
        self._tutor_tg_ids.append(tutor_tg_id)
        with SessionLocal() as db:
            tutor = Tutor(
                tg_user_id=tutor_tg_id,
                tg_username=f"invite_tutor_{suffix}",
                full_name="Invite Tutor",
            )
            db.add(tutor)
            db.commit()
            db.refresh(tutor)
            return tutor

    def _claim(self, token: str, user_id: int, username: str) -> tuple[bool, str]:
        self._student_tg_ids.append(user_id)
        fake_user = SimpleNamespace(
            id=user_id,
            username=username,
            full_name=f"{username} full name",
        )
        with SessionLocal() as db:
            return claim_invite(db, token=token, user=fake_user)  # type: ignore[arg-type]

    def test_invalid_token(self) -> None:
        ok, text = self._claim(token="does-not-exist", user_id=760000001, username="student_invalid")
        self.assertFalse(ok)
        self.assertIn("Инвайт не найден", text)

    def test_expired_token(self) -> None:
        tutor = self._create_tutor(self._seed % 1000000)
        token = f"expired-{self._seed}"
        with SessionLocal() as db:
            db.add(
                Invite(
                    token=token,
                    tutor_id=tutor.id,
                    expires_at=utcnow() - timedelta(seconds=60),
                    used_at=None,
                )
            )
            db.commit()

        ok, text = self._claim(token=token, user_id=760000002, username="student_expired")
        self.assertFalse(ok)
        self.assertIn("истек", text.lower())

    def test_used_token(self) -> None:
        tutor = self._create_tutor((self._seed + 1) % 1000000)
        token = f"used-{self._seed}"
        with SessionLocal() as db:
            db.add(
                Invite(
                    token=token,
                    tutor_id=tutor.id,
                    expires_at=utcnow() + timedelta(hours=1),
                    used_at=utcnow(),
                )
            )
            db.commit()

        ok, text = self._claim(token=token, user_id=760000003, username="student_used")
        self.assertFalse(ok)
        self.assertIn("уже использован", text.lower())

    def test_idempotent_claim_creates_single_link(self) -> None:
        tutor = self._create_tutor((self._seed + 2) % 1000000)
        token = f"idempotent-{self._seed}"
        with SessionLocal() as db:
            db.add(
                Invite(
                    token=token,
                    tutor_id=tutor.id,
                    expires_at=utcnow() + timedelta(hours=1),
                    used_at=None,
                )
            )
            db.commit()

        user_id = 760000004
        ok_first, text_first = self._claim(token=token, user_id=user_id, username="student_idempotent")
        ok_second, text_second = self._claim(
            token=token,
            user_id=user_id,
            username="student_idempotent",
        )

        self.assertTrue(ok_first)
        self.assertIn("готово", text_first.lower())
        self.assertFalse(ok_second)
        self.assertIn("уже использован", text_second.lower())

        with SessionLocal() as db:
            tutor_db = db.query(Tutor).filter(Tutor.tg_user_id == tutor.tg_user_id).first()
            self.assertIsNotNone(tutor_db)

            link_count = (
                db.query(TutorStudent)
                .join(Student, Student.id == TutorStudent.student_id)
                .filter(
                    TutorStudent.tutor_id == tutor_db.id,
                    Student.tg_user_id == user_id,
                )
                .count()
            )
            self.assertEqual(link_count, 1)

            invite = db.query(Invite).filter(Invite.token == token).first()
            self.assertIsNotNone(invite)
            self.assertIsNotNone(invite.used_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
