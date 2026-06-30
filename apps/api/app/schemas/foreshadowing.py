from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ForeshadowingCreate(BaseModel):
    title: str
    status: str = "planted"
    planted_chapter_index: int | None = None
    target_chapter_index: int | None = None
    resolved_chapter_index: int | None = None
    description: str = ""
    payload: dict = Field(default_factory=dict)


class ForeshadowingRead(BaseModel):
    id: UUID
    novel_id: UUID
    title: str
    status: str
    planted_chapter_index: int | None
    target_chapter_index: int | None
    resolved_chapter_index: int | None
    description: str
    payload: dict

    model_config = ConfigDict(from_attributes=True)
