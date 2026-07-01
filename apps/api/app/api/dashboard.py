"""工作台聚合接口。

前端工作台需要同时展示作品概况、章节进度、风险提醒和后台任务状态。
这些数据分散在多张表里，所以统一在这里做轻量聚合，避免前端发起过多请求。
"""

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
    )
