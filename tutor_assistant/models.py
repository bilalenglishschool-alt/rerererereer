from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Tutor(Base):
    __tablename__ = "tutors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    tg_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    students: Mapped[list[Student]] = relationship("Student", back_populates="tutor")
    lessons: Mapped[list[Lesson]] = relationship("Lesson", back_populates="tutor")


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutors.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tg_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    tg_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    tutor: Mapped[Tutor] = relationship("Tutor", back_populates="students")
    lessons: Mapped[list[Lesson]] = relationship("Lesson", back_populates="student")


class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("tutors.id"), index=True, nullable=False)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True, nullable=False)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="created", nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    transcript_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    draft_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    draft_difficulties: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    draft_homework: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    draft_sent_to_tutor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sent_to_student: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

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
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    lesson: Mapped[Lesson] = relationship("Lesson", back_populates="chunks")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    lesson: Mapped[Lesson] = relationship("Lesson", back_populates="artifacts")
