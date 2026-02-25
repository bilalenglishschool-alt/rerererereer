from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .time_utils import utcnow


class Tutor(Base):
    __tablename__ = "tutors"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    tg_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    student_links: Mapped[list[TutorStudent]] = relationship(
        "TutorStudent",
        back_populates="tutor",
        cascade="all, delete-orphan",
    )
    lessons: Mapped[list[Lesson]] = relationship("Lesson", back_populates="tutor")
    invites: Mapped[list[Invite]] = relationship("Invite", back_populates="tutor")


class Student(Base):
    __tablename__ = "students"
    __table_args__ = (
        Index(
            "ux_students_tg_user_id_not_null",
            "tg_user_id",
            unique=True,
            postgresql_where=text("tg_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tg_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    tutor_links: Mapped[list[TutorStudent]] = relationship(
        "TutorStudent",
        back_populates="student",
        cascade="all, delete-orphan",
    )
    lessons: Mapped[list[Lesson]] = relationship("Lesson", back_populates="student")
    invites: Mapped[list[Invite]] = relationship("Invite", back_populates="student")


class TutorStudent(Base):
    __tablename__ = "tutor_student"

    tutor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tutors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    student_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("students.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    tutor: Mapped[Tutor] = relationship("Tutor", back_populates="student_links")
    student: Mapped[Student] = relationship("Student", back_populates="tutor_links")


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    tutor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tutors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("students.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    tutor: Mapped[Tutor] = relationship("Tutor", back_populates="invites")
    student: Mapped[Student | None] = relationship("Student", back_populates="invites")


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (
        Index(
            "ux_lessons_one_in_progress_per_tutor",
            "tutor_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tutor_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tutors.id"),
        index=True,
        nullable=False,
    )
    student_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("students.id"),
        index=True,
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_difficulties: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_homework: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_sent_to_tutor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sent_to_student: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    tutor: Mapped[Tutor] = relationship("Tutor", back_populates="lessons")
    student: Mapped[Student] = relationship("Student", back_populates="lessons")
    chunks: Mapped[list[LessonChunk]] = relationship("LessonChunk", back_populates="lesson")
    artifacts: Mapped[list[Artifact]] = relationship("Artifact", back_populates="lesson")


class LessonChunk(Base):
    __tablename__ = "lesson_chunks"
    __table_args__ = (UniqueConstraint("lesson_id", "seq", name="uq_lesson_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), index=True, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    lesson: Mapped[Lesson] = relationship("Lesson", back_populates="chunks")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    lesson: Mapped[Lesson] = relationship("Lesson", back_populates="artifacts")


class TranscriptionJob(Base):
    __tablename__ = "transcription_jobs"
    __table_args__ = (
        Index("ix_transcription_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    transcript_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
