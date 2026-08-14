"""整本书自动生产接口的数据契约。"""

from uuid import UUID
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AutoNovelRunStart(BaseModel):
    """启动整本书自动生产时可覆盖的运行参数。"""

    chapter_count_per_event: int | None = Field(default=None, ge=4, le=12)
    max_event_count: int = Field(default=20, ge=1, le=80)
    production_mode: Literal["auto", "human_in_loop", "tomato_trial", "test_run"] = "auto"
    test_run_scope: Literal["event", "first_chapter"] = "event"


class AutoNovelRunRead(BaseModel):
    """返回给前端的整本书生产状态。"""

    id: UUID
    novel_id: UUID
    task_id: UUID | None
    current_event_id: UUID | None
    status: str
    stage: str
    target_words: int
    current_words: int
    produced_event_count: int
    max_event_count: int
    last_error: str
    payload: dict

    model_config = ConfigDict(from_attributes=True)
