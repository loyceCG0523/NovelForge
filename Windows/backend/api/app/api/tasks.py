"""生成任务接口。

任务用于把前端操作转成后台可消费的 Agent 工作，例如生成章节、同步记忆、反 AI 审校。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_owned_novel
from app.db.session import get_db
from app.models.chapter import Chapter
from app.models.generation_task import GenerationTask
from app.models.generation_task_event import GenerationTaskEvent
from app.models.chapter_revision_patch import ChapterRevisionPatch
from app.models.novel import Novel
from app.models.review_issue import ReviewIssue
from app.models.story_event import StoryEvent
from app.schemas.task import (
    AgentRunRequest,
    ChapterRevisionPatchRead,
    GenerationTaskCreate,
    GenerationTaskEventRead,
    GenerationTaskRead,
)
from app.services.agent_orchestrator import (
    enqueue_agent_task,
    notify_persisted_task,
    retry_chapter_memory_task,
)
from app.services.task_events import emit_task_event


router = APIRouter(prefix="/api/novels/{novel_id}/tasks", tags=["tasks"])


@router.post("", response_model=GenerationTaskRead, status_code=status.HTTP_201_CREATED)
def create_generation_task(
    payload: GenerationTaskCreate,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """直接创建任务记录，不自动入队；主要用于调试或保留手动任务能力。"""
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
    """创建 Agent 任务并推入 Redis 队列，由 Worker 异步执行。"""
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
    """列出作品任务，可按状态筛选 queued/running/completed/failed。"""
    statement = select(GenerationTask).where(GenerationTask.novel_id == novel.id)
    if status_filter:
        statement = statement.where(GenerationTask.status == status_filter)
    statement = statement.order_by(GenerationTask.created_at.desc())
    return list(db.scalars(statement).all())


@router.post(
    "/{task_id}/memory-sync/{memory_task_id}/retry",
    response_model=GenerationTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_failed_memory_sync(
    task_id: UUID,
    memory_task_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """只重试原正文任务中失败的一章记忆/时间线同步。"""
    source_task = db.get(GenerationTask, task_id)
    failed_task = db.get(GenerationTask, memory_task_id)
    if source_task is None or source_task.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Source task not found")
    if failed_task is None or failed_task.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Memory task not found")
    failed_input = (failed_task.result_payload or {}).get("input") or {}
    if (
        failed_task.task_type != "sync_chapter_memory"
        or failed_task.status != "failed"
        or str(failed_input.get("source_task_id") or "") != str(source_task.id)
    ):
        raise HTTPException(status_code=409, detail="Memory task is not retryable")
    chapter = db.get(Chapter, failed_task.chapter_id) if failed_task.chapter_id else None
    if chapter is None or chapter.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return retry_chapter_memory_task(
        db,
        failed_task=failed_task,
        source_task=source_task,
        chapter=chapter,
    )


@router.post(
    "/{task_id}/event-revision/retry",
    response_model=GenerationTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_failed_event_revision(
    task_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """只重试事件复检中补丁生成失败的章节，复用上轮修订蓝图。"""
    source_task = db.get(GenerationTask, task_id)
    if source_task is None or source_task.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Source task not found")
    story_event = db.scalar(
        select(StoryEvent).where(
            StoryEvent.novel_id == novel.id,
            StoryEvent.task_id == source_task.id,
        )
    )
    if story_event is None:
        raise HTTPException(status_code=404, detail="Story event not found")
    source_output = (source_task.result_payload or {}).get("output") or {}
    event_revision = source_output.get("event_revision") or (story_event.payload or {}).get("event_revision") or {}
    retryable = {
        int(index)
        for index in event_revision.get("retryable_chapter_indexes", [])
        if str(index).isdigit()
    }
    if not retryable:
        retryable = {
            int(index)
            for package in event_revision.get("repair_packages", [])
            for index in (package.get("chapter_errors") or {})
            if str(index).isdigit()
        }
    if not retryable:
        raise HTTPException(status_code=409, detail="Event revision has no retryable chapters")

    retry_task = GenerationTask(
        novel_id=novel.id,
        task_type="retry_event_revision",
        status="queued",
        progress=0,
        result_payload={
            "input": {
                "source": "manual_event_revision_retry",
                "source_task_id": str(source_task.id),
                "story_event_id": str(story_event.id),
                "chapter_indexes": sorted(retryable),
            },
            "agent": "EventRevisionRetryAgent",
            "note": "仅重试失败章节，复用原事件修订蓝图。",
        },
    )
    db.add(retry_task)
    db.commit()
    db.refresh(retry_task)
    package_number_by_chapter = {
        int(chapter_index): package_number
        for package_number, package in enumerate(event_revision.get("repair_packages", []), start=1)
        for chapter_index in package.get("requested_chapter_indexes", [])
        if str(chapter_index).isdigit()
    }
    for chapter_index in sorted(retryable):
        emit_task_event(
            db,
            source_task,
            event_type="revision_patch_stream",
            step_key=(
                f"revision_package_{package_number_by_chapter.get(chapter_index, 1)}"
                f"_chapter_{chapter_index}"
            ),
            status="pending",
            title=f"第 {chapter_index} 章事件补丁已重新排队",
            message="将复用原修订蓝图，不重跑其他已成功章节",
            progress=92,
            chapter_index=chapter_index,
            payload={"retry_task_id": str(retry_task.id), "manual_retry": True},
        )
    notify_persisted_task(db, retry_task)
    return retry_task


@router.get("/{task_id}", response_model=GenerationTaskRead)
def get_generation_task(
    task_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> GenerationTask:
    """读取单个任务详情，用于前端查看进度或结果。"""
    task = db.get(GenerationTask, task_id)
    if task is None or task.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/{task_id}/events", response_model=list[GenerationTaskEventRead])
def list_generation_task_events(
    task_id: UUID,
    after_sequence: int = 0,
    limit: int = 300,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[GenerationTaskEvent]:
    """增量读取任务执行事件，支持刷新页面后恢复看板与正文预览。"""
    task = db.get(GenerationTask, task_id)
    if task is None or task.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Task not found")
    safe_limit = max(1, min(limit, 500))
    return list(
        db.scalars(
            select(GenerationTaskEvent)
            .where(
                GenerationTaskEvent.task_id == task.id,
                GenerationTaskEvent.sequence_no > max(0, after_sequence),
            )
            .order_by(GenerationTaskEvent.sequence_no.asc())
            .limit(safe_limit)
        ).all()
    )


@router.get("/{task_id}/revision-patches", response_model=list[ChapterRevisionPatchRead])
def list_generation_task_revision_patches(
    task_id: UUID,
    novel: Novel = Depends(get_owned_novel),
    db: Session = Depends(get_db),
) -> list[ChapterRevisionPatchRead]:
    """读取段落补丁，并附带对应审校问题与修复建议。"""
    task = db.get(GenerationTask, task_id)
    if task is None or task.novel_id != novel.id:
        raise HTTPException(status_code=404, detail="Task not found")
    patches = list(
        db.scalars(
            select(ChapterRevisionPatch)
            .where(
                ChapterRevisionPatch.task_id == task.id,
                ChapterRevisionPatch.status == "applied",
            )
            .order_by(ChapterRevisionPatch.created_at.asc())
        ).all()
    )
    issue_ids = {patch.review_issue_id for patch in patches if patch.review_issue_id is not None}
    issues_by_id = {
        issue.id: issue
        for issue in (
            db.scalars(select(ReviewIssue).where(ReviewIssue.id.in_(issue_ids))).all()
            if issue_ids
            else []
        )
    }
    result: list[ChapterRevisionPatchRead] = []
    for patch in patches:
        issue = issues_by_id.get(patch.review_issue_id)
        issue_payload = (issue.payload or {}) if issue is not None else {}
        result.append(
            ChapterRevisionPatchRead.model_validate(patch).model_copy(
                update={
                    "problem": issue.message if issue is not None else "该段落需要局部调整",
                    "suggestion": str(issue_payload.get("suggestion") or patch.reason or "").strip(),
                    "issue_type": issue.issue_type if issue is not None else "",
                    "severity": issue.severity if issue is not None else "",
                }
            )
        )
    return result
