"""故事时间线接口契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TimelineEntryCreate(BaseModel):
    chapter_id: UUID | None = None
    story_event_id: UUID | None = None
    sequence_no: int = Field(ge=1)
    story_day: int | None = Field(default=None, ge=1)
    start_time: str = ""
    end_time: str = ""
    time_expression: str = ""
    era: str = ""
    location: str = ""
    summary: str
    participants: list[str] = Field(default_factory=list)
    certainty: str = "confirmed"
    source: str = "manual"
    payload: dict = Field(default_factory=dict)


class TimelineEntryUpdate(BaseModel):
    chapter_id: UUID | None = None
    story_event_id: UUID | None = None
    sequence_no: int | None = Field(default=None, ge=1)
    story_day: int | None = Field(default=None, ge=1)
    start_time: str | None = None
    end_time: str | None = None
    time_expression: str | None = None
    era: str | None = None
    location: str | None = None
    summary: str | None = None
    participants: list[str] | None = None
    certainty: str | None = None
    payload: dict | None = None


class TimelineEntryRead(BaseModel):
    id: UUID
    novel_id: UUID
    chapter_id: UUID | None
    story_event_id: UUID | None
    sequence_no: int
    story_day: int | None
    start_time: str
    end_time: str
    time_expression: str
    era: str
    location: str
    summary: str
    participants: list
    certainty: str
    source: str
    payload: dict

    model_config = ConfigDict(from_attributes=True)
