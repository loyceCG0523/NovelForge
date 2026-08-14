"""章节表模型。"""

from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class Chapter(IdMixin, TimestampMixin, Base):
    """小说章节，保存正文、摘要、字数和生成上下文快照。"""

    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("novel_id", "chapter_index", name="uq_chapters_novel_index"),)

    novel_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("novels.id"), index=True)
    chapter_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    context_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
