"""add observability tables

Revision ID: 20260711_0010
Revises: 20260711_0009
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260711_0010"
down_revision: str | None = "20260711_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 Worker 心跳和 LLM token 用量记录表。"""
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=160), nullable=False),
        sa.Column("hostname", sa.String(length=120), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("current_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["current_task_id"], ["generation_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_id"),
    )
    op.create_index(op.f("ix_worker_heartbeats_current_task_id"), "worker_heartbeats", ["current_task_id"], unique=False)
    op.create_index(op.f("ix_worker_heartbeats_last_seen_at"), "worker_heartbeats", ["last_seen_at"], unique=False)
    op.create_index(op.f("ix_worker_heartbeats_status"), "worker_heartbeats", ["status"], unique=False)
    op.create_index(op.f("ix_worker_heartbeats_worker_id"), "worker_heartbeats", ["worker_id"], unique=False)

    op.create_table(
        "llm_usage_records",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=120), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_usage_records_novel_id"), "llm_usage_records", ["novel_id"], unique=False)
    op.create_index(op.f("ix_llm_usage_records_operation"), "llm_usage_records", ["operation"], unique=False)
    op.create_index(op.f("ix_llm_usage_records_status"), "llm_usage_records", ["status"], unique=False)
    op.create_index(op.f("ix_llm_usage_records_task_id"), "llm_usage_records", ["task_id"], unique=False)


def downgrade() -> None:
    """删除观测表。"""
    op.drop_index(op.f("ix_llm_usage_records_task_id"), table_name="llm_usage_records")
    op.drop_index(op.f("ix_llm_usage_records_status"), table_name="llm_usage_records")
    op.drop_index(op.f("ix_llm_usage_records_operation"), table_name="llm_usage_records")
    op.drop_index(op.f("ix_llm_usage_records_novel_id"), table_name="llm_usage_records")
    op.drop_table("llm_usage_records")
    op.drop_index(op.f("ix_worker_heartbeats_worker_id"), table_name="worker_heartbeats")
    op.drop_index(op.f("ix_worker_heartbeats_status"), table_name="worker_heartbeats")
    op.drop_index(op.f("ix_worker_heartbeats_last_seen_at"), table_name="worker_heartbeats")
    op.drop_index(op.f("ix_worker_heartbeats_current_task_id"), table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
