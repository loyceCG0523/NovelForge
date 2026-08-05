"""作品圣经模型。

StoryBible 是全书级设定中心，优先级高于单章记忆和事件规划。
它把起始需求文档整理成可长期复用的结构化约束，供章节生成、剧情事件规划和审校共同读取。
"""

from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class StoryBible(IdMixin, TimestampMixin, Base):
    """一部作品对应一份作品圣经。"""

    __tablename__ = "story_bibles"
    __table_args__ = (
        UniqueConstraint("novel_id", name="story_bibles_novel_id_key"),
        Index("ix_story_bibles_novel_id", "novel_id", unique=True),
    )

    novel_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("novels.id")
    )
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(80), default="manual")
    summary: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    locked_fields: Mapped[dict] = mapped_column(JSONB, default=dict)
