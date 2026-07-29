"""add built-in and user meme rag library

Revision ID: 20260723_0017
Revises: 20260720_0016
Create Date: 2026-07-23
"""

from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision: str = "20260723_0017"
down_revision: str | None = "20260720_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _seed_rows() -> list[dict]:
    resource = Path(__file__).resolve().parents[2] / "app" / "resources" / "meme_library_v1.json"
    payload = json.loads(resource.read_text(encoding="utf-8"))
    rows = []
    for raw in payload.get("entries") or []:
        phrase = str(raw.get("phrase") or "").strip()
        normalized = re.sub(r"\s+", "", phrase).casefold()[:120]
        years = [int(item) for item in re.findall(r"(?<!\d)(20\d{2})(?!\d)", raw.get("popularity_period") or "")]
        retrieval_text = "\n".join(
            [
                f"网络表达：{phrase}",
                f"真实含义：{raw.get('meaning') or ''}",
                f"出处背景：{raw.get('origin_event') or ''}",
                f"适用人物关系、情绪与场景：{raw.get('suitable_scenes') or ''}",
                f"流行时间：{raw.get('popularity_period') or ''}",
            ]
        )[:1800]
        canonical = "\n".join(
            str(value or "").strip()
            for value in (
                phrase,
                raw.get("meaning"),
                raw.get("origin_event"),
                raw.get("suitable_scenes"),
                raw.get("popularity_period"),
                raw.get("source_urls"),
            )
        )
        rows.append(
            {
                "id": uuid4(),
                "owner_id": None,
                "namespace": "builtin",
                "source_type": "builtin",
                "phrase": phrase,
                "normalized_phrase": normalized,
                "meaning": raw.get("meaning") or "",
                "origin_event": raw.get("origin_event") or "",
                "suitable_scenes": raw.get("suitable_scenes") or "",
                "popularity_period": raw.get("popularity_period") or "",
                "popularity_year_start": min(years) if years else None,
                "popularity_year_end": max(years) if years else None,
                "source_urls": raw.get("source_urls") or [],
                "retrieval_text": retrieval_text,
                "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "enabled": True,
                "review_status": "approved",
                "library_version": payload.get("library_version") or "builtin-v1",
                "embedding_model": "",
                "embedding": None,
                "metadata_payload": {
                    "source_file": payload.get("source_file") or "",
                    "source_sha256": payload.get("source_sha256") or "",
                },
            }
        )
    return rows


def upgrade() -> None:
    op.create_table(
        "meme_entries",
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("namespace", sa.String(length=100), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("phrase", sa.String(length=120), nullable=False),
        sa.Column("normalized_phrase", sa.String(length=120), nullable=False),
        sa.Column("meaning", sa.Text(), nullable=False),
        sa.Column("origin_event", sa.Text(), nullable=False),
        sa.Column("suitable_scenes", sa.Text(), nullable=False),
        sa.Column("popularity_period", sa.String(length=80), nullable=False),
        sa.Column("popularity_year_start", sa.Integer(), nullable=True),
        sa.Column("popularity_year_end", sa.Integer(), nullable=True),
        sa.Column("source_urls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retrieval_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("review_status", sa.String(length=20), nullable=False),
        sa.Column("library_version", sa.String(length=80), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("metadata_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("namespace", "normalized_phrase", name="uq_meme_entries_namespace_phrase"),
    )
    for column in (
        "owner_id",
        "namespace",
        "source_type",
        "popularity_year_start",
        "popularity_year_end",
        "content_hash",
        "enabled",
        "review_status",
        "embedding_model",
    ):
        op.create_index(op.f(f"ix_meme_entries_{column}"), "meme_entries", [column], unique=False)
    op.create_index(
        "ix_meme_entries_embedding_hnsw",
        "meme_entries",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_meme_entries_retrieval_trgm",
        "meme_entries",
        ["retrieval_text"],
        postgresql_using="gin",
        postgresql_ops={"retrieval_text": "gin_trgm_ops"},
    )

    meme_table = sa.table(
        "meme_entries",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("owner_id", postgresql.UUID(as_uuid=True)),
        sa.column("namespace", sa.String()),
        sa.column("source_type", sa.String()),
        sa.column("phrase", sa.String()),
        sa.column("normalized_phrase", sa.String()),
        sa.column("meaning", sa.Text()),
        sa.column("origin_event", sa.Text()),
        sa.column("suitable_scenes", sa.Text()),
        sa.column("popularity_period", sa.String()),
        sa.column("popularity_year_start", sa.Integer()),
        sa.column("popularity_year_end", sa.Integer()),
        sa.column("source_urls", postgresql.JSONB()),
        sa.column("retrieval_text", sa.Text()),
        sa.column("content_hash", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("review_status", sa.String()),
        sa.column("library_version", sa.String()),
        sa.column("embedding_model", sa.String()),
        sa.column("embedding", Vector(1536)),
        sa.column("metadata_payload", postgresql.JSONB()),
    )
    op.bulk_insert(meme_table, _seed_rows())


def downgrade() -> None:
    op.drop_index("ix_meme_entries_retrieval_trgm", table_name="meme_entries")
    op.drop_index("ix_meme_entries_embedding_hnsw", table_name="meme_entries")
    for column in (
        "embedding_model",
        "review_status",
        "enabled",
        "content_hash",
        "popularity_year_end",
        "popularity_year_start",
        "source_type",
        "namespace",
        "owner_id",
    ):
        op.drop_index(op.f(f"ix_meme_entries_{column}"), table_name="meme_entries")
    op.drop_table("meme_entries")
