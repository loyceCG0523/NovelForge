"""作品级网络研究资料来源。"""

from uuid import UUID

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class ResearchSource(IdMixin, TimestampMixin, Base):
    """保存一次网络检索返回的、可追溯的来源摘要。"""

    __tablename__ = "research_sources"

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    query: Mapped[str] = mapped_column(String(500), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="tavily")
    title: Mapped[str] = mapped_column(String(500), default="")
    url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(String(255), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    published_at: Mapped[str] = mapped_column(String(80), default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
