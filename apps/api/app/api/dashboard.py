from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.foreshadowing import Foreshadowing
from app.models.generation_task import GenerationTask
from app.models.memory_item import MemoryItem
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.schemas.dashboard import NovelDashboardRead


router = APIRouter(prefix="/api/novels/{novel_id}/dashboard", tags=["dashboard"])


@router.get("", response_model=NovelDashboardRead)
def get_novel_dashboard(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> NovelDashboardRead:
    counts = {
        "chapters": db.scalar(select(func.count()).select_from(Chapter).where(Chapter.novel_id == novel.id)),
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
            .where(GenerationTask.novel_id == novel.id, GenerationTask.status.in_(["queued", "running"]))
        ),
    }

    latest_tasks = db.scalars(
        select(GenerationTask)
        .where(GenerationTask.novel_id == novel.id)
        .order_by(GenerationTask.created_at.desc())
        .limit(5)
    ).all()
    open_issues = db.scalars(
        select(ReviewIssue)
        .where(ReviewIssue.novel_id == novel.id, ReviewIssue.status == "open")
        .order_by(ReviewIssue.created_at.desc())
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
        },
        counts=counts,
        latest_tasks=[
            {
                "id": str(task.id),
                "task_type": task.task_type,
                "status": task.status,
                "progress": task.progress,
            }
            for task in latest_tasks
        ],
        open_review_issues=[
            {
                "id": str(issue.id),
                "issue_type": issue.issue_type,
                "severity": issue.severity,
                "message": issue.message,
            }
            for issue in open_issues
        ],
    )
