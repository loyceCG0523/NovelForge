"""QualityAgent：统一负责审校、自动修复和质量记录。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.story_event import StoryEvent
from app.services.auto_review_handler import auto_handle_review_issues
from app.services.continuity_checker import sync_continuity_issues
from app.services.event_quality_checker import sync_event_quality_issues
from app.services.factual_consistency_checker import sync_factual_consistency_issues
from app.services.llm_client import LLMConfig
from app.services.reasonability_checker import sync_reasonability_issues
from app.services.revision_service import revise_chapter_with_llm


AGENT_NAME = "QualityAgent"


def review_chapter_quality(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
    task_id: str,
    source: str,
    max_attempts: int = 2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """执行单章质量审校，并自动尝试修复。"""
    review = {"issues": 0, "continuity_issues": 0, "fact_issues": 0, "reasonability_issues": 0, "error": ""}
    auto_review = {"attempted": 0, "resolved": 0, "skipped": 0, "remaining_open": 0, "history": []}
    try:
        issues = sync_continuity_issues(
            db=db,
            novel=novel,
            chapter=chapter,
            context=context,
            llm_config=llm_config,
        )
        fact_issues = sync_factual_consistency_issues(db=db, novel=novel, chapter=chapter)
        reasonability_issues = sync_reasonability_issues(
            db=db,
            novel=novel,
            chapter=chapter,
            context=context,
            llm_config=llm_config,
        )
        review["continuity_issues"] = len(issues)
        review["fact_issues"] = len(fact_issues)
        review["reasonability_issues"] = len(reasonability_issues)
        review["issues"] = review["continuity_issues"] + review["fact_issues"] + review["reasonability_issues"]
        auto_review = auto_handle_review_issues(
            db=db,
            novel=novel,
            chapter=chapter,
            context=context,
            llm_config=llm_config,
            task_id=task_id,
            source=source,
            max_attempts=max_attempts,
        )
    except Exception as exc:
        db.rollback()
        review["error"] = str(exc)
    return review, auto_review


def review_chapter_continuity_only(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
) -> dict[str, Any]:
    """只重新审校章节，不触发二次修订。"""
    review = {"issues": 0, "continuity_issues": 0, "fact_issues": 0, "reasonability_issues": 0, "error": ""}
    try:
        issues = sync_continuity_issues(
            db=db,
            novel=novel,
            chapter=chapter,
            context=context,
            llm_config=llm_config,
        )
        fact_issues = sync_factual_consistency_issues(db=db, novel=novel, chapter=chapter)
        reasonability_issues = sync_reasonability_issues(
            db=db,
            novel=novel,
            chapter=chapter,
            context=context,
            llm_config=llm_config,
        )
        review["continuity_issues"] = len(issues)
        review["fact_issues"] = len(fact_issues)
        review["reasonability_issues"] = len(reasonability_issues)
        review["issues"] = review["continuity_issues"] + review["fact_issues"] + review["reasonability_issues"]
    except Exception as exc:
        db.rollback()
        review["error"] = str(exc)
    return review


def revise_chapter_with_quality_agent(
    novel: Novel,
    chapter: Chapter,
    issue: ReviewIssue,
    context: dict,
    llm_config: LLMConfig,
) -> dict[str, str]:
    """根据审校记录定向修订章节。"""
    return revise_chapter_with_llm(
        novel=novel,
        chapter=chapter,
        issue=issue,
        context=context,
        llm_config=llm_config,
    )


def review_story_event_quality(
    db: Session,
    novel: Novel,
    story_event: StoryEvent,
    llm_config: LLMConfig | None,
) -> dict[str, Any]:
    """执行事件级质量审校。"""
    return sync_event_quality_issues(db=db, novel=novel, story_event=story_event, llm_config=llm_config)
