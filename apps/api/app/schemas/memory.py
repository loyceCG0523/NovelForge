from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryItemCreate(BaseModel):
    memory_type: str
    entity_name: str
    chapter_index_start: int | None = None
    chapter_index_end: int | None = None
    payload: dict = Field(default_factory=dict)


class MemoryItemRead(BaseModel):
    id: UUID
    novel_id: UUID
    memory_type: str
    entity_name: str
    chapter_index_start: int | None
    chapter_index_end: int | None
    payload: dict

    model_config = ConfigDict(from_attributes=True)
