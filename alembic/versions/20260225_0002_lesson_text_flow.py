"""lesson session flow v2: text chunks, sent_at, active lesson guard

Revision ID: 20260225_0002
Revises: 20260224_0001
Create Date: 2026-02-25 15:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260225_0002"
down_revision: Union[str, None] = "20260224_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("lessons", sa.Column("sent_at", sa.DateTime(), nullable=True))

    op.add_column("lesson_chunks", sa.Column("content", sa.Text(), nullable=True))
    op.alter_column(
        "lesson_chunks",
        "path",
        existing_type=sa.String(length=1024),
        nullable=True,
    )
    op.alter_column(
        "lesson_chunks",
        "size_bytes",
        existing_type=sa.Integer(),
        nullable=True,
    )

    op.add_column("artifacts", sa.Column("content", sa.Text(), nullable=True))
    op.alter_column(
        "artifacts",
        "path",
        existing_type=sa.String(length=1024),
        nullable=True,
    )

    op.create_index(
        "ux_lessons_one_in_progress_per_tutor",
        "lessons",
        ["tutor_id"],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
    )


def downgrade() -> None:
    op.drop_index("ux_lessons_one_in_progress_per_tutor", table_name="lessons")

    op.alter_column(
        "artifacts",
        "path",
        existing_type=sa.String(length=1024),
        nullable=False,
    )
    op.drop_column("artifacts", "content")

    op.alter_column(
        "lesson_chunks",
        "size_bytes",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "lesson_chunks",
        "path",
        existing_type=sa.String(length=1024),
        nullable=False,
    )
    op.drop_column("lesson_chunks", "content")

    op.drop_column("lessons", "sent_at")
