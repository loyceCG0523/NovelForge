from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NovelCreate(BaseModel):
    title: str
    genre: str = ""
    target_words: int = 300000
    premise: str = ""
    brief: dict = Field(default_factory=dict)


class NovelUpdate(BaseModel):
    title: str | None = None
    genre: str | None = None
    status: str | None = None
    target_words: int | None = None
    premise: str | None = None
    brief: dict | None = None


class NovelRead(BaseModel):
    id: UUID
    owner_id: UUID
    title: str
    genre: str
    status: str
    target_words: int
    current_chapter_index: int
    premise: str
    brief: dict

    model_config = ConfigDict(from_attributes=True)
