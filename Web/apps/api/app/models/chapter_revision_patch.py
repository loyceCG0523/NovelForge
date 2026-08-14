"""章节局部修订补丁。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class ChapterRevisionPatch(IdMixin, TimestampMixin, Base):
    """只替换已验证原文片段，并保留前后文本和审计状态。"""

    __tablename__ = "chapter_revision_patches"

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    story_event_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("story_events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    review_issue_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("review_issues.id", ondelete="SET NULL"), nullable=True, index=True
    )
    chapter_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    chapter_index: Mapped[int] = mapped_column(Integer, index=True)
    paragraph_index: Mapped[int] = mapped_column(Integer)
    paragraph_id: Mapped[str] = mapped_column(String(80), default="")
    base_content_hash: Mapped[str] = mapped_column(String(64))
    old_text_hash: Mapped[str] = mapped_column(String(64))
    old_text: Mapped[str] = mapped_column(Text)
    new_text: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="proposed", index=True)
    validation: Mapped[dict] = mapped_column(JSONB, default=dict)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
