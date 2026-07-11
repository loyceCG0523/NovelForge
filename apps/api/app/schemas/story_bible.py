"""作品圣经接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StoryBibleUpdate(BaseModel):
    """用户手动调整作品圣经时允许修改的字段。"""

    status: str | None = None
    summary: str | None = None
    content: dict | None = None
    locked_fields: dict | None = None


class StoryBibleRead(BaseModel):
    """返回给前端的作品圣经视图。"""

    id: UUID
    novel_id: UUID
    status: str
    version: int
    source: str
    summary: str
    content: dict
    locked_fields: dict

    model_config = ConfigDict(from_attributes=True)


class StoryBibleGenerateRequest(BaseModel):
    """触发作品圣经 Agent 时提交的附加要求。"""

    input_payload: dict = Field(default_factory=dict)
