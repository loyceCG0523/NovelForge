from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChapterCreate(BaseModel):
    chapter_index: int
    title: str = ""
    summary: str = ""
    content: str = ""
    context_snapshot: dict = Field(default_factory=dict)


class ChapterRead(BaseModel):
    id: UUID
    novel_id: UUID
    chapter_index: int
    title: str
    status: str
    word_count: int
    summary: str
    content: str
    context_snapshot: dict

    model_config = ConfigDict(from_attributes=True)
