"""剧情事件接口。

剧情事件是一组围绕同一大事件生成的连续章节。本模块负责给前端提供事件详情、
章节计划看板，以及局部重跑/从某章继续生成的任务入口。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.story_event import StoryEvent
from app.schemas.task import AgentRunRequest, GenerationTaskRead
from app.services.agent_orchestrator import enqueue_agent_task


router = APIRouter(prefix="/api/novels/{novel_id}/story-events", tags=["story-events"])


def _get_story_event(db: Session, novel: Novel, event_id: UUID) -> StoryEvent:
    """读取并校验剧情事件归属。"""
    story_event = db.get(StoryEvent, event_id)
    if story_event is None or story_event.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Story event not found")
    return story_event


def _issue_to_dict(issue: ReviewIssue) -> dict:
    """把 ReviewIssue 转为前端直接可用的字典。"""
    return {
        "id": str(issue.id),
        "chapter_id": str(issue.chapter_id) if issue.chapter_id else None,
        "issue_type": issue.issue_type,
        "severity": issue.severity,
        "status": issue.status,
        "message": issue.message,
        "payload": issue.payload,
    }


def _plan_to_dict(plan: EventChapterPlan, issues_by_chapter: dict[UUID, list[ReviewIssue]]) -> dict:
    """把章节计划、生成状态和关联风险合并为看板卡片。"""
    issues = issues_by_chapter.get(plan.chapter_id, []) if plan.chapter_id else []
    system_pending_issues = [issue for issue in issues if issue.status in {"open", "system_deferred"}]
    return {
        "id": str(plan.id),
        "story_event_id": str(plan.story_event_id),
        "chapter_id": str(plan.chapter_id) if plan.chapter_id else None,
        "chapter_index": plan.chapter_index,
        "title": plan.title,
        "function": plan.function,
        "core_event": plan.core_event,
        "ending_hook": plan.ending_hook,
        "status": plan.status,
        "payload": plan.payload,
        "open_issue_count": len(system_pending_issues),
        "issues": [_issue_to_dict(issue) for issue in issues],
    }


def _event_to_dict(db: Session, story_event: StoryEvent, include_plans: bool = True) -> dict:
    """组装剧情事件详情，包含章节计划、完成标准和相关风险。"""
    task = db.get(GenerationTask, story_event.task_id) if story_event.task_id else None
    plans = []
    issues = []
    event_issues = []
    if include_plans:
        plans = db.scalars(
            select(EventChapterPlan)
            .where(EventChapterPlan.story_event_id == story_event.id)
            .order_by(EventChapterPlan.chapter_index.asc())
        ).all()
        chapter_ids = [plan.chapter_id for plan in plans if plan.chapter_id]
        if chapter_ids:
            issues = db.scalars(
                select(ReviewIssue)
                .where(ReviewIssue.novel_id == story_event.novel_id, ReviewIssue.chapter_id.in_(chapter_ids))
                .order_by(ReviewIssue.updated_at.desc())
            ).all()
        event_issues = db.scalars(
            select(ReviewIssue)
            .where(
                ReviewIssue.novel_id == story_event.novel_id,
                ReviewIssue.chapter_id.is_(None),
                ReviewIssue.issue_type.like("event_%"),
            )
            .order_by(ReviewIssue.updated_at.desc())
        ).all()
        event_issues = [
            issue
            for issue in event_issues
            if (issue.payload or {}).get("story_event_id") == str(story_event.id)
        ]

    issues_by_chapter: dict[UUID, list[ReviewIssue]] = {}
    for issue in issues:
        if issue.chapter_id:
            issues_by_chapter.setdefault(issue.chapter_id, []).append(issue)

    payload = story_event.payload or {}
    return {
        "id": str(story_event.id),
        "task_id": str(story_event.task_id) if story_event.task_id else "",
        "task_status": task.status if task else story_event.status,
        "task_progress": task.progress if task else 100,
        "graph_status": (task.result_payload or {}).get("graph_status", "") if task else "",
        "title": story_event.title,
        "goal": story_event.goal,
        "core_conflict": story_event.core_conflict,
        "status": story_event.status,
        "start_chapter_index": story_event.start_chapter_index,
        "end_chapter_index": story_event.end_chapter_index,
        "planned_chapter_count": story_event.planned_chapter_count,
        "generated_chapter_count": story_event.generated_chapter_count,
        "auto_repair_count": story_event.auto_repair_count,
        "remaining_open_risks": story_event.remaining_open_risks,
        "next_event_hook": story_event.next_event_hook,
        "completion_criteria": payload.get("completion_criteria", []),
        "quality_report": payload.get("quality_report", {}),
        "payload": payload,
        "plans": [_plan_to_dict(plan, issues_by_chapter) for plan in plans],
        "event_issues": [_issue_to_dict(issue) for issue in event_issues],
        "open_issues": [_issue_to_dict(issue) for issue in [*event_issues, *issues] if issue.status in {"open", "system_deferred"}],
    }


@router.get("")
def list_story_events(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[dict]:
    """列出当前作品的剧情事件，用于事件管理页左侧导航。"""
    story_events = db.scalars(
        select(StoryEvent)
        .where(StoryEvent.novel_id == novel.id)
        .order_by(StoryEvent.updated_at.desc())
    ).all()
    return [_event_to_dict(db, story_event, include_plans=False) for story_event in story_events]


@router.get("/{event_id}")
def get_story_event(
    event_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> dict:
    """读取单个剧情事件的完整详情。"""
    return _event_to_dict(db, _get_story_event(db, novel, event_id))


@router.post("/{event_id}/quality-check", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def check_story_event_quality(
    event_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """手动触发事件级质量审校。"""
    story_event = _get_story_event(db, novel, event_id)
    return enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="check_story_event_quality",
            input_payload={
                "source": "story_event_quality_check",
                "story_event_id": str(story_event.id),
            },
        ),
    )


@router.post("/{event_id}/plans/{plan_id}/rerun", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def rerun_event_chapter(
    event_id: UUID,
    plan_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """重新生成剧情事件中的某一章，不影响其他章节计划。"""
    story_event = _get_story_event(db, novel, event_id)
    plan = db.get(EventChapterPlan, plan_id)
    if plan is None or plan.story_event_id != story_event.id or plan.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Event chapter plan not found")

    return enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="generate_chapter",
            chapter_id=plan.chapter_id,
            input_payload={
                "source": "story_event_rerun",
                "story_event_id": str(story_event.id),
                "event_plan_id": str(plan.id),
                "chapter_index": plan.chapter_index,
                "force_regenerate": True,
                "story_event": story_event.payload or {},
                "chapter_plan": plan.payload or {
                    "chapter_index": plan.chapter_index,
                    "title": plan.title,
                    "function": plan.function,
                    "core_event": plan.core_event,
                    "ending_hook": plan.ending_hook,
                },
            },
        ),
    )


@router.post("/{event_id}/continue", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def continue_story_event(
    event_id: UUID,
    from_chapter_index: int | None = None,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """从指定章节继续生成当前剧情事件的剩余计划。"""
    story_event = _get_story_event(db, novel, event_id)
    if story_event.status not in {"planned", "generating", "completed", "failed"}:
        raise HTTPException(status_code=400, detail="Story event status does not support continuation")

    first_pending = db.scalar(
        select(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id, EventChapterPlan.status == "planned")
        .order_by(EventChapterPlan.chapter_index.asc())
        .limit(1)
    )
    start_index = from_chapter_index or (first_pending.chapter_index if first_pending else story_event.start_chapter_index)
    if start_index is None:
        raise HTTPException(status_code=400, detail="Story event has no chapter plan to continue")

    return enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="continue_story_event",
            input_payload={
                "source": "story_event_continue",
                "story_event_id": str(story_event.id),
                "from_chapter_index": start_index,
            },
        ),
    )
