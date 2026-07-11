"""生成任务接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GenerationTaskCreate(BaseModel):
    """直接创建任务记录时提交的数据。"""

    chapter_id: UUID | None = None
    task_type: str
    result_payload: dict = Field(default_factory=dict)


class GenerationTaskRead(BaseModel):
    """返回给前端的任务状态与执行结果。"""

    id: UUID
    novel_id: UUID | None
    chapter_id: UUID | None
    task_type: str
    status: str
    progress: int
    error_message: str
    result_payload: dict

    model_config = ConfigDict(from_attributes=True)


class AgentRunRequest(BaseModel):
    """触发 Agent 任务时前端提交的数据。"""

    task_type: str = "generate_chapter"
    chapter_id: UUID | None = None
    input_payload: dict = Field(default_factory=dict)
