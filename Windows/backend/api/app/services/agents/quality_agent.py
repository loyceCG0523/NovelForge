"""QualityAgent：统一负责审校、自动修复和质量记录。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.services.event_quality_checker import sync_event_quality_issues
from app.services.llm_client import LLMConfig


AGENT_NAME = "QualityAgent"


def review_story_event_quality(
    db: Session,
    novel: Novel,
    story_event: StoryEvent,
    llm_config: LLMConfig | None,
) -> dict:
    """执行事件级质量审校。"""
    return sync_event_quality_issues(db=db, novel=novel, story_event=story_event, llm_config=llm_config)
