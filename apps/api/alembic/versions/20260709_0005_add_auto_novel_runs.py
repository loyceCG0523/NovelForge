"""add auto novel runs

Revision ID: 20260709_0005
Revises: 20260707_0004
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260709_0005"
down_revision: str | None = "20260707_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建整本书自动生产状态表。"""
    op.create_table(
        "auto_novel_runs",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("current_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("target_words", sa.Integer(), nullable=False),
        sa.Column("current_words", sa.Integer(), nullable=False),
        sa.Column("produced_event_count", sa.Integer(), nullable=False),
        sa.Column("max_event_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["current_event_id"], ["story_events.id"]),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_auto_novel_runs_novel_id"), "auto_novel_runs", ["novel_id"], unique=False)
    op.create_index(op.f("ix_auto_novel_runs_status"), "auto_novel_runs", ["status"], unique=False)


def downgrade() -> None:
    """删除整本书自动生产状态表。"""
    op.drop_index(op.f("ix_auto_novel_runs_status"), table_name="auto_novel_runs")
    op.drop_index(op.f("ix_auto_novel_runs_novel_id"), table_name="auto_novel_runs")
    op.drop_table("auto_novel_runs")
