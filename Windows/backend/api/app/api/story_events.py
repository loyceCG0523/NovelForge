"""剧情事件接口。

剧情事件是一组围绕同一大事件生成的连续章节。本模块负责给前端提供事件详情、
章节计划看板，以及局部重跑/从某章继续生成的任务入口。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.auto_novel_run import AutoNovelRun
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.story_event import StoryEvent
from app.schemas.task import AgentRunRequest, GenerationTaskRead
from app.services.agent_orchestrator import enqueue_agent_task


router = APIRouter(prefix="/api/novels/{novel_id}/story-events", tags=["story-events"])


class EventChapterPlanPatch(BaseModel):
    """用户在 human-in-loop 模式下可编辑的章节计划字段。"""

    id: UUID
    title: str | None = None
    function: str | None = None
    core_event: str | None = None
    ending_hook: str | None = None


class StoryEventPlanPatch(BaseModel):
    """用户确认前可编辑的事件大纲和章节计划。"""

    title: str | None = None
    goal: str | None = None
    core_conflict: str | None = None
    next_event_hook: str | None = None
    completion_criteria: list[str] | None = None
    chapter_plans: list[EventChapterPlanPatch] = Field(default_factory=list)


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


def _latest_auto_run(db: Session, novel: Novel) -> AutoNovelRun | None:
    """读取最近一次整本书生产记录。"""
    return db.scalar(
        select(AutoNovelRun)
        .where(AutoNovelRun.novel_id == novel.id)
        .order_by(AutoNovelRun.updated_at.desc())
        .limit(1)
    )


@router.get("/{event_id}")
def get_story_event(
    event_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> dict:
    """读取单个剧情事件的完整详情。"""
    return _event_to_dict(db, _get_story_event(db, novel, event_id))


@router.patch("/{event_id}/plan")
def update_story_event_plan(
    event_id: UUID,
    payload: StoryEventPlanPatch,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> dict:
    """保存用户在 human-in-loop 模式下编辑后的事件大纲和章节计划。"""
    story_event = _get_story_event(db, novel, event_id)
    if story_event.status not in {"planned", "generating", "completed"}:
        raise HTTPException(status_code=400, detail="Story event plan cannot be edited in current status")

    if payload.title is not None:
        story_event.title = payload.title
    if payload.goal is not None:
        story_event.goal = payload.goal
    if payload.core_conflict is not None:
        story_event.core_conflict = payload.core_conflict
    if payload.next_event_hook is not None:
        story_event.next_event_hook = payload.next_event_hook

    event_payload = story_event.payload or {}
    if payload.title is not None:
        event_payload["event_title"] = payload.title
    if payload.goal is not None:
        event_payload["event_goal"] = payload.goal
    if payload.core_conflict is not None:
        event_payload["core_conflict"] = payload.core_conflict
    if payload.next_event_hook is not None:
        event_payload["next_event_hook"] = payload.next_event_hook
    if payload.completion_criteria is not None:
        event_payload["completion_criteria"] = payload.completion_criteria

    existing_plans = {
        str(plan.id): plan
        for plan in db.scalars(
            select(EventChapterPlan).where(EventChapterPlan.story_event_id == story_event.id)
        ).all()
    }
    chapter_plans_payload = event_payload.get("chapter_plans")
    if not isinstance(chapter_plans_payload, list):
        chapter_plans_payload = []
    chapter_plans_by_index = {
        int(item.get("chapter_index")): item
        for item in chapter_plans_payload
        if isinstance(item, dict) and item.get("chapter_index") is not None
    }

    for patch in payload.chapter_plans:
        plan = existing_plans.get(str(patch.id))
        if plan is None:
            continue
        if patch.title is not None:
            plan.title = patch.title
        if patch.function is not None:
            plan.function = patch.function
        if patch.core_event is not None:
            plan.core_event = patch.core_event
        if patch.ending_hook is not None:
            plan.ending_hook = patch.ending_hook
        plan_payload = {
            **(plan.payload or {}),
            "chapter_index": plan.chapter_index,
            "title": plan.title,
            "function": plan.function,
            "core_event": plan.core_event,
            "ending_hook": plan.ending_hook,
            "edited_by_user": True,
        }
        plan.payload = plan_payload
        if plan.chapter_index in chapter_plans_by_index:
            chapter_plans_by_index[plan.chapter_index].update(plan_payload)

    event_payload["chapter_plans"] = [
        chapter_plans_by_index.get(plan.chapter_index, plan.payload or {})
        for plan in db.scalars(
            select(EventChapterPlan)
            .where(EventChapterPlan.story_event_id == story_event.id)
            .order_by(EventChapterPlan.chapter_index.asc())
        ).all()
    ]
    event_payload["human_review"] = {
        **(event_payload.get("human_review") if isinstance(event_payload.get("human_review"), dict) else {}),
        "status": "edited",
    }
    story_event.payload = event_payload
    db.commit()
    db.refresh(story_event)
    return _event_to_dict(db, story_event)


@router.post("/{event_id}/confirm", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def confirm_story_event_plan(
    event_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """确认事件章节计划，并批量生成该事件的所有未生成章节。"""
    story_event = _get_story_event(db, novel, event_id)
    first_pending = db.scalar(
        select(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id, EventChapterPlan.status == "planned")
        .order_by(EventChapterPlan.chapter_index.asc())
        .limit(1)
    )
    if first_pending is None:
        raise HTTPException(status_code=400, detail="Story event has no pending chapter plan to confirm")

    auto_run = _latest_auto_run(db, novel)
    auto_run_id = ""
    production_pacing = {}
    if auto_run is not None and (
        auto_run.current_event_id == story_event.id
        or (auto_run.payload or {}).get("pending_human_event_id") == str(story_event.id)
    ):
        auto_run_id = str(auto_run.id)
        production_pacing = {
            "pacing_plan": (auto_run.payload or {}).get("pacing_plan"),
            "pacing_state": (auto_run.payload or {}).get("pacing_state"),
            "production_mode": (auto_run.payload or {}).get("production_mode", "human_in_loop"),
        }
        auto_run.status = "running"
        auto_run.stage = "event_confirmed_generating"
        auto_run.payload = {
            **(auto_run.payload or {}),
            "human_loop_status": "event_generation_running",
            "pause_requested": False,
        }

    story_event.payload = {
        **(story_event.payload or {}),
        "human_review": {
            **((story_event.payload or {}).get("human_review") if isinstance((story_event.payload or {}).get("human_review"), dict) else {}),
            "status": "confirmed",
        },
    }
    db.commit()

    return enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="continue_story_event",
            input_payload={
                "source": "human_loop_confirm_event",
                "story_event_id": str(story_event.id),
                "from_chapter_index": first_pending.chapter_index,
                "auto_run_id": auto_run_id,
                "production_pacing": production_pacing,
            },
        ),
    )


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
    if story_event.status not in {"planned", "generating", "reviewing", "paused", "completed", "failed"}:
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
                "quality_only": bool(
                    story_event.status == "paused"
                    and story_event.remaining_open_risks
                    and first_pending is None
                ),
            },
        ),
    )
