"""剧情事件表模型。

StoryEvent 表示一段 4-12 章左右、具有阶段结果的连续剧情单元，是事件级生成 Graph 的正式业务实体。
"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class StoryEvent(IdMixin, TimestampMixin, Base):
    """一个完整剧情事件，例如一次考试、篮球赛、社团冲突或阶段性危机。"""

    __tablename__ = "story_events"

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("generation_tasks.id"),
        nullable=True,
        unique=True,
    )
    title: Mapped[str] = mapped_column(String(160), default="")
    goal: Mapped[str] = mapped_column(Text, default="")
    core_conflict: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    start_chapter_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_chapter_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    planned_chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    generated_chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_repair_count: Mapped[int] = mapped_column(Integer, default=0)
    remaining_open_risks: Mapped[int] = mapped_column(Integer, default=0)
    next_event_hook: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
