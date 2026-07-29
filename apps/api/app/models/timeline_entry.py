"""作品故事时间线条目。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class TimelineEntry(IdMixin, TimestampMixin, Base):
    """一个可排序、可追溯的故事内事件时间点或时间段。"""

    __tablename__ = "timeline_entries"

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    chapter_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("chapters.id"), nullable=True, index=True)
    story_event_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("story_events.id"), nullable=True, index=True)
    sequence_no: Mapped[int] = mapped_column(Integer, index=True)
    story_day: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    start_time: Mapped[str] = mapped_column(String(80), default="")
    end_time: Mapped[str] = mapped_column(String(80), default="")
    time_expression: Mapped[str] = mapped_column(String(160), default="")
    era: Mapped[str] = mapped_column(String(160), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    participants: Mapped[list] = mapped_column(JSONB, default=list)
    certainty: Mapped[str] = mapped_column(String(40), default="inferred")
    source: Mapped[str] = mapped_column(String(60), default="timeline_extractor")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
