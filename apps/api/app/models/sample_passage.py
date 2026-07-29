"""样本作品中可检索的剧情与表达经验卡。"""

from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class SamplePassage(IdMixin, TimestampMixin, Base):
    __tablename__ = "sample_passages"
    __table_args__ = (
        UniqueConstraint(
            "sample_analysis_id",
            "content_hash",
            name="uq_sample_passages_analysis_content_hash",
        ),
    )

    owner_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    sample_analysis_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sample_analyses.id", ondelete="CASCADE"),
        index=True,
    )
    chapter_index: Mapped[int] = mapped_column(Integer, default=0, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    passage_index: Mapped[int] = mapped_column(Integer, default=0)
    passage_type: Mapped[str] = mapped_column(String(50), default="narration", index=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    technique_summary: Mapped[str] = mapped_column(Text, default="")
    metadata_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    quality_score: Mapped[float] = mapped_column(Float, default=0, index=True)
    embedding_model: Mapped[str] = mapped_column(String(200), default="")
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dimensions),
    )
