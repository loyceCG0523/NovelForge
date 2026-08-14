"""add timeline entries

Revision ID: 20260714_0013
Revises: 20260712_0012
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260714_0013"
down_revision: str | None = "20260712_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "timeline_entries",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("story_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("story_day", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.String(length=80), nullable=False),
        sa.Column("end_time", sa.String(length=80), nullable=False),
        sa.Column("time_expression", sa.String(length=160), nullable=False),
        sa.Column("era", sa.String(length=160), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("participants", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("certainty", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.ForeignKeyConstraint(["story_event_id"], ["story_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_timeline_entries_chapter_id"), "timeline_entries", ["chapter_id"], unique=False)
    op.create_index(op.f("ix_timeline_entries_novel_id"), "timeline_entries", ["novel_id"], unique=False)
    op.create_index(op.f("ix_timeline_entries_sequence_no"), "timeline_entries", ["sequence_no"], unique=False)
    op.create_index(op.f("ix_timeline_entries_story_day"), "timeline_entries", ["story_day"], unique=False)
    op.create_index(op.f("ix_timeline_entries_story_event_id"), "timeline_entries", ["story_event_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_timeline_entries_story_event_id"), table_name="timeline_entries")
    op.drop_index(op.f("ix_timeline_entries_story_day"), table_name="timeline_entries")
    op.drop_index(op.f("ix_timeline_entries_sequence_no"), table_name="timeline_entries")
    op.drop_index(op.f("ix_timeline_entries_novel_id"), table_name="timeline_entries")
    op.drop_index(op.f("ix_timeline_entries_chapter_id"), table_name="timeline_entries")
    op.drop_table("timeline_entries")
