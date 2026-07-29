"""生成任务接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


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


class GenerationTaskEventRead(BaseModel):
    id: UUID
    task_id: UUID
    sequence_no: int
    event_type: str
    step_key: str
    status: str
    title: str
    message: str
    progress: int
    chapter_id: UUID | None
    chapter_index: int | None
    payload: dict
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChapterRevisionPatchRead(BaseModel):
    id: UUID
    task_id: UUID | None
    story_event_id: UUID | None
    review_issue_id: UUID | None
    chapter_id: UUID
    chapter_index: int
    paragraph_index: int
    paragraph_id: str
    old_text: str
    new_text: str
    reason: str
    problem: str = ""
    suggestion: str = ""
    issue_type: str = ""
    severity: str = ""
    status: str
    validation: dict
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
