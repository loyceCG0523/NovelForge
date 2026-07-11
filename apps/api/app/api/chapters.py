"""章节管理接口。

章节是 Agent 生成和人工编辑共同作用的主实体。除正文外，这里还保存章节摘要、
字数和生成时使用的 context_snapshot，便于后续追踪“这一章为什么这样写”。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.event_chapter_plan import EventChapterPlan
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.story_event import StoryEvent
from app.schemas.chapter import ChapterCreate, ChapterRead, ChapterUpdate
from app.services.agents.memory_agent import purge_memory_for_chapter


router = APIRouter(prefix="/api/novels/{novel_id}/chapters", tags=["chapters"])


def _chapter_event_plan(db: Session, chapter: Chapter) -> dict | None:
    """读取章节所属剧情事件计划，用于章节管理页展示。"""
    plan = db.scalar(
        select(EventChapterPlan)
        .where(
            EventChapterPlan.novel_id == chapter.novel_id,
            EventChapterPlan.chapter_index == chapter.chapter_index,
        )
        .order_by(EventChapterPlan.updated_at.desc())
        .limit(1)
    )
    if plan is None:
        return None
    story_event = db.get(StoryEvent, plan.story_event_id)
    return {
        "story_event_id": str(plan.story_event_id),
        "story_event_title": story_event.title if story_event else "",
        "plan_id": str(plan.id),
        "function": plan.function,
        "core_event": plan.core_event,
        "ending_hook": plan.ending_hook,
        "status": plan.status,
    }


def _chapter_to_read(db: Session, chapter: Chapter) -> dict:
    """把章节 ORM 对象补充事件计划后返回给前端。"""
    return {
        "id": chapter.id,
        "novel_id": chapter.novel_id,
        "chapter_index": chapter.chapter_index,
        "title": chapter.title,
        "status": chapter.status,
        "word_count": chapter.word_count,
        "summary": chapter.summary,
        "content": chapter.content,
        "context_snapshot": chapter.context_snapshot,
        "event_plan": _chapter_event_plan(db, chapter),
    }


def _event_level_issue_filter(novel_id: UUID, story_event_id: UUID):
    """匹配某个剧情事件自动生成的事件级质量记录。"""
    return (
        ReviewIssue.novel_id == novel_id,
        ReviewIssue.chapter_id.is_(None),
        ReviewIssue.issue_type.like("event_%"),
        ReviewIssue.payload["story_event_id"].astext == str(story_event_id),
    )


def _refresh_story_events_after_chapter_delete(
    db: Session,
    novel: Novel,
    story_event_ids: set[UUID],
) -> None:
    """章节删除后刷新或清理关联剧情事件。

    - 如果事件还剩实际章节：更新 generated_chapter_count / remaining_open_risks。
    - 如果事件已没有实际章节：删除事件级质量记录、章节计划和 StoryEvent，避免前端显示空壳事件。
    """
    for story_event_id in story_event_ids:
        story_event = db.get(StoryEvent, story_event_id)
        if story_event is None or story_event.novel_id != novel.id:
            continue

        plans = list(
            db.scalars(
                select(EventChapterPlan)
                .where(EventChapterPlan.story_event_id == story_event.id)
                .order_by(EventChapterPlan.chapter_index.asc())
            ).all()
        )
        materialized_plans = [
            plan
            for plan in plans
            if plan.chapter_id is not None and plan.status in {"generated", "revised"}
        ]

        if not materialized_plans:
            db.execute(delete(ReviewIssue).where(*_event_level_issue_filter(novel.id, story_event.id)))
            db.execute(delete(EventChapterPlan).where(EventChapterPlan.story_event_id == story_event.id))
            db.delete(story_event)
            continue

        chapter_ids = [plan.chapter_id for plan in materialized_plans if plan.chapter_id is not None]
        open_chapter_risks = db.scalar(
            select(func.count())
            .select_from(ReviewIssue)
            .where(
                ReviewIssue.novel_id == novel.id,
                ReviewIssue.chapter_id.in_(chapter_ids),
                ReviewIssue.status == "open",
            )
        ) or 0
        open_event_risks = db.scalar(
            select(func.count())
            .select_from(ReviewIssue)
            .where(
                *_event_level_issue_filter(novel.id, story_event.id),
                ReviewIssue.status == "open",
            )
        ) or 0
        story_event.generated_chapter_count = len(materialized_plans)
        story_event.remaining_open_risks = open_chapter_risks + open_event_risks
        story_event.status = (
            "completed"
            if story_event.generated_chapter_count >= story_event.planned_chapter_count
            else "generating"
        )


@router.post("", response_model=ChapterRead, status_code=status.HTTP_201_CREATED)
def create_chapter(
    novel_id: UUID,
    payload: ChapterCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> dict:
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
    return _chapter_to_read(db, chapter)


@router.get("", response_model=list[ChapterRead])
def list_chapters(
    novel_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[dict]:
    """按章节序号升序返回作品全部章节。"""
    statement = (
        select(Chapter)
        .where(Chapter.novel_id == novel_id)
        .order_by(Chapter.chapter_index.asc())
    )
    return [_chapter_to_read(db, chapter) for chapter in db.scalars(statement).all()]


@router.get("/{chapter_id}", response_model=ChapterRead)
def get_chapter(
    chapter_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> dict:
    """读取章节详情，包含正文与上下文快照。"""
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return _chapter_to_read(db, chapter)


@router.patch("/{chapter_id}", response_model=ChapterRead)
def update_chapter(
    chapter_id: UUID,
    payload: ChapterUpdate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> dict:
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
    return _chapter_to_read(db, chapter)


@router.delete("/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chapter(
    chapter_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    """删除章节，并同步清理该章节关联的派生数据。

    ReviewIssue 直接依赖 chapter_id，应随章节删除；GenerationTask 是历史任务记录，
    只解除章节引用，避免删除章节时触发外键约束。
    """
    chapter = db.get(Chapter, chapter_id)
    if chapter is None or chapter.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Chapter not found")

    deleted_chapter_index = chapter.chapter_index
    affected_plans = list(
        db.scalars(
            select(EventChapterPlan).where(
                EventChapterPlan.novel_id == novel.id,
                or_(
                    EventChapterPlan.chapter_id == chapter.id,
                    EventChapterPlan.chapter_index == deleted_chapter_index,
                ),
            )
        ).all()
    )
    affected_story_event_ids = {plan.story_event_id for plan in affected_plans}

    purge_memory_for_chapter(db=db, novel=novel, chapter=chapter)
    db.execute(delete(ReviewIssue).where(ReviewIssue.novel_id == novel.id, ReviewIssue.chapter_id == chapter.id))
    db.execute(update(GenerationTask).where(GenerationTask.chapter_id == chapter.id).values(chapter_id=None))
    for plan in affected_plans:
        plan.chapter_id = None
        if plan.status in {"generated", "revised"}:
            plan.status = "planned"
        plan.payload = {
            key: value
            for key, value in (plan.payload or {}).items()
            if key
            not in {
                "generated_chapter_id",
                "generated_word_count",
                "last_generated_chapter_id",
                "last_generation_source",
            }
        }
    db.delete(chapter)
    novel.current_chapter_index = db.scalar(
        select(func.coalesce(func.max(Chapter.chapter_index), 0)).where(
            Chapter.novel_id == novel.id,
            Chapter.id != chapter.id,
        )
    )
    _refresh_story_events_after_chapter_delete(db, novel, affected_story_event_ids)
    db.commit()
