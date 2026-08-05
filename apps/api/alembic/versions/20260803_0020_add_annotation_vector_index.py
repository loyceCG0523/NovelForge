"""add hnsw index for trusted annotation retrieval

Revision ID: 20260803_0020
Revises: 20260803_0019
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0020"
down_revision: str | None = "20260803_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_sample_annotations_embedding_hnsw",
        "sample_annotations",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sample_annotations_embedding_hnsw", table_name="sample_annotations")
