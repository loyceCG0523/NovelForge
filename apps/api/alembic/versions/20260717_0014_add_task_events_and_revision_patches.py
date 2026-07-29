"""add task events and revision patches

Revision ID: 20260717_0014
Revises: 20260714_0013
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260717_0014"
down_revision: str | None = "20260714_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_task_events",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("step_key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chapter_index", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "sequence_no", name="uq_generation_task_events_task_sequence"),
    )
    for column in ("novel_id", "task_id", "event_type", "step_key", "status", "chapter_id"):
        op.create_index(op.f(f"ix_generation_task_events_{column}"), "generation_task_events", [column], unique=False)

    op.create_table(
        "chapter_revision_patches",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("story_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("review_issue_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_index", sa.Integer(), nullable=False),
        sa.Column("paragraph_index", sa.Integer(), nullable=False),
        sa.Column("paragraph_id", sa.String(length=80), nullable=False),
        sa.Column("base_content_hash", sa.String(length=64), nullable=False),
        sa.Column("old_text_hash", sa.String(length=64), nullable=False),
        sa.Column("old_text", sa.Text(), nullable=False),
        sa.Column("new_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_issue_id"], ["review_issues.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["story_event_id"], ["story_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("novel_id", "task_id", "story_event_id", "review_issue_id", "chapter_id", "chapter_index", "status"):
        op.create_index(op.f(f"ix_chapter_revision_patches_{column}"), "chapter_revision_patches", [column], unique=False)


def downgrade() -> None:
    for column in ("status", "chapter_index", "chapter_id", "review_issue_id", "story_event_id", "task_id", "novel_id"):
        op.drop_index(op.f(f"ix_chapter_revision_patches_{column}"), table_name="chapter_revision_patches")
    op.drop_table("chapter_revision_patches")
    for column in ("chapter_id", "status", "step_key", "event_type", "task_id", "novel_id"):
        op.drop_index(op.f(f"ix_generation_task_events_{column}"), table_name="generation_task_events")
    op.drop_table("generation_task_events")
