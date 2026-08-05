"""add chapter metadata to sample reading units

Revision ID: 20260803_0021
Revises: 20260803_0020
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260803_0021"
down_revision: str | None = "20260803_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sample_text_segments",
        sa.Column("title", sa.String(length=300), nullable=False, server_default=""),
    )
    op.add_column(
        "sample_text_segments",
        sa.Column(
            "unit_type",
            sa.String(length=30),
            nullable=False,
            server_default="legacy_segment",
        ),
    )
    op.execute(
        "UPDATE sample_text_segments "
        "SET title = '旧版阅读单元 ' || sequence_no::text "
        "WHERE title = ''"
    )
    op.create_index(
        "ix_sample_text_segments_unit_type",
        "sample_text_segments",
        ["unit_type"],
    )
    op.alter_column(
        "sample_text_segments",
        "unit_type",
        server_default="chapter",
    )


def downgrade() -> None:
    op.drop_index("ix_sample_text_segments_unit_type", table_name="sample_text_segments")
    op.drop_column("sample_text_segments", "unit_type")
    op.drop_column("sample_text_segments", "title")
