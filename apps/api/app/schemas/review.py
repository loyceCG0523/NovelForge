"""审校问题接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReviewIssueCreate(BaseModel):
    """创建审校风险时提交的数据。"""

    chapter_id: UUID | None = None
    issue_type: str
    severity: str = "medium"
    status: str = "open"
    message: str
    payload: dict = Field(default_factory=dict)


class ReviewIssueRead(BaseModel):
    """返回给前端的审校风险记录。"""

    id: UUID
    novel_id: UUID
    chapter_id: UUID | None
    issue_type: str
    severity: str
    status: str
    message: str
    payload: dict

    model_config = ConfigDict(from_attributes=True)
