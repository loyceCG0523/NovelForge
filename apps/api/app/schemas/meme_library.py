"""热梗知识库接口契约。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemeEntryRead(BaseModel):
    id: UUID
    source_type: str
    phrase: str
    meaning: str
    origin_event: str
    suitable_scenes: str
    popularity_period: str
    source_urls: list[str]
    enabled: bool
    review_status: str
    embedding_model: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MemeEntryCreate(BaseModel):
    phrase: str = Field(min_length=1, max_length=120)
    meaning: str = Field(min_length=1, max_length=1000)
    origin_event: str = Field(min_length=1, max_length=1600)
    suitable_scenes: str = Field(min_length=1, max_length=1200)
    popularity_period: str = Field(min_length=1, max_length=80)
    source_urls: list[str] = Field(min_length=1, max_length=5)
    enabled: bool = True


class MemeEntryUpdate(BaseModel):
    phrase: str | None = Field(default=None, min_length=1, max_length=120)
    meaning: str | None = Field(default=None, min_length=1, max_length=1000)
    origin_event: str | None = Field(default=None, min_length=1, max_length=1600)
    suitable_scenes: str | None = Field(default=None, min_length=1, max_length=1200)
    popularity_period: str | None = Field(default=None, min_length=1, max_length=80)
    source_urls: list[str] | None = Field(default=None, min_length=1, max_length=5)
    enabled: bool | None = None


class MemeImportResult(BaseModel):
    imported: int
    updated: int
    skipped: int
    total: int
    indexed: int
    index_status: str
    errors: list[str]


class MemeIndexResult(BaseModel):
    status: str
    indexed: int
    total_visible: int
    embedding_model: str
    message: str
