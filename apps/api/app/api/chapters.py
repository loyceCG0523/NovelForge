from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.novel import Novel
from app.schemas.chapter import ChapterCreate, ChapterRead


router = APIRouter(prefix="/api/novels/{novel_id}/chapters", tags=["chapters"])


@router.post("", response_model=ChapterRead, status_code=status.HTTP_201_CREATED)
def create_chapter(
    novel_id: UUID,
    payload: ChapterCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> Chapter:
    existing_chapter = db.scalar(
        select(Chapter).where(
            Chapter.novel_id == novel_id,
            Chapter.chapter_index == payload.chapter_index,
        )
    )
    if existing_chapter is not None:
        raise HTTPException(status_code=409, detail="Chapter index already exists")

    chapter = Chapter(
        novel_id=novel_id,
        word_count=len(payload.content),
        **payload.model_dump(),
    )
    db.add(chapter)
    novel.current_chapter_index = max(novel.current_chapter_index, payload.chapter_index)
    db.commit()
    db.refresh(chapter)
    return chapter


@router.get("", response_model=list[ChapterRead])
def list_chapters(
    novel_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[Chapter]:
    statement = (
        select(Chapter)
        .where(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index.asc())
    )
    return list(db.scalars(statement).all())
