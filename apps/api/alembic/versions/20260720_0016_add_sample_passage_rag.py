"""add sample passage rag

Revision ID: 20260720_0016
Revises: 20260720_0015
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision: str = "20260720_0016"
down_revision: str | None = "20260720_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "sample_passages",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sample_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_index", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("passage_index", sa.Integer(), nullable=False),
        sa.Column("passage_type", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("technique_summary", sa.Text(), nullable=False),
        sa.Column("metadata_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sample_analysis_id"], ["sample_analyses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sample_analysis_id",
            "content_hash",
            name="uq_sample_passages_analysis_content_hash",
        ),
    )
    for column in ("owner_id", "sample_analysis_id", "chapter_index", "passage_type", "quality_score"):
        op.create_index(
            op.f(f"ix_sample_passages_{column}"),
            "sample_passages",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_sample_passages_embedding_hnsw",
        "sample_passages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_sample_passages_content_trgm",
        "sample_passages",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_sample_passages_content_trgm", table_name="sample_passages")
    op.drop_index("ix_sample_passages_embedding_hnsw", table_name="sample_passages")
    for column in ("quality_score", "passage_type", "chapter_index", "sample_analysis_id", "owner_id"):
        op.drop_index(op.f(f"ix_sample_passages_{column}"), table_name="sample_passages")
    op.drop_table("sample_passages")
