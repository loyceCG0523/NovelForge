"""样本分段、协同标注、投票、举报、信誉和事件模型。"""

from __future__ import annotations

from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class SampleTextSegment(IdMixin, TimestampMixin, Base):
    """原文的章节阅读单元；保留历史表名以兼容既有标注外键。"""

    __tablename__ = "sample_text_segments"
    __table_args__ = (
        UniqueConstraint(
            "sample_analysis_id",
            "sequence_no",
            name="uq_sample_text_segments_analysis_sequence",
        ),
    )

    sample_analysis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_analyses.id", ondelete="CASCADE"),
        index=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300), default="")
    unit_type: Mapped[str] = mapped_column(String(30), default="chapter", index=True)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    source_object_key: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)


class SampleAnnotation(IdMixin, TimestampMixin, Base):
    """用户对一段原文的独立、多分类标注。"""

    __tablename__ = "sample_annotations"
    __table_args__ = (
        UniqueConstraint(
            "sample_analysis_id",
            "creator_id",
            "segment_id",
            "start_offset",
            "end_offset",
            name="uq_sample_annotations_creator_selection",
        ),
        CheckConstraint("end_offset > start_offset", name="ck_sample_annotations_range"),
        Index(
            "ix_sample_annotations_categories_gin",
            "categories",
            postgresql_using="gin",
        ),
        Index(
            "ix_sample_annotations_quote_trgm",
            "quote_text",
            postgresql_using="gin",
            postgresql_ops={"quote_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_sample_annotations_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=text("embedding IS NOT NULL"),
        ),
    )

    sample_analysis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_analyses.id", ondelete="CASCADE"),
        index=True,
    )
    segment_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_text_segments.id", ondelete="CASCADE"),
        index=True,
    )
    creator_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    quote_text: Mapped[str] = mapped_column(Text)
    categories: Mapped[list] = mapped_column(JSONB, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    trust_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding_model: Mapped[str] = mapped_column(String(200), default="", index=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dimensions), nullable=True
    )


class SampleAnnotationRevision(IdMixin, TimestampMixin, Base):
    """标注修改前的追加式快照。"""

    __tablename__ = "sample_annotation_revisions"
    __table_args__ = (
        UniqueConstraint(
            "annotation_id",
            "revision_no",
            name="uq_sample_annotation_revisions_annotation_revision",
        ),
    )

    annotation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_annotations.id", ondelete="CASCADE"),
        index=True,
    )
    editor_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class SampleAnnotationVote(IdMixin, TimestampMixin, Base):
    """每名用户对每条标注只有一个当前有效投票。"""

    __tablename__ = "sample_annotation_votes"
    __table_args__ = (
        UniqueConstraint(
            "annotation_id", "user_id", name="uq_sample_annotation_votes_user"
        ),
        CheckConstraint("value IN (-1, 1)", name="ck_sample_annotation_votes_value"),
    )

    annotation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_annotations.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    value: Mapped[int] = mapped_column(SmallInteger)


class SampleAnnotationReport(IdMixin, TimestampMixin, Base):
    """用户对公共标注的结构化举报。"""

    __tablename__ = "sample_annotation_reports"
    __table_args__ = (
        UniqueConstraint(
            "annotation_id", "reporter_id", name="uq_sample_annotation_reports_user"
        ),
    )

    annotation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_annotations.id", ondelete="CASCADE"),
        index=True,
    )
    reporter_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(String(40), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)


class SampleUserReputation(IdMixin, TimestampMixin, Base):
    """只用于标注聚合权重的有界信誉值。"""

    __tablename__ = "sample_user_reputations"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_sample_user_reputations_user"),
    )

    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[float] = mapped_column(Float, default=1.0)
    trusted_contribution_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_contribution_count: Mapped[int] = mapped_column(Integer, default=0)


class SampleCollaborationEvent(IdMixin, TimestampMixin, Base):
    """SSE断线重连时可按序号补拉的持久事件。"""

    __tablename__ = "sample_collaboration_events"
    __table_args__ = (
        UniqueConstraint(
            "sample_analysis_id",
            "sequence_no",
            name="uq_sample_collaboration_events_analysis_sequence",
        ),
    )

    sample_analysis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_analyses.id", ondelete="CASCADE"),
        index=True,
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
