"""initial schema

Revision ID: 20260630_0001
Revises:
Create Date: 2026-06-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260630_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 NovelForge 首批核心业务表。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.Column("plan", sa.String(length=40), nullable=False),
        sa.Column("preferences", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "novels",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("genre", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("target_words", sa.Integer(), nullable=False),
        sa.Column("current_chapter_index", sa.Integer(), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column("brief", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_novels_owner_id"), "novels", ["owner_id"], unique=False)
    op.create_index(op.f("ix_novels_title"), "novels", ["title"], unique=False)

    op.create_table(
        "chapters",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("context_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("novel_id", "chapter_index", name="uq_chapters_novel_index"),
    )
    op.create_index(op.f("ix_chapters_novel_id"), "chapters", ["novel_id"], unique=False)

    op.create_table(
        "foreshadowing",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("planted_chapter_index", sa.Integer(), nullable=True),
        sa.Column("target_chapter_index", sa.Integer(), nullable=True),
        sa.Column("resolved_chapter_index", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_foreshadowing_novel_id"), "foreshadowing", ["novel_id"], unique=False)

    op.create_table(
        "memory_items",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("memory_type", sa.String(length=60), nullable=False),
        sa.Column("entity_name", sa.String(length=120), nullable=False),
        sa.Column("chapter_index_start", sa.Integer(), nullable=True),
        sa.Column("chapter_index_end", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_memory_items_entity_name"), "memory_items", ["entity_name"], unique=False)
    op.create_index(op.f("ix_memory_items_memory_type"), "memory_items", ["memory_type"], unique=False)
    op.create_index(op.f("ix_memory_items_novel_id"), "memory_items", ["novel_id"], unique=False)

    op.create_table(
        "generation_tasks",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_type", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_generation_tasks_novel_id"), "generation_tasks", ["novel_id"], unique=False)
    op.create_index(op.f("ix_generation_tasks_status"), "generation_tasks", ["status"], unique=False)
    op.create_index(op.f("ix_generation_tasks_task_type"), "generation_tasks", ["task_type"], unique=False)

    op.create_table(
        "review_issues",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("issue_type", sa.String(length=60), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"]),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_review_issues_issue_type"), "review_issues", ["issue_type"], unique=False)
    op.create_index(op.f("ix_review_issues_novel_id"), "review_issues", ["novel_id"], unique=False)
    op.create_index(op.f("ix_review_issues_severity"), "review_issues", ["severity"], unique=False)
    op.create_index(op.f("ix_review_issues_status"), "review_issues", ["status"], unique=False)


def downgrade() -> None:
    """按依赖顺序删除首批核心业务表。"""
    op.drop_index(op.f("ix_review_issues_status"), table_name="review_issues")
    op.drop_index(op.f("ix_review_issues_severity"), table_name="review_issues")
    op.drop_index(op.f("ix_review_issues_novel_id"), table_name="review_issues")
    op.drop_index(op.f("ix_review_issues_issue_type"), table_name="review_issues")
    op.drop_table("review_issues")
    op.drop_index(op.f("ix_generation_tasks_task_type"), table_name="generation_tasks")
    op.drop_index(op.f("ix_generation_tasks_status"), table_name="generation_tasks")
    op.drop_index(op.f("ix_generation_tasks_novel_id"), table_name="generation_tasks")
    op.drop_table("generation_tasks")
    op.drop_index(op.f("ix_memory_items_novel_id"), table_name="memory_items")
    op.drop_index(op.f("ix_memory_items_memory_type"), table_name="memory_items")
    op.drop_index(op.f("ix_memory_items_entity_name"), table_name="memory_items")
    op.drop_table("memory_items")
    op.drop_index(op.f("ix_foreshadowing_novel_id"), table_name="foreshadowing")
    op.drop_table("foreshadowing")
    op.drop_index(op.f("ix_chapters_novel_id"), table_name="chapters")
    op.drop_table("chapters")
    op.drop_index(op.f("ix_novels_title"), table_name="novels")
    op.drop_index(op.f("ix_novels_owner_id"), table_name="novels")
    op.drop_table("novels")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
