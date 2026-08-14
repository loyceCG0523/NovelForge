"""网络研究资料接口的数据契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ResearchSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=400)
    max_results: int = Field(default=5, ge=1, le=10)
    search_depth: str = Field(default="basic", pattern="^(basic|advanced)$")


class ResearchSourceRead(BaseModel):
    id: UUID
    novel_id: UUID
    query: str
    provider: str
    title: str
    url: str
    domain: str
    snippet: str
    score: float
    published_at: str
    payload: dict

    model_config = ConfigDict(from_attributes=True)
