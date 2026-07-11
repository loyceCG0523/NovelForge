"""整本书自动生产接口。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.auto_novel_run import AutoNovelRun
from app.models.chapter import Chapter
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.schemas.auto_novel_run import AutoNovelRunRead, AutoNovelRunStart
from app.schemas.task import AgentRunRequest, GenerationTaskRead
from app.services.agent_orchestrator import enqueue_agent_task


router = APIRouter(prefix="/api/novels/{novel_id}/auto-runs", tags=["auto-runs"])


def _current_word_count(db: Session, novel: Novel) -> int:
    """统计当前作品已生成正文总字数。"""
    return db.scalar(select(func.coalesce(func.sum(Chapter.word_count), 0)).where(Chapter.novel_id == novel.id)) or 0


def _latest_auto_run(db: Session, novel: Novel) -> AutoNovelRun | None:
    """读取当前作品最近一次整本书生产记录。"""
    return db.scalar(
        select(AutoNovelRun)
        .where(AutoNovelRun.novel_id == novel.id)
        .order_by(AutoNovelRun.updated_at.desc())
        .limit(1)
    )


def _active_task(db: Session, run: AutoNovelRun | None) -> GenerationTask | None:
    """如果总控任务仍在队列或运行中，返回对应任务。"""
    if run is None or run.task_id is None:
        return None
    task = db.get(GenerationTask, run.task_id)
    if task is not None and task.status in {"queued", "running"}:
        return task
    return None


def _enqueue_production_task(db: Session, novel: Novel, run: AutoNovelRun, source: str) -> GenerationTask:
    """为已有 AutoNovelRun 创建新的 Worker 总控任务。"""
    task = enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="produce_novel",
            input_payload={
                "source": source,
                "auto_run_id": str(run.id),
                "chapter_count_per_event": (run.payload or {}).get("chapter_count_per_event", 8),
                "max_event_count": run.max_event_count,
            },
        ),
    )
    run.task_id = task.id
    run.status = "running"
    run.stage = "queued"
    run.last_error = ""
    db.commit()
    db.refresh(task)
    return task


@router.get("/current", response_model=AutoNovelRunRead | None)
def get_current_auto_run(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> AutoNovelRun | None:
    """读取最近一次整本书自动生产状态。"""
    return _latest_auto_run(db, novel)


@router.post("/start", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def start_auto_run(
    payload: AutoNovelRunStart,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """启动整本书自动生产；已有运行中任务时直接返回当前任务。"""
    run = _latest_auto_run(db, novel)
    active_task = _active_task(db, run)
    if active_task is not None:
        return active_task

    if run is None or run.status in {"completed", "failed", "cancelled"}:
        run = AutoNovelRun(
            novel_id=novel.id,
            status="running",
            stage="queued",
            target_words=novel.target_words,
            current_words=_current_word_count(db, novel),
            produced_event_count=0,
            max_event_count=payload.max_event_count,
            payload={
                "chapter_count_per_event": payload.chapter_count_per_event,
                "events": [],
            },
        )
        db.add(run)
        db.commit()
        db.refresh(run)
    else:
        run.status = "running"
        run.stage = "queued"
        run.max_event_count = payload.max_event_count
        run.payload = {
            **(run.payload or {}),
            "chapter_count_per_event": payload.chapter_count_per_event,
        }
        db.commit()

    return _enqueue_production_task(db, novel, run, "auto_run_start")


@router.post("/pause", response_model=AutoNovelRunRead)
def pause_auto_run(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> AutoNovelRun:
    """请求暂停自动生产；Worker 会在当前剧情事件完成后停下。"""
    run = _latest_auto_run(db, novel)
    if run is None:
        raise HTTPException(status_code=404, detail="Auto run not found")
    run.status = "paused"
    run.stage = "pause_requested"
    run.payload = {**(run.payload or {}), "pause_requested": True}
    db.commit()
    db.refresh(run)
    return run


@router.post("/resume", response_model=GenerationTaskRead, status_code=status.HTTP_202_ACCEPTED)
def resume_auto_run(
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """继续最近一次已暂停或失败的自动生产。"""
    run = _latest_auto_run(db, novel)
    if run is None:
        raise HTTPException(status_code=404, detail="Auto run not found")
    active_task = _active_task(db, run)
    if active_task is not None:
        return active_task
    if run.status == "completed":
        raise HTTPException(status_code=409, detail="Auto run already completed")
    run.payload = {**(run.payload or {}), "pause_requested": False}
    return _enqueue_production_task(db, novel, run, "auto_run_resume")
