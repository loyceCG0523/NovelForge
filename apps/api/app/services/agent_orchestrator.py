from sqlalchemy.orm import Session

from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.schemas.task import AgentRunRequest


def enqueue_agent_task(db: Session, novel: Novel, payload: AgentRunRequest) -> GenerationTask:
    task = GenerationTask(
        novel_id=novel.id,
        chapter_id=payload.chapter_id,
        task_type=payload.task_type,
        status="queued",
        progress=0,
        result_payload={
            "input": payload.input_payload,
            "agent": "reserved",
            "note": "Agent workflow placeholder. Connect LangGraph/Celery here.",
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task
