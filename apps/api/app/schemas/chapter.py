"""章节接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChapterCreate(BaseModel):
    """创建章节时前端提交的数据。"""

    chapter_index: int
    title: str = ""
    status: str = "draft"
    summary: str = ""
    content: str = ""
    context_snapshot: dict = Field(default_factory=dict)


class ChapterUpdate(BaseModel):
    """更新章节时允许局部提交的字段。"""

    chapter_index: int | None = None
    title: str | None = None
    status: str | None = None
    summary: str | None = None
    content: str | None = None
    context_snapshot: dict | None = None


class ChapterRead(BaseModel):
    """返回给前端的章节完整视图。"""

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
