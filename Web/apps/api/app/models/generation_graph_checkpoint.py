"""LangGraph 业务状态的持久化检查点。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class GenerationGraphCheckpoint(IdMixin, TimestampMixin, Base):
    __tablename__ = "generation_graph_checkpoints"
    __table_args__ = (
        UniqueConstraint("task_id", "graph_name", name="uq_generation_graph_checkpoint_task_graph"),
    )

    novel_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("novels.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    graph_name: Mapped[str] = mapped_column(String(100), index=True)
    node_name: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
