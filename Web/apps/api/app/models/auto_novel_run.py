"""整本小说自动生产任务模型。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class AutoNovelRun(IdMixin, TimestampMixin, Base):
    """从起始需求到整本书生成完成的总控生产记录。"""

    __tablename__ = "auto_novel_runs"

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("generation_tasks.id"), nullable=True)
    current_event_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("story_events.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    stage: Mapped[str] = mapped_column(String(80), default="initializing")
    target_words: Mapped[int] = mapped_column(Integer, default=300000)
    current_words: Mapped[int] = mapped_column(Integer, default=0)
    produced_event_count: Mapped[int] = mapped_column(Integer, default=0)
    max_event_count: Mapped[int] = mapped_column(Integer, default=20)
    last_error: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
