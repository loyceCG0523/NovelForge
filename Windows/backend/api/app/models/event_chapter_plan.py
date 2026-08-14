"""剧情事件章节计划表模型。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class EventChapterPlan(IdMixin, TimestampMixin, Base):
    """StoryEvent 下某一章的结构化计划。"""

    __tablename__ = "event_chapter_plans"
    __table_args__ = (
        UniqueConstraint("story_event_id", "chapter_index", name="uq_event_chapter_plans_event_index"),
    )

    story_event_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("story_events.id"), index=True)
    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    chapter_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("chapters.id"), nullable=True)
    chapter_index: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    function: Mapped[str] = mapped_column(String(60), default="")
    core_event: Mapped[str] = mapped_column(Text, default="")
    ending_hook: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
