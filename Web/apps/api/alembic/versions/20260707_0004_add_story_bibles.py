"""add story bibles

Revision ID: 20260707_0004
Revises: 20260706_0003
Create Date: 2026-07-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260707_0004"
down_revision: str | None = "20260706_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建作品圣经表。"""
    op.create_table(
        "story_bibles",
        sa.Column("novel_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("locked_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["novel_id"], ["novels.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("novel_id"),
    )
    op.create_index(op.f("ix_story_bibles_novel_id"), "story_bibles", ["novel_id"], unique=True)
    op.create_index(op.f("ix_story_bibles_status"), "story_bibles", ["status"], unique=False)


def downgrade() -> None:
    """删除作品圣经表。"""
    op.drop_index(op.f("ix_story_bibles_status"), table_name="story_bibles")
    op.drop_index(op.f("ix_story_bibles_novel_id"), table_name="story_bibles")
    op.drop_table("story_bibles")
