"""StoryPlanningAgent：负责作品圣经和剧情事件规划的上层门面。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.novel import Novel
from app.models.story_bible import StoryBible
from app.services.llm_client import LLMConfig
from app.services.story_bible_builder import (
    generate_story_bible_content,
    get_or_build_story_bible_context,
    upsert_story_bible,
)


AGENT_NAME = "StoryPlanningAgent"


def build_or_refresh_story_bible(
    db: Session,
    novel: Novel,
    llm_config: LLMConfig | None,
    extra_input: dict[str, Any] | None = None,
) -> tuple[StoryBible, str]:
    """生成或刷新作品圣经，并返回生成模式。"""
    content, generation_mode = generate_story_bible_content(
        novel=novel,
        llm_config=llm_config,
        extra_input=extra_input,
    )
    story_bible = upsert_story_bible(
        db=db,
        novel=novel,
        content=content,
        source=generation_mode,
    )
    return story_bible, generation_mode


def get_story_bible_context(db: Session, novel: Novel) -> dict[str, Any]:
    """读取剧情规划和章节生成共用的作品圣经上下文。"""
    return get_or_build_story_bible_context(db, novel)
