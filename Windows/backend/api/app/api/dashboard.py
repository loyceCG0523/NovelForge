"""工作台聚合接口。

前端工作台需要同时展示作品概况、章节进度、系统审校记录和后台任务状态。
这些数据分散在多张表里，所以统一在这里做轻量聚合，避免前端发起过多请求。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.auto_novel_run import AutoNovelRun
from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.foreshadowing import Foreshadowing
from app.models.generation_task import GenerationTask
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.story_event import StoryEvent
from app.schemas.dashboard import NovelDashboardRead


router = APIRouter(prefix="/api/novels/{novel_id}/dashboard", tags=["dashboard"])


def _build_story_event_summary(db: Session, story_event: StoryEvent | None) -> dict | None:
    """从正式 StoryEvent 表提取工作台展示用的剧情事件摘要。"""
    if story_event is None:
        return None

    task = db.get(GenerationTask, story_event.task_id) if story_event.task_id else None
    task_payload = task.result_payload if task else {}
    chapter_plan_records = db.scalars(
        select(EventChapterPlan)
        .where(EventChapterPlan.story_event_id == story_event.id)
        .order_by(EventChapterPlan.chapter_index.asc())
    ).all()
    chapter_plans = [
        {
            "id": str(plan.id),
            "chapter_id": str(plan.chapter_id) if plan.chapter_id else None,
            "chapter_index": plan.chapter_index,
            "title": plan.title,
            "function": plan.function,
            "core_event": plan.core_event,
            "ending_hook": plan.ending_hook,
            "status": plan.status,
            "payload": plan.payload,
        }
        for plan in chapter_plan_records
    ]
    generated_chapters = [
        {
            "chapter_id": str(plan.chapter_id),
            "chapter_index": plan.chapter_index,
            "title": plan.title,
            "word_count": (plan.payload or {}).get("generated_word_count", 0),
            "status": plan.status,
        }
        for plan in chapter_plan_records
        if plan.chapter_id is not None
    ]

    return {
        "id": str(story_event.id),
        "task_id": str(story_event.task_id) if story_event.task_id else "",
        "task_status": task.status if task else story_event.status,
        "progress": task.progress if task else 100,
        "graph_status": task_payload.get("graph_status", "") if task_payload else "",
        "event_title": story_event.title or "剧情事件生成中",
        "event_goal": story_event.goal,
        "core_conflict": story_event.core_conflict,
        "next_event_hook": story_event.next_event_hook,
        "chapter_range": {
            "start": story_event.start_chapter_index,
            "end": story_event.end_chapter_index,
        },
        "planned_chapter_count": story_event.planned_chapter_count,
        "generated_chapter_count": story_event.generated_chapter_count,
        "auto_repair_count": story_event.auto_repair_count,
        "remaining_open_risks": story_event.remaining_open_risks,
        "completion_criteria": (story_event.payload or {}).get("completion_criteria", []),
        "chapter_plans": chapter_plans,
        "generated_chapters": generated_chapters,
    }


def _build_auto_run_summary(db: Session, auto_run: AutoNovelRun | None) -> dict | None:
    """从 AutoNovelRun 提取工作台展示用的整本书生产状态。"""
    if auto_run is None:
        return None

    task = db.get(GenerationTask, auto_run.task_id) if auto_run.task_id else None
    progress = 0
    if auto_run.target_words:
        progress = min(100, round((auto_run.current_words / auto_run.target_words) * 100))
    return {
        "id": str(auto_run.id),
        "task_id": str(auto_run.task_id) if auto_run.task_id else "",
        "task_status": task.status if task else "",
        "task_progress": task.progress if task else progress,
        "status": auto_run.status,
        "stage": auto_run.stage,
        "target_words": auto_run.target_words,
        "current_words": auto_run.current_words,
        "word_progress": progress,
        "produced_event_count": auto_run.produced_event_count,
        "max_event_count": auto_run.max_event_count,
        "current_event_id": str(auto_run.current_event_id) if auto_run.current_event_id else "",
        "last_error": auto_run.last_error,
        "payload": auto_run.payload or {},
    }


@router.get("", response_model=NovelDashboardRead)
def get_novel_dashboard(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> NovelDashboardRead:
    """返回作品工作台所需的汇总数据。"""
    counts = {
        "chapters": db.scalar(select(func.count()).select_from(Chapter).where(Chapter.novel_id == novel.id)),
        "words": db.scalar(select(func.coalesce(func.sum(Chapter.word_count), 0)).where(Chapter.novel_id == novel.id)),
        "memory_items": db.scalar(select(func.count()).select_from(MemoryItem).where(MemoryItem.novel_id == novel.id)),
        "foreshadowing": db.scalar(select(func.count()).select_from(Foreshadowing).where(Foreshadowing.novel_id == novel.id)),
        "open_review_issues": db.scalar(
            select(func.count())
            .select_from(ReviewIssue)
            .where(ReviewIssue.novel_id == novel.id, ReviewIssue.status == "open")
        ),
        "active_tasks": db.scalar(
            select(func.count())
            .select_from(GenerationTask)
            .where(
                GenerationTask.novel_id == novel.id,
                GenerationTask.status.in_(["queued", "running", "waiting"]),
            )
        ),
    }

    latest_tasks = db.scalars(
        select(GenerationTask)
        .where(GenerationTask.novel_id == novel.id)
        .order_by(GenerationTask.updated_at.desc())
        .limit(5)
    ).all()
    current_auto_run = db.scalar(
        select(AutoNovelRun)
        .where(AutoNovelRun.novel_id == novel.id)
        .order_by(AutoNovelRun.updated_at.desc())
        .limit(1)
    )
    current_story_event = db.scalar(
        select(StoryEvent)
        .where(
            StoryEvent.novel_id == novel.id,
            StoryEvent.status.in_(["planned", "generating", "reviewing", "paused"]),
        )
        .order_by(StoryEvent.updated_at.desc())
        .limit(1)
    )
    if current_story_event is None:
        current_story_event = db.scalar(
            select(StoryEvent)
            .where(
                StoryEvent.novel_id == novel.id,
                StoryEvent.status.in_(["completed", "failed"]),
            )
            .order_by(StoryEvent.updated_at.desc())
            .limit(1)
        )
    open_issues = db.scalars(
        select(ReviewIssue)
        .where(ReviewIssue.novel_id == novel.id, ReviewIssue.status == "open")
        .order_by(ReviewIssue.created_at.desc())
        .limit(5)
    ).all()
    recent_issues = db.scalars(
        select(ReviewIssue)
        .where(ReviewIssue.novel_id == novel.id)
        .order_by(ReviewIssue.updated_at.desc())
        .limit(8)
    ).all()
    latest_chapters = db.scalars(
        select(Chapter)
        .where(Chapter.novel_id == novel.id)
        .order_by(Chapter.chapter_index.desc())
        .limit(5)
    ).all()

    return NovelDashboardRead(
        novel={
            "id": str(novel.id),
            "title": novel.title,
            "genre": novel.genre,
            "status": novel.status,
            "target_words": novel.target_words,
            "current_chapter_index": novel.current_chapter_index,
            "premise": novel.premise,
            "brief": novel.brief,
        },
        counts=counts,
        latest_tasks=[
            {
                "id": str(task.id),
                "task_type": task.task_type,
                "status": task.status,
                "progress": task.progress,
                "graph_status": (task.result_payload or {}).get("graph_status", ""),
                "error_message": task.error_message or "",
            }
            for task in latest_tasks
        ],
        open_review_issues=[
            {
                "id": str(issue.id),
                "chapter_id": str(issue.chapter_id) if issue.chapter_id else None,
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "status": issue.status,
                "message": issue.message,
                "payload": issue.payload,
            }
            for issue in open_issues
        ],
        review_issues=[
            {
                "id": str(issue.id),
                "chapter_id": str(issue.chapter_id) if issue.chapter_id else None,
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "status": issue.status,
                "message": issue.message,
                "payload": issue.payload,
            }
            for issue in recent_issues
        ],
        latest_chapters=[
            {
                "id": str(chapter.id),
                "chapter_index": chapter.chapter_index,
                "title": chapter.title,
                "status": chapter.status,
                "word_count": chapter.word_count,
                "summary": chapter.summary,
            }
            for chapter in latest_chapters
        ],
        current_story_event=_build_story_event_summary(db, current_story_event),
        current_auto_run=_build_auto_run_summary(db, current_auto_run),
    )
