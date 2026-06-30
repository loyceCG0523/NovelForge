from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.schemas.task import AgentRunRequest, GenerationTaskCreate, GenerationTaskRead
from app.services.agent_orchestrator import enqueue_agent_task


router = APIRouter(prefix="/api/novels/{novel_id}/tasks", tags=["tasks"])


@router.post("", response_model=GenerationTaskRead, status_code=status.HTTP_201_CREATED)
def create_generation_task(
    payload: GenerationTaskCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    if payload.chapter_id is not None:
        chapter = db.get(Chapter, payload.chapter_id)
        if chapter is None or chapter.novel_id != novel.id:
            raise HTTPException(status_code=404, detail="Chapter not found")

    task = GenerationTask(novel_id=novel.id, **payload.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.post("/agent-runs", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def create_agent_run(
    payload: AgentRunRequest,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    if payload.chapter_id is not None:
        chapter = db.get(Chapter, payload.chapter_id)
        if chapter is None or chapter.novel_id != novel.id:
            raise HTTPException(status_code=404, detail="Chapter not found")
    return enqueue_agent_task(db=db, novel=novel, payload=payload)


@router.get("", response_model=list[GenerationTaskRead])
def list_generation_tasks(
    status_filter: str | None = None,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[GenerationTask]:
    statement = select(GenerationTask).where(GenerationTask.novel_id == novel.id)
    if status_filter:
        statement = statement.where(GenerationTask.status == status_filter)
    statement = statement.order_by(GenerationTask.created_at.desc())
    return list(db.scalars(statement).all())


@router.get("/{task_id}", response_model=GenerationTaskRead)
def get_generation_task(
    task_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    task = db.get(GenerationTask, task_id)
    if task is None or task.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
