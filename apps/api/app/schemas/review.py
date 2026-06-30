from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReviewIssueCreate(BaseModel):
    chapter_id: UUID | None = None
    issue_type: str
    severity: str = "medium"
    status: str = "open"
    message: str
    payload: dict = Field(default_factory=dict)


class ReviewIssueRead(BaseModel):
    id: UUID
    novel_id: UUID
    chapter_id: UUID | None
    issue_type: str
    severity: str
    status: str
    message: str
    payload: dict

    model_config = ConfigDict(from_attributes=True)
