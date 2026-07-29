"""异步事件子任务与整书总控之间的持久化交接。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.auto_novel_run import AutoNovelRun
from app.models.chapter import Chapter
from app.models.generation_task import GenerationTask
from app.models.story_event import StoryEvent
from app.services.agent_orchestrator import push_task_to_queue
from app.services.task_events import emit_task_event


def _current_words(db: Session, novel_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(Chapter.word_count), 0)).where(
                Chapter.novel_id == novel_id
            )
        )
        or 0
    )


def _load_handoff(
    db: Session,
    child_task: GenerationTask,
) -> tuple[GenerationTask | None, AutoNovelRun | None, dict]:
    task_input = (child_task.result_payload or {}).get("input") or {}
    parent_id = task_input.get("parent_task_id")
    run_id = task_input.get("auto_run_id")
    parent = db.get(GenerationTask, UUID(str(parent_id))) if parent_id else None
    run = db.get(AutoNovelRun, UUID(str(run_id))) if run_id else None
    return parent, run, task_input


def complete_event_child_handoff(
    db: Session,
    *,
    child_task: GenerationTask,
    output: dict,
) -> dict:
    """合并子任务结果；继续生产时只重新唤醒父任务，不在当前进程嵌套执行。"""
    parent, run, task_input = _load_handoff(db, child_task)
    if parent is None or run is None:
        return {"handled": False}
    run_payload = run.payload or {}
    pending_child_task_id = str(run_payload.get("pending_child_task_id") or "")
    if pending_child_task_id != str(child_task.id):
        reason = (
            "already_handed_off"
            if str(run_payload.get("last_child_task_id") or "") == str(child_task.id)
            else "superseded_child"
        )
        return {"handled": False, "reason": reason}

    event_number = int(task_input.get("production_event_index") or 0)
    story_event_id = str(output.get("story_event_id") or "")
    story_event = (
        db.get(StoryEvent, UUID(story_event_id))
        if story_event_id
        else None
    )
    run.current_event_id = story_event.id if story_event is not None else None
    run.current_words = _current_words(db, run.novel_id)
    run.last_error = ""
    event_record = {
        "story_event_id": story_event_id,
        "title": (output.get("event_plan") or {}).get("event_title", ""),
        "event_type": (output.get("event_plan") or {}).get("event_type", ""),
        "phase": (run.payload or {}).get("pending_event_phase", ""),
        "plan_only": bool(task_input.get("plan_only")),
        "chapter_count": len(output.get("generated_chapters", [])),
        "word_count": sum(
            int(item.get("word_count") or 0)
            for item in output.get("generated_chapters", [])
        ),
        "child_task_id": str(child_task.id),
    }
    events = [
        item
        for item in (run.payload or {}).get("events", [])
        if str(item.get("child_task_id") or "") != str(child_task.id)
    ]
    events.append(event_record)
    contract = output.get("contract") or {}

    pause_reason = ""
    pause_payload: dict = {}
    if output.get("requires_quality_revision"):
        pause_reason = "event_quality_revision_required"
        pause_payload = {
            "quality_revision_pause": output.get("quality_revision_pause") or {}
        }
        run.stage = "quality_revision_required"
    elif output.get("requires_word_revision"):
        pause_reason = "chapter_word_revision_required"
        pause_payload = {"word_revision_pause": output.get("word_revision_pause") or {}}
        run.stage = "word_revision_required"
    elif output.get("pause_requested"):
        pause_reason = "paused_immediately"
        pause_payload = {"immediate_pause": output.get("immediate_pause") or {}}
        run.stage = "paused_immediately"
    elif bool(task_input.get("plan_only")):
        pause_reason = "human_plan_review_required"
        pause_payload = {
            "human_loop_status": "waiting_plan_confirmation",
            "pending_human_event_id": story_event_id,
        }
        run.stage = "waiting_plan_confirmation"
    elif contract.get("status") == "degraded":
        pause_reason = "degraded_output_review_required"
        pause_payload = {
            "degraded_child_task_id": str(child_task.id),
            "degraded_contract": contract,
        }
        run.stage = "degraded_output_review_required"
    elif run.status == "paused" or bool((run.payload or {}).get("pause_requested")):
        pause_reason = "paused_after_event"
        run.stage = "paused_after_event"
    elif (run.payload or {}).get("production_mode") == "test_run":
        pause_reason = "test_run_event_completed"
        run.stage = "test_run_completed"

    run.payload = {
        **(run.payload or {}),
        "events": events,
        "pending_child_task_id": "",
        "pending_event_number": 0,
        "pending_event_phase": "",
        "last_child_task_id": str(child_task.id),
        "last_child_completed_at": datetime.now(timezone.utc).isoformat(),
        **pause_payload,
    }
    if event_number:
        run.produced_event_count = max(run.produced_event_count, event_number)

    if pause_reason:
        run.status = "paused"
        run.payload = {
            **run.payload,
            "stop_reason": pause_reason,
            "pause_requested": False,
        }
        parent.status = "completed"
        parent.progress = 100
        parent.result_payload = {
            **(parent.result_payload or {}),
            "handoff": {
                "status": "completed",
                "child_task_id": str(child_task.id),
                "stop_reason": pause_reason,
            },
        }
        db.commit()
        emit_task_event(
            db,
            parent,
            event_type="handoff",
            step_key=f"event_{event_number}_handoff",
            status="completed",
            title="剧情事件子任务已完成，整书生产已按策略暂停",
            message=pause_reason,
            progress=100,
            payload={"child_task_id": str(child_task.id), "stop_reason": pause_reason},
        )
        return {"handled": True, "parent_requeued": False, "stop_reason": pause_reason}

    run.status = "running"
    run.stage = "event_completed_resuming"
    run.payload = {
        **run.payload,
        "stop_reason": "",
        "pause_requested": False,
    }
    parent.status = "queued"
    parent.error_message = ""
    parent.result_payload = {
        **(parent.result_payload or {}),
        "handoff": {
            "status": "child_completed",
            "child_task_id": str(child_task.id),
            "event_number": event_number,
        },
    }
    db.commit()
    emit_task_event(
        db,
        parent,
        event_type="handoff",
        step_key=f"event_{event_number}_handoff",
        status="completed",
        title=f"第 {event_number} 个剧情事件已完成",
        message="正在唤醒整书总控检查停止条件",
        progress=min(95, max(10, int(parent.progress or 0))),
        payload={"child_task_id": str(child_task.id), "parent_requeued": True},
    )
    try:
        push_task_to_queue(str(parent.id), settings.agent_task_queue)
    except Exception as exc:
        parent.result_payload = {
            **(parent.result_payload or {}),
            "queue_notification_error": str(exc),
            "queue_notification_retryable": True,
        }
        db.commit()
    return {"handled": True, "parent_requeued": True}


def fail_event_child_handoff(
    db: Session,
    *,
    child_task: GenerationTask,
    error: str,
) -> dict:
    parent, run, _ = _load_handoff(db, child_task)
    if parent is None or run is None:
        return {"handled": False}
    run_payload = run.payload or {}
    pending_child_task_id = str(run_payload.get("pending_child_task_id") or "")
    if pending_child_task_id != str(child_task.id):
        reason = (
            "already_handed_off"
            if str(run_payload.get("last_child_task_id") or "") == str(child_task.id)
            else "superseded_child"
        )
        return {"handled": False, "reason": reason}
    run.status = "failed"
    run.stage = "event_failed"
    run.last_error = error
    run.payload = {
        **(run.payload or {}),
        "stop_reason": "event_generation_failed",
        "failed_child_task_id": str(child_task.id),
        "pending_child_task_id": "",
    }
    parent.status = "failed"
    parent.progress = 100
    parent.error_message = error
    parent.result_payload = {
        **(parent.result_payload or {}),
        "handoff": {
            "status": "failed",
            "child_task_id": str(child_task.id),
            "error": error,
            "retryable": True,
        },
    }
    db.commit()
    emit_task_event(
        db,
        parent,
        event_type="handoff",
        step_key="event_child_failed",
        status="failed",
        title="剧情事件子任务失败",
        message=error,
        progress=100,
        payload={"child_task_id": str(child_task.id), "retryable": True},
    )
    return {"handled": True}
