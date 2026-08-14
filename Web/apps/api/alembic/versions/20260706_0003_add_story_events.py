"""add story events

Revision ID: 20260706_0003
Revises: 20260630_0002
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260706_0003"
down_revision: str | None = "20260630_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建剧情事件和事件章节计划表。"""
    op.create_table(
        "story_events",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("core_conflict", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("start_chapter_index", sa.Integer(), nullable=True),
        sa.Column("end_chapter_index", sa.Integer(), nullable=True),
        sa.Column("planned_chapter_count", sa.Integer(), nullable=False),
        sa.Column("generated_chapter_count", sa.Integer(), nullable=False),
        sa.Column("auto_repair_count", sa.Integer(), nullable=False),
        sa.Column("remaining_open_risks", sa.Integer(), nullable=False),
        sa.Column("next_event_hook", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index(op.f("ix_story_events_novel_id"), "story_events", ["novel_id"], unique=False)
    op.create_index(op.f("ix_story_events_status"), "story_events", ["status"], unique=False)

    op.create_table(
        "event_chapter_plans",
        sa.Column("story_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chapter_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("function", sa.String(length=60), nullable=False),
        sa.Column("core_event", sa.Text(), nullable=False),
        sa.Column("ending_hook", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.ForeignKeyConstraint(["story_event_id"], ["story_events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("story_event_id", "chapter_index", name="uq_event_chapter_plans_event_index"),
    )
    op.create_index(op.f("ix_event_chapter_plans_chapter_index"), "event_chapter_plans", ["chapter_index"], unique=False)
    op.create_index(op.f("ix_event_chapter_plans_novel_id"), "event_chapter_plans", ["novel_id"], unique=False)
    op.create_index(op.f("ix_event_chapter_plans_status"), "event_chapter_plans", ["status"], unique=False)
    op.create_index(op.f("ix_event_chapter_plans_story_event_id"), "event_chapter_plans", ["story_event_id"], unique=False)


def downgrade() -> None:
    """删除剧情事件和事件章节计划表。"""
    op.drop_index(op.f("ix_event_chapter_plans_story_event_id"), table_name="event_chapter_plans")
    op.drop_index(op.f("ix_event_chapter_plans_status"), table_name="event_chapter_plans")
    op.drop_index(op.f("ix_event_chapter_plans_novel_id"), table_name="event_chapter_plans")
    op.drop_index(op.f("ix_event_chapter_plans_chapter_index"), table_name="event_chapter_plans")
    op.drop_table("event_chapter_plans")
    op.drop_index(op.f("ix_story_events_status"), table_name="story_events")
    op.drop_index(op.f("ix_story_events_novel_id"), table_name="story_events")
    op.drop_table("story_events")
