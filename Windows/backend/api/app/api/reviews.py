"""审校问题接口。

ReviewIssue 用于记录连续性、伏笔遗漏、AI 句式、风格偏差等风险，并在工作台中实时展示。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.schemas.review import ReviewIssueCreate, ReviewIssueRead, ReviewIssueStatusUpdate


router = APIRouter(prefix="/api/novels/{novel_id}/reviews", tags=["reviews"])


@router.post("", response_model=ReviewIssueRead, status_code=status.HTTP_201_CREATED)
def create_review_issue(
    payload: ReviewIssueCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> ReviewIssue:
    """新增审校风险，可绑定到具体章节，也可作为作品级风险存在。"""
    if payload.chapter_id is not None:
        chapter = db.get(Chapter, payload.chapter_id)
        if chapter is None or chapter.novel_id != novel.id:
            raise HTTPException(status_code=404, detail="Chapter not found")

    issue = ReviewIssue(novel_id=novel.id, **payload.model_dump())
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


@router.get("", response_model=list[ReviewIssueRead])
def list_review_issues(
    status_filter: str | None = None,
    chapter_id: UUID | None = None,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[ReviewIssue]:
    """列出审校问题，可按 open/resolved/ignored 等状态过滤。"""
    statement = select(ReviewIssue).where(ReviewIssue.novel_id == novel.id)
    if status_filter:
        statement = statement.where(ReviewIssue.status == status_filter)
    if chapter_id:
        statement = statement.where(ReviewIssue.chapter_id == chapter_id)
    statement = statement.order_by(ReviewIssue.created_at.desc())
    return list(db.scalars(statement).all())


@router.patch("/{issue_id}/status", response_model=ReviewIssueRead)
def update_review_issue_status(
    issue_id: UUID,
    payload: ReviewIssueStatusUpdate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> ReviewIssue:
    """更新审校问题状态。"""
    issue = db.get(ReviewIssue, issue_id)
    if issue is None or issue.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Review issue not found")
    if payload.status not in {"open", "resolved", "ignored"}:
        raise HTTPException(status_code=400, detail="Invalid review issue status")
    issue.status = payload.status
    db.commit()
    db.refresh(issue)
    return issue
