"""Agent 任务编排服务。

API 层只负责接收用户操作；真正耗时的生成任务通过这里写入数据库并推送到 Redis 队列。
"""

import hashlib
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from redis import Redis

from app.core.config import settings
from app.models.chapter import Chapter
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.schemas.task import AgentRunRequest
from app.services.task_events import emit_task_event, initialize_task_todo


def push_task_to_queue(task_id: str, queue_name: str | None = None) -> None:
    """把任务 ID 推入 Redis 队列，等待 Worker 消费。"""
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    redis_client.rpush(queue_name or settings.agent_task_queue, task_id)


def notify_persisted_task(
    db: Session,
    task: GenerationTask,
    queue_name: str | None = None,
) -> bool:
    """DB 记录是任务真源；Redis 暂时不可用时由 Worker 恢复器补发通知。"""
    try:
        push_task_to_queue(str(task.id), queue_name)
        return True
    except Exception as exc:
        task.result_payload = {
            **(task.result_payload or {}),
            "queue_notification_error": str(exc),
            "queue_notification_retryable": True,
        }
        db.commit()
        return False


def _chapter_content_hash(chapter: Chapter) -> str:
    return hashlib.sha256((chapter.content or "").encode("utf-8")).hexdigest()


def aggregate_memory_batch_progress(
    batch_tasks: list[GenerationTask],
    expected_total: int = 0,
) -> dict[str, int]:
    """按章节的最新正文版本统计进度，重同步任务不重复增加章节总数。"""
    unique_tasks = list({str(task.id): task for task in batch_tasks}.values())
    latest_by_chapter: dict[str, GenerationTask] = {}
    for task in sorted(
        unique_tasks,
        key=lambda item: (
            item.created_at.timestamp() if getattr(item, "created_at", None) else 0,
            str(getattr(item, "id", "")),
        ),
    ):
        task_input = (task.result_payload or {}).get("input") or {}
        chapter_key = str(
            getattr(task, "chapter_id", None)
            or task_input.get("chapter_id")
            or f"task:{task.id}"
        )
        latest_by_chapter[chapter_key] = task

    latest_tasks = list(latest_by_chapter.values())
    completed = sum(task.status == "completed" for task in latest_tasks)
    failed = sum(task.status == "failed" for task in latest_tasks)
    total = max(int(expected_total or 0), len(latest_tasks))
    finished = completed + failed
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "pending": max(0, total - finished),
        "resync_count": max(0, len(unique_tasks) - len(latest_tasks)),
    }


def enqueue_chapter_memory_tasks(
    db: Session,
    *,
    novel: Novel,
    chapters: list[Chapter],
    source_task: GenerationTask,
    story_event_id=None,
    source_progress: int = 95,
    sync_reason: str = "chapter_ready",
    expected_total: int | None = None,
) -> list[GenerationTask]:
    """把章节记忆同步拆到专用队列，并按正文版本避免重复入队。"""
    if not chapters:
        return []
    source_progress = max(0, min(int(source_progress), 100))
    batch_id = f"source:{source_task.id}"
    expected_total = max(len(chapters), int(expected_total or len(chapters)))
    tasks: list[GenerationTask] = []
    queued_chapters: list[Chapter] = []
    for chapter in chapters:
        content_hash = _chapter_content_hash(chapter)
        candidates = db.scalars(
            select(GenerationTask).where(
                GenerationTask.novel_id == novel.id,
                GenerationTask.chapter_id == chapter.id,
                GenerationTask.task_type == "sync_chapter_memory",
                GenerationTask.status.in_(["queued", "running", "completed"]),
            )
        ).all()
        duplicate = any(
            str(((candidate.result_payload or {}).get("input") or {}).get("source_task_id") or "")
            == str(source_task.id)
            and str(((candidate.result_payload or {}).get("input") or {}).get("content_hash") or "")
            == content_hash
            for candidate in candidates
        )
        if duplicate:
            continue
        task = GenerationTask(
            novel_id=novel.id,
            chapter_id=chapter.id,
            task_type="sync_chapter_memory",
            status="queued",
            progress=0,
            result_payload={
                "input": {
                    "source": "background_memory_sync",
                    "source_task_id": str(source_task.id),
                    "memory_batch_id": batch_id,
                    "chapter_id": str(chapter.id),
                    "chapter_index": chapter.chapter_index,
                    "story_event_id": str(story_event_id or ""),
                    "content_hash": content_hash,
                    "source_progress": source_progress,
                    "sync_reason": sync_reason,
                    "expected_total": expected_total,
                },
                "agent": "MemoryAgent.background",
                "note": "章节正文已可用；结构化记忆与时间线正在后台同步。",
            },
        )
        db.add(task)
        tasks.append(task)
        queued_chapters.append(chapter)
    if not tasks:
        return []
    db.commit()
    for task in tasks:
        db.refresh(task)

    persisted_memory_tasks = db.scalars(
        select(GenerationTask).where(
            GenerationTask.novel_id == novel.id,
            GenerationTask.task_type == "sync_chapter_memory",
        )
    ).all()
    batch_tasks = [
        candidate
        for candidate in [*persisted_memory_tasks, *tasks]
        if str(((candidate.result_payload or {}).get("input") or {}).get("memory_batch_id") or "")
        == batch_id
    ]
    unique_batch_tasks = list({
        str(candidate.id): candidate
        for candidate in batch_tasks
    }.values())
    aggregate = aggregate_memory_batch_progress(unique_batch_tasks, expected_total)
    emit_task_event(
        db,
        source_task,
        event_type="memory_sync",
        step_key="memory_sync",
        status="running",
        title="后台同步记忆与时间线",
        message=f"已提交 {len(tasks)} 个章节后台同步，正文生成可继续执行",
        progress=source_progress,
        payload={
            "memory_batch_id": batch_id,
            "total": aggregate["total"],
            "submitted": len(tasks),
            "completed": aggregate["completed"],
            "failed": aggregate["failed"],
            "pending": aggregate["pending"],
            "resync_count": aggregate["resync_count"],
            "background": True,
            "sync_reason": sync_reason,
        },
    )
    for task, chapter in zip(tasks, queued_chapters):
        emit_task_event(
            db,
            source_task,
            event_type="memory_sync",
            step_key=f"chapter_{chapter.chapter_index}_memory_sync",
            status="pending",
            title=f"第 {chapter.chapter_index} 章记忆同步已排队",
            message="等待后台 Memory Worker 处理",
            progress=source_progress,
            chapter_id=chapter.id,
            chapter_index=chapter.chapter_index,
            payload={
                "memory_task_id": str(task.id),
                "memory_batch_id": batch_id,
                "background": True,
                "content_hash": ((task.result_payload or {}).get("input") or {}).get("content_hash", ""),
                "sync_reason": sync_reason,
            },
        )
    for task in tasks:
        notify_persisted_task(db, task, settings.memory_task_queue)
    return tasks


def retry_chapter_memory_task(
    db: Session,
    *,
    failed_task: GenerationTask,
    source_task: GenerationTask,
    chapter: Chapter,
) -> GenerationTask:
    """只重试一个失败章节，并继续把状态镜像到原正文任务。"""
    task_input = (failed_task.result_payload or {}).get("input") or {}
    source_progress = max(0, min(int(task_input.get("source_progress") or 95), 100))
    batch_id = str(uuid4())
    task = GenerationTask(
        novel_id=failed_task.novel_id,
        chapter_id=chapter.id,
        task_type="sync_chapter_memory",
        status="queued",
        progress=0,
        result_payload={
            "input": {
                **task_input,
                "source": "background_memory_retry",
                "source_task_id": str(source_task.id),
                "memory_batch_id": batch_id,
                "retry_of": str(failed_task.id),
                "content_hash": _chapter_content_hash(chapter),
                "source_progress": source_progress,
                "sync_reason": "manual_retry",
                "expected_total": 1,
            },
            "agent": "MemoryAgent.background",
            "note": f"正在重试第 {chapter.chapter_index} 章记忆同步。",
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    emit_task_event(
        db,
        source_task,
        event_type="memory_sync",
        step_key="memory_sync",
        status="running",
        title="正在重试失败章节的后台记忆同步",
        message=f"仅重新处理第 {chapter.chapter_index} 章",
        progress=source_progress,
        payload={
            "memory_batch_id": batch_id,
            "total": 1,
            "completed": 0,
            "failed": 0,
            "background": True,
            "retry": True,
        },
    )
    emit_task_event(
        db,
        source_task,
        event_type="memory_sync",
        step_key=f"chapter_{chapter.chapter_index}_memory_sync",
        status="pending",
        title=f"第 {chapter.chapter_index} 章记忆同步已重新排队",
        message="仅重试此前失败的章节",
        progress=source_progress,
        chapter_id=chapter.id,
        chapter_index=chapter.chapter_index,
        payload={
            "memory_task_id": str(task.id),
            "memory_batch_id": batch_id,
            "retry_of": str(failed_task.id),
            "background": True,
            "sync_reason": "manual_retry",
        },
    )
    notify_persisted_task(db, task, settings.memory_task_queue)
    return task


def enqueue_agent_task(db: Session, novel: Novel, payload: AgentRunRequest) -> GenerationTask:
    """创建任务记录并入队，返回可供前端轮询的任务实体。"""
    task = GenerationTask(
        novel_id=novel.id,
        chapter_id=payload.chapter_id,
        task_type=payload.task_type,
        status="queued",
        progress=0,
        result_payload={
            "input": payload.input_payload,
            "agent": "RedisTaskQueue",
            "note": "任务已进入 Redis 队列，Worker 会按任务类型分发到处理器或 LangGraph 工作流。",
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    emit_task_event(
        db,
        task,
        event_type="task_status",
        step_key="queue",
        status="pending",
        title="任务已进入队列",
        message="等待 Worker 领取任务",
        progress=0,
    )
    task_input = payload.input_payload or {}
    requested_count = task_input.get("chapter_count") or task_input.get("event_chapter_count") or 6
    try:
        chapter_count = max(1, min(int(requested_count), 12))
    except (TypeError, ValueError):
        chapter_count = 6
    initialize_task_todo(
        db,
        task,
        start_chapter_index=max(1, novel.current_chapter_index + 1),
        chapter_count=chapter_count,
        plan_only=bool(task_input.get("plan_only")),
    )
    notify_persisted_task(db, task)
    return task


def enqueue_standalone_agent_task(db: Session, payload: AgentRunRequest) -> GenerationTask:
    """创建不绑定作品的 Agent 任务，用于样本库等独立模块。"""
    task = GenerationTask(
        novel_id=None,
        chapter_id=payload.chapter_id,
        task_type=payload.task_type,
        status="queued",
        progress=0,
        result_payload={
            "input": payload.input_payload,
            "agent": "RedisTaskQueue",
            "note": "独立任务已进入 Redis 队列，Worker 会分发到对应处理器执行。",
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    emit_task_event(
        db,
        task,
        event_type="task_status",
        step_key="queue",
        status="pending",
        title="任务已进入队列",
        message="等待 Worker 领取独立任务",
        progress=0,
    )
    notify_persisted_task(db, task)
    return task
