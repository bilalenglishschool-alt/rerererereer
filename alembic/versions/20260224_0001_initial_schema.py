"""initial clean schema for invite-based multi-teacher

Revision ID: 20260224_0001
Revises:
Create Date: 2026-02-24 23:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260224_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tutors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_username", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tutors_tg_user_id", "tutors", ["tg_user_id"], unique=True)

    op.create_table(
        "students",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("tg_username", sa.String(length=255), nullable=True),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_students_tg_user_id_not_null",
        "students",
        ["tg_user_id"],
        unique=True,
        postgresql_where=sa.text("tg_user_id IS NOT NULL"),
    )

    op.create_table(
        "tutor_student",
        sa.Column("tutor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tutor_id"], ["tutors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tutor_id", "student_id"),
    )

    op.create_table(
        "invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("tutor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tutor_id"], ["tutors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invites_token", "invites", ["token"], unique=True)
    op.create_index("ix_invites_tutor_id", "invites", ["tutor_id"], unique=False)
    op.create_index("ix_invites_student_id", "invites", ["student_id"], unique=False)

    op.create_table(
        "lessons",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tutor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("student_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'created'"), nullable=False),
        sa.Column(
            "processing_status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("processing_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("draft_summary", sa.Text(), nullable=True),
        sa.Column("draft_difficulties", sa.Text(), nullable=True),
        sa.Column("draft_homework", sa.Text(), nullable=True),
        sa.Column("draft_sent_to_tutor", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sent_to_student", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
        sa.ForeignKeyConstraint(["tutor_id"], ["tutors.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lessons_token", "lessons", ["token"], unique=True)
    op.create_index("ix_lessons_tutor_id", "lessons", ["tutor_id"], unique=False)
    op.create_index("ix_lessons_student_id", "lessons", ["student_id"], unique=False)

    op.create_table(
        "lesson_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lesson_id", "seq", name="uq_lesson_seq"),
    )
    op.create_index("ix_lesson_chunks_lesson_id", "lesson_chunks", ["lesson_id"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_lesson_id", "artifacts", ["lesson_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_artifacts_lesson_id", table_name="artifacts")
    op.drop_table("artifacts")

    op.drop_index("ix_lesson_chunks_lesson_id", table_name="lesson_chunks")
    op.drop_table("lesson_chunks")

    op.drop_index("ix_lessons_student_id", table_name="lessons")
    op.drop_index("ix_lessons_tutor_id", table_name="lessons")
    op.drop_index("ix_lessons_token", table_name="lessons")
    op.drop_table("lessons")

    op.drop_index("ix_invites_student_id", table_name="invites")
    op.drop_index("ix_invites_tutor_id", table_name="invites")
    op.drop_index("ix_invites_token", table_name="invites")
    op.drop_table("invites")

    op.drop_table("tutor_student")

    op.drop_index("ux_students_tg_user_id_not_null", table_name="students")
    op.drop_table("students")

    op.drop_index("ix_tutors_tg_user_id", table_name="tutors")
    op.drop_table("tutors")
