"""作品故事时间线管理接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.novel import Novel
from app.models.story_event import StoryEvent
from app.models.timeline_entry import TimelineEntry
from app.schemas.timeline import TimelineEntryCreate, TimelineEntryRead, TimelineEntryUpdate


router = APIRouter(prefix="/api/novels/{novel_id}/timeline", tags=["timeline"])


def _validate_relations(db: Session, novel: Novel, chapter_id: UUID | None, story_event_id: UUID | None) -> None:
    if chapter_id:
        chapter = db.get(Chapter, chapter_id)
        if chapter is None or chapter.novel_id != novel.id:
            raise HTTPException(status_code=400, detail="Chapter does not belong to the novel")
    if story_event_id:
        story_event = db.get(StoryEvent, story_event_id)
        if story_event is None or story_event.novel_id != novel.id:
            raise HTTPException(status_code=400, detail="Story event does not belong to the novel")


@router.get("", response_model=list[TimelineEntryRead])
def list_timeline_entries(novel: Novel = Depends(get_owned_novel), db: Session = Depends(get_db)) -> list[TimelineEntry]:
    return list(
        db.scalars(
            select(TimelineEntry)
            .where(TimelineEntry.novel_id == novel.id)
            .order_by(TimelineEntry.sequence_no.asc(), TimelineEntry.created_at.asc())
        ).all()
    )


@router.post("", response_model=TimelineEntryRead, status_code=status.HTTP_201_CREATED)
def create_timeline_entry(
    payload: TimelineEntryCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> TimelineEntry:
    _validate_relations(db, novel, payload.chapter_id, payload.story_event_id)
    entry = TimelineEntry(novel_id=novel.id, **payload.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.patch("/{entry_id}", response_model=TimelineEntryRead)
def update_timeline_entry(
    entry_id: UUID,
    payload: TimelineEntryUpdate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> TimelineEntry:
    entry = db.get(TimelineEntry, entry_id)
    if entry is None or entry.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Timeline entry not found")
    updates = payload.model_dump(exclude_unset=True)
    _validate_relations(
        db,
        novel,
        updates.get("chapter_id", entry.chapter_id),
        updates.get("story_event_id", entry.story_event_id),
    )
    for key, value in updates.items():
        setattr(entry, key, value)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_timeline_entry(
    entry_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> None:
    entry = db.get(TimelineEntry, entry_id)
    if entry is None or entry.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Timeline entry not found")
    db.delete(entry)
    db.commit()
