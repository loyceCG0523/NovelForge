"""伏笔接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ForeshadowingCreate(BaseModel):
    """创建伏笔记录时提交的数据。"""

    title: str
    status: str = "planted"
    planted_chapter_index: int | None = None
    target_chapter_index: int | None = None
    resolved_chapter_index: int | None = None
    description: str = ""
    payload: dict = Field(default_factory=dict)


class ForeshadowingRead(BaseModel):
    """返回给前端的伏笔记录。"""

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
