"""add sample analyses

Revision ID: 20260709_0006
Revises: 20260709_0005
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260709_0006"
down_revision: str | None = "20260709_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建样本分析报告表。"""
    op.create_table(
        "sample_analyses",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("sample_title", sa.String(length=160), nullable=False),
        sa.Column("source_author", sa.String(length=120), nullable=False),
        sa.Column("source_genre", sa.String(length=80), nullable=False),
        sa.Column("source_word_count", sa.Integer(), nullable=False),
        sa.Column("chapter_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sample_analyses_novel_id"), "sample_analyses", ["novel_id"], unique=False)
    op.create_index(op.f("ix_sample_analyses_status"), "sample_analyses", ["status"], unique=False)


def downgrade() -> None:
    """删除样本分析报告表。"""
    op.drop_index(op.f("ix_sample_analyses_status"), table_name="sample_analyses")
    op.drop_index(op.f("ix_sample_analyses_novel_id"), table_name="sample_analyses")
    op.drop_table("sample_analyses")
