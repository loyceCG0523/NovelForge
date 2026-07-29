"""整本书自动生产接口。"""

from datetime import datetime, timedelta, timezone
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
from app.models.story_event import StoryEvent
from app.schemas.auto_novel_run import AutoNovelRunRead, AutoNovelRunStart
from app.schemas.task import AgentRunRequest, GenerationTaskRead
from app.services.agent_orchestrator import enqueue_agent_task
from app.services.tone_pacing_contract import resolve_event_chapter_count


router = APIRouter(prefix="/api/novels/{novel_id}/auto-runs", tags=["auto-runs"])
STALE_PAUSE_TASK_SECONDS = 6 * 60


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
    if task is not None and task.status in {"queued", "running", "waiting"}:
        return task
    return None


def _cancel_stale_auto_run_tasks(db: Session, novel: Novel, run: AutoNovelRun) -> list[str]:
    """回收 Worker 重启后遗留的运行中任务，避免暂停状态永久卡在前端。"""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_PAUSE_TASK_SECONDS)
    candidates = db.scalars(
        select(GenerationTask).where(
            GenerationTask.novel_id == novel.id,
            GenerationTask.status.in_({"queued", "running"}),
            GenerationTask.updated_at < cutoff,
        )
    ).all()
    cancelled_ids: list[str] = []
    for task in candidates:
        task_input = (task.result_payload or {}).get("input") or {}
        if task.id != run.task_id and str(task_input.get("auto_run_id") or "") != str(run.id):
            continue
        task.status = "cancelled"
        task.progress = 100
        task.error_message = ""
        task.result_payload = {
            **(task.result_payload or {}),
            "graph_status": "Worker 重启后未完成的任务已回收",
        }
        cancelled_ids.append(str(task.id))
    return cancelled_ids


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
                "production_mode": (run.payload or {}).get("production_mode", "auto"),
                "test_run_scope": (run.payload or {}).get("test_run_scope", "event"),
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


def _enqueue_word_revision_task(
    db: Session,
    novel: Novel,
    run: AutoNovelRun,
    pause_info: dict,
) -> GenerationTask:
    """优先重试字数未达标的当前章节，并沿用原剧情事件章节计划。"""
    task = enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="continue_story_event",
            input_payload={
                "source": "word_guard_resume",
                "auto_run_id": str(run.id),
                "story_event_id": str(pause_info["story_event_id"]),
                "from_chapter_index": int(pause_info["chapter_index"]),
                "test_run_scope": (run.payload or {}).get("test_run_scope", "event"),
                "production_pacing": {
                    "pacing_plan": (run.payload or {}).get("pacing_plan"),
                    "pacing_state": (run.payload or {}).get("pacing_state"),
                    "production_mode": (run.payload or {}).get("production_mode", "auto"),
                    "test_run_scope": (run.payload or {}).get("test_run_scope", "event"),
                },
            },
        ),
    )
    run.task_id = task.id
    run.status = "running"
    run.stage = "word_revision_queued"
    run.last_error = ""
    run.payload = {
        **(run.payload or {}),
        "pause_requested": False,
        "stop_reason": "word_revision_in_progress",
    }
    db.commit()
    db.refresh(task)
    return task


def _enqueue_quality_revision_task(
    db: Session,
    novel: Novel,
    run: AutoNovelRun,
    pause_info: dict,
) -> GenerationTask:
    """事件质量门槛未通过时，只重跑有限事件级修订，不重新生成章节。"""
    story_event = db.get(StoryEvent, UUID(str(pause_info["story_event_id"])))
    if story_event is None or story_event.novel_id != novel.id:
        raise HTTPException(status_code=409, detail="Event quality revision state is invalid")
    event_scope = pause_info.get("scope") == "event"
    production_mode = str((run.payload or {}).get("production_mode") or "auto")
    test_run_scope = str(
        (run.payload or {}).get("test_run_scope")
        or ("event" if event_scope else "first_chapter")
    )
    from_chapter_index = int(
        pause_info.get("chapter_index") or story_event.start_chapter_index or 1
    )
    task = enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="continue_story_event",
            input_payload={
                "source": "event_quality_revision_resume" if event_scope else "test_first_chapter_quality_resume",
                "auto_run_id": str(run.id),
                "story_event_id": str(pause_info["story_event_id"]),
                "from_chapter_index": from_chapter_index,
                "quality_only": event_scope,
                "test_run_scope": test_run_scope,
                "production_pacing": {
                    "pacing_plan": (run.payload or {}).get("pacing_plan"),
                    "pacing_state": (run.payload or {}).get("pacing_state"),
                    "production_mode": production_mode,
                    "test_run_scope": test_run_scope,
                },
            },
        ),
    )
    run.task_id = task.id
    run.status = "running"
    run.stage = "quality_revision_queued"
    run.last_error = ""
    run.payload = {
        **(run.payload or {}),
        "pause_requested": False,
        "stop_reason": "test_first_chapter_quality_revision_in_progress",
    }
    db.commit()
    db.refresh(task)
    return task


def _enqueue_immediate_pause_resume_task(
    db: Session,
    novel: Novel,
    run: AutoNovelRun,
    pause_info: dict,
) -> GenerationTask:
    """从被立即暂停的剧情事件的下一未完成章节继续。"""
    task = enqueue_agent_task(
        db=db,
        novel=novel,
        payload=AgentRunRequest(
            task_type="continue_story_event",
            input_payload={
                "source": "immediate_pause_resume",
                "auto_run_id": str(run.id),
                "story_event_id": str(pause_info["story_event_id"]),
                "from_chapter_index": int(pause_info["next_chapter_index"]),
                "test_run_scope": (run.payload or {}).get("test_run_scope", "event"),
                "production_pacing": {
                    "pacing_plan": (run.payload or {}).get("pacing_plan"),
                    "pacing_state": (run.payload or {}).get("pacing_state"),
                    "production_mode": (run.payload or {}).get("production_mode", "auto"),
                    "test_run_scope": (run.payload or {}).get("test_run_scope", "event"),
                },
            },
        ),
    )
    run.task_id = task.id
    run.status = "running"
    run.stage = "resume_current_event_queued"
    run.last_error = ""
    run.payload = {
        **(run.payload or {}),
        "pause_requested": False,
        "stop_reason": "immediate_pause_resuming",
    }
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

    chapter_count_per_event = (
        payload.chapter_count_per_event
        or resolve_event_chapter_count(novel.brief or {})
    )
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
                "chapter_count_per_event": chapter_count_per_event,
                "production_mode": payload.production_mode,
                "test_run_scope": payload.test_run_scope,
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
            "chapter_count_per_event": chapter_count_per_event,
            "production_mode": payload.production_mode,
            "test_run_scope": payload.test_run_scope,
            "human_loop_status": "",
            "pending_human_event_id": "",
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
    cancelled_task_ids = _cancel_stale_auto_run_tasks(db, novel, run)
    if cancelled_task_ids:
        run.stage = "paused_worker_recovery"
        run.payload = {
            **(run.payload or {}),
            "orphaned_task_ids": cancelled_task_ids,
            "stop_reason": "paused_worker_recovery",
        }
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
    next_payload = {**(run.payload or {}), "pause_requested": False}
    if next_payload.get("human_loop_status") == "waiting_plan_confirmation":
        raise HTTPException(status_code=409, detail="Please confirm the current event chapter plan before continuing")
    immediate_pause = next_payload.get("immediate_pause") or {}
    if next_payload.get("stop_reason") == "paused_immediately" and immediate_pause:
        if not immediate_pause.get("story_event_id") or not immediate_pause.get("next_chapter_index"):
            raise HTTPException(status_code=409, detail="Immediate pause state is incomplete")
        run.payload = next_payload
        return _enqueue_immediate_pause_resume_task(db, novel, run, immediate_pause)
    quality_revision_pause = next_payload.get("quality_revision_pause") or {}
    if next_payload.get("stop_reason") in {
        "test_first_chapter_quality_revision_required",
        "event_quality_revision_required",
        "event_generation_failed",
    } and quality_revision_pause:
        if not quality_revision_pause.get("story_event_id"):
            raise HTTPException(status_code=409, detail="Event quality revision state is incomplete")
        if quality_revision_pause.get("scope") != "event" and not quality_revision_pause.get("chapter_index"):
            raise HTTPException(status_code=409, detail="Chapter quality revision state is incomplete")
        run.payload = next_payload
        return _enqueue_quality_revision_task(db, novel, run, quality_revision_pause)
    word_revision_pause = next_payload.get("word_revision_pause") or {}
    if next_payload.get("stop_reason") == "chapter_word_revision_required" and word_revision_pause:
        if not word_revision_pause.get("story_event_id") or not word_revision_pause.get("chapter_index"):
            raise HTTPException(status_code=409, detail="Word revision state is incomplete")
        run.payload = next_payload
        return _enqueue_word_revision_task(db, novel, run, word_revision_pause)
    if next_payload.get("stop_reason") == "tomato_trial_reached":
        next_payload["production_mode"] = "auto"
        next_payload["tomato_trial_continued"] = True
    if next_payload.get("stop_reason") == "test_run_event_completed":
        next_payload["production_mode"] = "auto"
        next_payload["test_run_continued"] = True
    run.payload = next_payload
    return _enqueue_production_task(db, novel, run, "auto_run_resume")
