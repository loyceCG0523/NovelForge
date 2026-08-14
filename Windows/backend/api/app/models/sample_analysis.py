"""协同样本作品及其AI总体分析模型。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class SampleAnalysis(IdMixin, TimestampMixin, Base):
    """优秀作品的经验报告；原文件在对象存储，经验卡另建向量索引。"""

    __tablename__ = "sample_analyses"

    owner_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), index=True)
    novel_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), nullable=True, index=True)
    task_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("generation_tasks.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    sample_title: Mapped[str] = mapped_column(String(160), default="")
    source_author: Mapped[str] = mapped_column(String(120), default="")
    source_genre: Mapped[str] = mapped_column(String(80), default="")
    source_file_name: Mapped[str] = mapped_column(String(255), default="")
    source_object_key: Mapped[str] = mapped_column(String(512), default="")
    source_file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    source_word_count: Mapped[int] = mapped_column(Integer, default=0)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    analyzed_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    report: Mapped[dict] = mapped_column(JSONB, default=dict)
    visibility: Mapped[str] = mapped_column(String(20), default="private", index=True)
    publication_status: Mapped[str] = mapped_column(
        String(30), default="private", index=True
    )
    reuse_policy: Mapped[str] = mapped_column(
        String(30), default="reference_only", index=True
    )
    rights_declared: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
