from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class MemoryItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "memory_items"

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    memory_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_name: Mapped[str] = mapped_column(String(120), index=True)
    chapter_index_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chapter_index_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
