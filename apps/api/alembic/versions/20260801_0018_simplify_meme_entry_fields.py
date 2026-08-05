"""keep only phrase, meaning and suitable scenes as meme content fields

Revision ID: 20260801_0018
Revises: 20260723_0017
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260801_0018"
down_revision: str | None = "20260723_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_meme_entries_popularity_year_end", table_name="meme_entries")
    op.drop_index("ix_meme_entries_popularity_year_start", table_name="meme_entries")
    op.drop_column("meme_entries", "source_urls")
    op.drop_column("meme_entries", "popularity_year_end")
    op.drop_column("meme_entries", "popularity_year_start")
    op.drop_column("meme_entries", "popularity_period")
    op.drop_column("meme_entries", "origin_event")


def downgrade() -> None:
    op.add_column(
        "meme_entries",
        sa.Column("origin_event", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "meme_entries",
        sa.Column("popularity_period", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column("meme_entries", sa.Column("popularity_year_start", sa.Integer(), nullable=True))
    op.add_column("meme_entries", sa.Column("popularity_year_end", sa.Integer(), nullable=True))
    op.add_column(
        "meme_entries",
        sa.Column(
            "source_urls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_index(
        "ix_meme_entries_popularity_year_start",
        "meme_entries",
        ["popularity_year_start"],
        unique=False,
    )
    op.create_index(
        "ix_meme_entries_popularity_year_end",
        "meme_entries",
        ["popularity_year_end"],
        unique=False,
    )
