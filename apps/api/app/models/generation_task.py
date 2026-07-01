"""后台生成任务表模型。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class GenerationTask(IdMixin, TimestampMixin, Base):
    """前端触发、Worker 消费的异步任务记录。"""

    __tablename__ = "generation_tasks"

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    chapter_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("chapters.id"), nullable=True)
    task_type: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    result_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
