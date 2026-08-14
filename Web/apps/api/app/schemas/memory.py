"""结构化记忆接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryItemCreate(BaseModel):
    """创建记忆条目时提交的数据。"""

    memory_type: str
    entity_name: str
    chapter_index_start: int | None = None
    chapter_index_end: int | None = None
    payload: dict = Field(default_factory=dict)


class MemoryItemRead(BaseModel):
    """返回给前端或上下文构建器的记忆条目。"""

    id: UUID
    novel_id: UUID
    memory_type: str
    entity_name: str
    chapter_index_start: int | None
    chapter_index_end: int | None
    payload: dict

    model_config = ConfigDict(from_attributes=True)
