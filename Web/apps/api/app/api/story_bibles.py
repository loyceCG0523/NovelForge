"""作品圣经接口。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.novel import Novel
from app.models.story_bible import StoryBible
from app.schemas.story_bible import StoryBibleGenerateRequest, StoryBibleRead, StoryBibleUpdate
from app.schemas.task import AgentRunRequest, GenerationTaskRead
from app.services.agent_orchestrator import enqueue_agent_task
from app.services.story_bible_builder import build_fallback_story_bible, build_story_bible_summary


router = APIRouter(prefix="/api/novels/{novel_id}/story-bible", tags=["story-bibles"])


def _get_story_bible(db: Session, novel: Novel) -> StoryBible | None:
    """读取作品绑定的 StoryBible。"""
    return db.scalar(select(StoryBible).where(StoryBible.novel_id == novel.id))


@router.get("", response_model=StoryBibleRead)
def get_story_bible(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> StoryBible:
    """读取作品圣经；如果还没有正式生成，则用起始需求即时创建一版草案。"""
    story_bible = _get_story_bible(db, novel)
    if story_bible is None:
        content = build_fallback_story_bible(novel)
        story_bible = StoryBible(
            novel_id=novel.id,
            status="draft",
            source="fallback_from_brief",
            summary=build_story_bible_summary(content, novel),
            content=content,
            locked_fields={},
        )
        db.add(story_bible)
        db.commit()
        db.refresh(story_bible)
    return story_bible


@router.patch("", response_model=StoryBibleRead)
def update_story_bible(
    payload: StoryBibleUpdate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> StoryBible:
    """手动更新作品圣经，供作者微调和锁定关键设定。"""
    story_bible = _get_story_bible(db, novel)
    if story_bible is None:
        raise HTTPException(status_code=404, detail="StoryBible not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(story_bible, key, value)
    if "content" in update_data and "summary" not in update_data:
        story_bible.summary = build_story_bible_summary(story_bible.content or {}, novel)
    story_bible.version += 1
    story_bible.source = "manual"
    db.commit()
    db.refresh(story_bible)
    return story_bible


@router.post("/generate", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def generate_story_bible(
    payload: StoryBibleGenerateRequest,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTaskRead:
    """触发作品圣经 Agent，异步生成或刷新全书级设定。"""
    task = enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="build_story_bible",
            chapter_id=None,
            input_payload=payload.input_payload,
        ),
    )
    return task
