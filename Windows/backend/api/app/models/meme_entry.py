"""系统内置与用户扩展的热梗知识条目。"""

from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.db.base import Base
from app.models.mixins import IdMixin, TimestampMixin


class MemeEntry(IdMixin, TimestampMixin, Base):
    __tablename__ = "meme_entries"
    __table_args__ = (
        UniqueConstraint(
            "namespace",
            "normalized_phrase",
            name="uq_meme_entries_namespace_phrase",
        ),
        Index(
            "ix_meme_entries_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "ix_meme_entries_retrieval_trgm",
            "retrieval_text",
            postgresql_using="gin",
            postgresql_ops={"retrieval_text": "gin_trgm_ops"},
        ),
    )

    owner_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    namespace: Mapped[str] = mapped_column(String(100), index=True)
    source_type: Mapped[str] = mapped_column(String(20), index=True)
    phrase: Mapped[str] = mapped_column(String(120))
    normalized_phrase: Mapped[str] = mapped_column(String(120))
    meaning: Mapped[str] = mapped_column(Text)
    suitable_scenes: Mapped[str] = mapped_column(Text)
    retrieval_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    review_status: Mapped[str] = mapped_column(String(20), default="approved", index=True)
    library_version: Mapped[str] = mapped_column(String(80), default="")
    embedding_model: Mapped[str] = mapped_column(String(200), default="", index=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dimensions),
        nullable=True,
    )
    metadata_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
