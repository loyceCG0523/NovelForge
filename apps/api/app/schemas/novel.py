"""作品接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NovelCreate(BaseModel):
    """创建作品项目时提交的基础信息和起始需求。"""

    title: str
    genre: str = ""
    target_words: int = 300000
    premise: str = ""
    brief: dict = Field(default_factory=dict)


class NovelUpdate(BaseModel):
    """更新作品项目时允许局部修改的字段。"""

    title: str | None = None
    genre: str | None = None
    status: str | None = None
    target_words: int | None = None
    premise: str | None = None
    brief: dict | None = None


class NovelRead(BaseModel):
    """返回给前端的作品项目视图。"""

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
