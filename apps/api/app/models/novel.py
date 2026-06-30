from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class Novel(IdMixin, TimestampMixin, Base):
    __tablename__ = "novels"

    owner_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(120), index=True)
    genre: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="draft")
    target_words: Mapped[int] = mapped_column(Integer, default=300000)
    current_chapter_index: Mapped[int] = mapped_column(Integer, default=0)
    premise: Mapped[str] = mapped_column(Text, default="")
    brief: Mapped[dict] = mapped_column(JSONB, default=dict)
