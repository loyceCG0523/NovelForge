"""自动审校风险处理服务。

连续性审校发现风险后，系统不再默认等待用户点击修复，而是尝试调用 RevisionAgent
自动修订章节，并把处理历史写回 ReviewIssue.payload，供前端展示。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.services.chapter_context_builder import build_chapter_context
from app.services.continuity_checker import AUTO_REVIEW_SOURCE as CONTINUITY_REVIEW_SOURCE
from app.services.continuity_checker import sync_continuity_issues
from app.services.factual_consistency_checker import AUTO_REVIEW_SOURCE as FACTUAL_REVIEW_SOURCE
from app.services.factual_consistency_checker import sync_factual_consistency_issues
from app.services.llm_client import LLMConfig
from app.services.reasonability_checker import AUTO_REVIEW_SOURCE as REASONABILITY_REVIEW_SOURCE
from app.services.reasonability_checker import sync_reasonability_issues
from app.services.revision_service import revise_chapter_with_llm


SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}
SYSTEM_DEFERRED_STATUS = "system_deferred"
AUTO_REPAIRABLE_SOURCES = {
    CONTINUITY_REVIEW_SOURCE,
    FACTUAL_REVIEW_SOURCE,
    REASONABILITY_REVIEW_SOURCE,
}
AUTO_REPAIRABLE_ISSUE_TYPES = {
    "behavior_reasonability",
    "count_mismatch",
    "local_fact_consistency",
    "timeline_conflict",
}


def _rank_issue(issue: ReviewIssue) -> tuple[int, int]:
    """按严重程度和创建时间排序，优先处理高风险。"""
    return (SEVERITY_RANK.get(issue.severity, 0), int(issue.created_at.timestamp()))


def _append_repair_history(payload: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """追加自动修复历史，避免覆盖前一次处理记录。"""
    history = payload.get("repair_history")
    if not isinstance(history, list):
        history = []
    return {
        **payload,
        "auto_repair": entry,
        "repair_history": [*history, entry],
    }


def _mark_remaining_issues_skipped(
    db: Session,
    issues: list[ReviewIssue],
    source: str,
    task_id: str,
    reason: str,
    message: str,
) -> None:
    """把暂未修复的问题转为系统后续处理项，避免变成用户待办。"""
    changed = False
    for issue in issues:
        payload = issue.payload or {}
        auto_repair = payload.get("auto_repair")
        if issue.status == SYSTEM_DEFERRED_STATUS and isinstance(auto_repair, dict) and auto_repair.get("reason") == reason:
            continue
        issue.status = SYSTEM_DEFERRED_STATUS
        issue.payload = _append_repair_history(
            payload,
            {
                "status": "deferred",
                "reason": reason,
                "source": source,
                "task_id": task_id,
                "message": message,
            },
        )
        changed = True
    if changed:
        db.commit()


def _load_open_auto_issues(db: Session, novel: Novel, chapter: Chapter) -> list[ReviewIssue]:
    """读取当前章节仍需处理的自动审校问题。"""
    issues = db.scalars(
        select(ReviewIssue).where(
            ReviewIssue.novel_id == novel.id,
            ReviewIssue.chapter_id == chapter.id,
            ReviewIssue.status == "open",
        )
    ).all()
    return sorted(
        [
            issue
            for issue in issues
            if (issue.payload or {}).get("source") in AUTO_REPAIRABLE_SOURCES
            and (issue.issue_type.startswith("continuity_") or issue.issue_type in AUTO_REPAIRABLE_ISSUE_TYPES)
        ],
        key=_rank_issue,
        reverse=True,
    )


def _sync_all_chapter_quality_issues(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
) -> None:
    """修订后重新同步所有章节级自动审校问题。"""
    sync_continuity_issues(db=db, novel=novel, chapter=chapter, context=context, llm_config=llm_config)
    sync_factual_consistency_issues(db=db, novel=novel, chapter=chapter)
    sync_reasonability_issues(db=db, novel=novel, chapter=chapter, context=context, llm_config=llm_config)


def auto_handle_review_issues(
    db: Session,
    novel: Novel,
    chapter: Chapter,
    context: dict,
    llm_config: LLMConfig | None,
    task_id: str,
    source: str,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """自动处理章节审校风险。

    每次选择一个最高优先级风险定向修订，然后重新审校。为了避免反复重写同一章，
    单章默认最多自动修订两次；仍未修好的问题会转为系统后续处理项，
    不再作为需要用户接手的事项。
    """
    result: dict[str, Any] = {
        "attempted": 0,
        "resolved": 0,
        "skipped": 0,
        "remaining_open": 0,
        "history": [],
    }

    open_issues = _load_open_auto_issues(db, novel, chapter)
    if not open_issues:
        return result

    if llm_config is None:
        for issue in open_issues:
            issue.status = SYSTEM_DEFERRED_STATUS
            issue.payload = _append_repair_history(
                issue.payload or {},
                {
                    "status": "deferred",
                    "reason": "missing_llm_config",
                    "source": source,
                    "task_id": task_id,
                    "message": "未配置 LLM API Key，系统已记录为后续自动处理项。",
                },
            )
            result["skipped"] += 1
        db.commit()
        result["remaining_open"] = 0
        return result

    current_context = context
    for attempt_index in range(max_attempts):
        open_issues = _load_open_auto_issues(db, novel, chapter)
        if not open_issues:
            break

        issue = open_issues[0]
        issue_id = issue.id
        result["attempted"] += 1
        try:
            revision = revise_chapter_with_llm(
                novel=novel,
                chapter=chapter,
                issue=issue,
                context=current_context,
                llm_config=llm_config,
            )
            chapter.title = revision["title"]
            chapter.summary = revision["summary"]
            chapter.content = revision["content"]
            chapter.word_count = len(revision["content"])
            chapter.status = "done"
            chapter.context_snapshot = current_context

            repair_entry = {
                "status": "resolved",
                "source": source,
                "task_id": task_id,
                "attempt": attempt_index + 1,
                "revision_note": revision["revision_note"],
            }
            issue.status = "resolved"
            issue.payload = _append_repair_history(issue.payload or {}, repair_entry)
            db.commit()
            db.refresh(chapter)
            db.refresh(issue)

            result["resolved"] += 1
            result["history"].append(
                {
                    "issue_id": str(issue.id),
                    "chapter_id": str(chapter.id),
                    "status": "resolved",
                    "revision_note": revision["revision_note"],
                }
            )

            current_context = build_chapter_context(
                db=db,
                novel=novel,
                target_chapter_index=chapter.chapter_index,
                task_input={
                    "source": f"{source}_post_auto_repair",
                    "auto_repair_issue_id": str(issue.id),
                    "auto_repair_attempt": attempt_index + 1,
                },
            )
            _sync_all_chapter_quality_issues(
                db=db,
                novel=novel,
                chapter=chapter,
                context=current_context,
                llm_config=llm_config,
            )
        except Exception as exc:
            db.rollback()
            issue = db.get(ReviewIssue, issue_id)
            if issue is not None:
                issue.payload = _append_repair_history(
                    issue.payload or {},
                    {
                        "status": "failed",
                        "source": source,
                        "task_id": task_id,
                        "attempt": attempt_index + 1,
                        "error": str(exc),
                    },
                )
                db.commit()
            result["history"].append(
                {
                    "issue_id": str(issue.id) if issue else "",
                    "chapter_id": str(chapter.id),
                    "status": "failed",
                    "error": str(exc),
                }
            )
            break

    remaining_issues = _load_open_auto_issues(db, novel, chapter)
    if remaining_issues:
        _mark_remaining_issues_skipped(
            db=db,
            issues=remaining_issues,
            source=source,
            task_id=task_id,
            reason="max_auto_repair_attempts_reached",
            message="已达到本章自动修复次数上限，系统已记录为后续自动处理项。",
        )
    result["remaining_open"] = 0
    return result
