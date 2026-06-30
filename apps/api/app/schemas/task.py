from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GenerationTaskCreate(BaseModel):
    chapter_id: UUID | None = None
    task_type: str
    result_payload: dict = Field(default_factory=dict)


class GenerationTaskRead(BaseModel):
    id: UUID
    novel_id: UUID
    chapter_id: UUID | None
    task_type: str
    status: str
    progress: int
    error_message: str
    result_payload: dict

    model_config = ConfigDict(from_attributes=True)


class AgentRunRequest(BaseModel):
    task_type: str = "generate_chapter"
    chapter_id: UUID | None = None
    input_payload: dict = Field(default_factory=dict)
