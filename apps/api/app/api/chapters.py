"""章节管理接口。

章节是 Agent 生成和人工编辑共同作用的主实体。除正文外，这里还保存章节摘要、
字数和生成时使用的 context_snapshot，便于后续追踪“这一章为什么这样写”。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.novel import Novel
from app.schemas.chapter import ChapterCreate, ChapterRead, ChapterUpdate


router = APIRouter(prefix="/api/novels/{novel_id}/chapters", tags=["chapters"])


@router.post("", response_model=ChapterRead, status_code=status.HTTP_201_CREATED)
def create_chapter(
    novel_id: UUID,
    payload: ChapterCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> Chapter:
    """手动创建章节，并保证同一作品内 chapter_index 不重复。"""
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
    """按章节序号升序返回作品全部章节。"""
    statement = (
        select(Chapter)
        .where(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index.asc())
    )
    return list(db.scalars(statement).all())


@router.get("/{chapter_id}", response_model=ChapterRead)
def get_chapter(
    chapter_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> Chapter:
    """读取章节详情，包含正文与上下文快照。"""
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return chapter


@router.patch("/{chapter_id}", response_model=ChapterRead)
def update_chapter(
    chapter_id: UUID,
    payload: ChapterUpdate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> Chapter:
    """更新章节内容；正文变化时同步刷新字数统计。"""
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "chapter_index" in update_data and update_data["chapter_index"] != chapter.chapter_index:
        existing_chapter = db.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_index == update_data["chapter_index"],
            )
        )
        if existing_chapter is not None:
            raise HTTPException(status_code=409, detail="Chapter index already exists")

    for key, value in update_data.items():
        setattr(chapter, key, value)
    if "content" in update_data:
        chapter.word_count = len(update_data["content"] or "")

    novel.current_chapter_index = max(novel.current_chapter_index, chapter.chapter_index)
    db.commit()
    db.refresh(chapter)
    return chapter


@router.delete("/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chapter(
    chapter_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    """删除章节记录。当前版本不级联删除相关任务或审校问题。"""
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    db.delete(chapter)
    db.commit()
