"""生成任务的可恢复执行事件。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class GenerationTaskEvent(IdMixin, TimestampMixin, Base):
    """Worker 执行过程中追加写入的步骤、预览和修订事件。"""

    __tablename__ = "generation_task_events"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence_no", name="uq_generation_task_events_task_sequence"),
    )

    novel_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("novels.id", ondelete="CASCADE"), nullable=True, index=True
    )
    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="CASCADE"), index=True
    )
    sequence_no: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    step_key: Mapped[str] = mapped_column(String(100), default="", index=True)
    status: Mapped[str] = mapped_column(String(30), default="info", index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    chapter_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("chapters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    chapter_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
