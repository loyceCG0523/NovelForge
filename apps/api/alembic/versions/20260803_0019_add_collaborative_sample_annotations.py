"""add collaborative sample annotation platform

Revision ID: 20260803_0019
Revises: 20260801_0018
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision: str = "20260803_0019"
down_revision: str | None = "20260801_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column(
        "sample_analyses",
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="private"),
    )
    op.add_column(
        "sample_analyses",
        sa.Column(
            "publication_status", sa.String(length=30), nullable=False, server_default="private"
        ),
    )
    op.add_column(
        "sample_analyses",
        sa.Column(
            "reuse_policy", sa.String(length=30), nullable=False, server_default="reference_only"
        ),
    )
    op.add_column(
        "sample_analyses",
        sa.Column("rights_declared", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "sample_analyses",
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "sample_analyses",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "sample_analyses",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("visibility", "publication_status", "reuse_policy", "content_hash"):
        op.create_index(f"ix_sample_analyses_{column}", "sample_analyses", [column])

    op.create_table(
        "sample_text_segments",
        sa.Column("sample_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("source_object_key", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["sample_analysis_id"], ["sample_analyses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sample_analysis_id", "sequence_no", name="uq_sample_text_segments_analysis_sequence"
        ),
    )
    op.create_index("ix_sample_text_segments_sample_analysis_id", "sample_text_segments", ["sample_analysis_id"])
    op.create_index("ix_sample_text_segments_content_hash", "sample_text_segments", ["content_hash"])

    op.create_table(
        "sample_annotations",
        sa.Column("sample_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("quote_text", sa.Text(), nullable=False),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("trust_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("end_offset > start_offset", name="ck_sample_annotations_range"),
        sa.ForeignKeyConstraint(["creator_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sample_analysis_id"], ["sample_analyses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["sample_text_segments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sample_analysis_id", "creator_id", "segment_id", "start_offset", "end_offset",
            name="uq_sample_annotations_creator_selection",
        ),
    )
    for column in ("sample_analysis_id", "segment_id", "creator_id", "status", "trust_score", "content_hash", "embedding_model"):
        op.create_index(f"ix_sample_annotations_{column}", "sample_annotations", [column])
    op.create_index(
        "ix_sample_annotations_categories_gin", "sample_annotations", ["categories"], postgresql_using="gin"
    )
    op.create_index(
        "ix_sample_annotations_quote_trgm",
        "sample_annotations",
        ["quote_text"],
        postgresql_using="gin",
        postgresql_ops={"quote_text": "gin_trgm_ops"},
    )

    op.create_table(
        "sample_annotation_revisions",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("editor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["annotation_id"], ["sample_annotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["editor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("annotation_id", "revision_no", name="uq_sample_annotation_revisions_annotation_revision"),
    )
    op.create_index("ix_sample_annotation_revisions_annotation_id", "sample_annotation_revisions", ["annotation_id"])
    op.create_index("ix_sample_annotation_revisions_editor_id", "sample_annotation_revisions", ["editor_id"])

    op.create_table(
        "sample_annotation_votes",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("value", sa.SmallInteger(), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("value IN (-1, 1)", name="ck_sample_annotation_votes_value"),
        sa.ForeignKeyConstraint(["annotation_id"], ["sample_annotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("annotation_id", "user_id", name="uq_sample_annotation_votes_user"),
    )
    op.create_index("ix_sample_annotation_votes_annotation_id", "sample_annotation_votes", ["annotation_id"])
    op.create_index("ix_sample_annotation_votes_user_id", "sample_annotation_votes", ["user_id"])

    op.create_table(
        "sample_annotation_reports",
        sa.Column("annotation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reporter_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="open"),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["annotation_id"], ["sample_annotations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reporter_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("annotation_id", "reporter_id", name="uq_sample_annotation_reports_user"),
    )
    for column in ("annotation_id", "reporter_id", "reason", "status"):
        op.create_index(f"ix_sample_annotation_reports_{column}", "sample_annotation_reports", [column])

    op.create_table(
        "sample_user_reputations",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("trusted_contribution_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_contribution_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_sample_user_reputations_user"),
    )
    op.create_index("ix_sample_user_reputations_user_id", "sample_user_reputations", ["user_id"])

    op.create_table(
        "sample_collaboration_events",
        sa.Column("sample_analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sample_analysis_id"], ["sample_analyses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sample_analysis_id", "sequence_no", name="uq_sample_collaboration_events_analysis_sequence"),
    )
    for column in ("sample_analysis_id", "actor_id", "event_type"):
        op.create_index(f"ix_sample_collaboration_events_{column}", "sample_collaboration_events", [column])


def downgrade() -> None:
    for table in (
        "sample_collaboration_events",
        "sample_user_reputations",
        "sample_annotation_reports",
        "sample_annotation_votes",
        "sample_annotation_revisions",
        "sample_annotations",
        "sample_text_segments",
    ):
        op.drop_table(table)
    for column in (
        "published_at", "version", "content_hash", "rights_declared", "reuse_policy",
        "publication_status", "visibility",
    ):
        op.drop_column("sample_analyses", column)
