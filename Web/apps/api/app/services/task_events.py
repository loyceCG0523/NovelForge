"""任务执行事件写入与步骤快照。"""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from typing import Any, Iterator
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.generation_task import GenerationTask
from app.models.generation_task_event import GenerationTaskEvent


_SEQUENCE_ALLOCATION_RETRIES = 4
_SEQUENCE_RETRY_SECONDS = 0.02


class _TaskEventLockPool:
    """Short-lived in-process locks used where SQLite cannot lock a row."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._users: dict[str, int] = {}

    @contextmanager
    def hold(self, task_id: UUID) -> Iterator[None]:
        key = str(task_id)
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
                self._users[key] = 0
            self._users[key] += 1
        try:
            with lock:
                yield
        finally:
            with self._guard:
                remaining = self._users[key] - 1
                if remaining:
                    self._users[key] = remaining
                else:
                    self._users.pop(key, None)
                    self._locks.pop(key, None)


_task_event_locks = _TaskEventLockPool()


def _uses_sqlite(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "sqlite"
    except Exception:
        return False


def _is_task_sequence_conflict(error: IntegrityError) -> bool:
    message = str(getattr(error, "orig", error)).lower()
    return (
        "uq_generation_task_events_task_sequence" in message
        or (
            "generation_task_events" in message
            and "task_id" in message
            and "sequence_no" in message
            and ("unique" in message or "duplicate" in message)
        )
    )


def emit_task_event(
    db: Session,
    task: GenerationTask,
    *,
    event_type: str = "step",
    step_key: str = "",
    status: str = "running",
    title: str = "",
    message: str = "",
    progress: int | None = None,
    chapter_id=None,
    chapter_index: int | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> GenerationTaskEvent:
    """为任务追加事件；sequence_no 让前端可以断线增量恢复。"""
    task_id = task.id
    task_novel_id = task.novel_id
    task_progress = task.progress

    # PostgreSQL can serialize the following allocation with FOR UPDATE. SQLite
    # ignores that clause, and callers may already hold a stale read snapshot.
    # Commit that caller transaction first, then allocate in a fresh Session
    # while a process-local per-task lock covers SELECT MAX + INSERT + COMMIT.
    if _uses_sqlite(db) and commit:
        from app.db.session import SessionLocal

        with _task_event_locks.hold(task_id):
            db.commit()
            with SessionLocal() as event_db:
                return _emit_task_event(
                    event_db,
                    task_id=task_id,
                    novel_id=task_novel_id,
                    task_progress=task_progress,
                    event_type=event_type,
                    step_key=step_key,
                    status=status,
                    title=title,
                    message=message,
                    progress=progress,
                    chapter_id=chapter_id,
                    chapter_index=chapter_index,
                    payload=payload,
                    commit=True,
                )

    return _emit_task_event(
        db,
        task_id=task_id,
        novel_id=task_novel_id,
        task_progress=task_progress,
        event_type=event_type,
        step_key=step_key,
        status=status,
        title=title,
        message=message,
        progress=progress,
        chapter_id=chapter_id,
        chapter_index=chapter_index,
        payload=payload,
        commit=commit,
    )


def _emit_task_event(
    db: Session,
    *,
    task_id: UUID,
    novel_id: UUID | None,
    task_progress: int | None,
    event_type: str,
    step_key: str,
    status: str,
    title: str,
    message: str,
    progress: int | None,
    chapter_id,
    chapter_index: int | None,
    payload: dict[str, Any] | None,
    commit: bool,
) -> GenerationTaskEvent:
    for attempt in range(_SEQUENCE_ALLOCATION_RETRIES):
        # PostgreSQL serializes concurrent workers here. SQLite uses the fresh
        # Session and process-local lock selected by emit_task_event above.
        db.scalar(
            select(GenerationTask.id)
            .where(GenerationTask.id == task_id)
            .with_for_update()
        )
        sequence_no = int(
            db.scalar(
                select(func.coalesce(func.max(GenerationTaskEvent.sequence_no), 0)).where(
                    GenerationTaskEvent.task_id == task_id
                )
            )
            or 0
        ) + 1
        event = GenerationTaskEvent(
            novel_id=novel_id,
            task_id=task_id,
            sequence_no=sequence_no,
            event_type=str(event_type or "step")[:50],
            step_key=str(step_key or "")[:100],
            status=str(status or "info")[:30],
            title=str(title or "")[:200],
            message=str(message or ""),
            progress=max(0, min(int(progress if progress is not None else task_progress or 0), 100)),
            chapter_id=chapter_id,
            chapter_index=chapter_index,
            payload=payload or {},
        )
        db.add(event)
        try:
            if commit:
                db.commit()
                db.refresh(event)
            else:
                db.flush()
            return event
        except IntegrityError as error:
            if not commit or not _is_task_sequence_conflict(error) or attempt + 1 >= _SEQUENCE_ALLOCATION_RETRIES:
                raise
            db.rollback()
            time.sleep(_SEQUENCE_RETRY_SECONDS * (attempt + 1))

    raise RuntimeError("Task event sequence allocation retries were exhausted.")


class ChapterPreviewPublisher:
    """把高频 token 增量压成低频、可恢复的正文预览快照。"""

    def __init__(
        self,
        db: Session,
        task: GenerationTask,
        chapter_index: int,
        *,
        min_chars: int = 240,
    ) -> None:
        self.db = db
        self.task = task
        self.chapter_index = chapter_index
        self.min_chars = min_chars
        self.text = ""
        self.last_emitted_length = 0

    def reset(self, attempt: int) -> None:
        self.text = ""
        self.last_emitted_length = 0
        emit_task_event(
            self.db,
            self.task,
            event_type="chapter_preview_reset",
            step_key=f"chapter_{self.chapter_index}_draft",
            status="running",
            title=f"第 {self.chapter_index} 章正在生成",
            chapter_index=self.chapter_index,
            payload={"attempt": attempt, "text": ""},
        )

    def append(self, delta: str, *, attempt: int = 1, force: bool = False) -> None:
        self.text += delta
        if not force and len(self.text) - self.last_emitted_length < self.min_chars:
            return
        emit_task_event(
            self.db,
            self.task,
            event_type="chapter_preview",
            step_key=f"chapter_{self.chapter_index}_draft",
            status="running",
            title=f"第 {self.chapter_index} 章正文生成中",
            message=f"已接收约 {len(self.text)} 个字符",
            chapter_index=self.chapter_index,
            payload={"attempt": attempt, "text": self.text, "char_count": len(self.text)},
        )
        self.last_emitted_length = len(self.text)

    def finish(self, text: str, *, chapter_id=None, attempt: int = 1) -> None:
        self.text = text
        emit_task_event(
            self.db,
            self.task,
            event_type="chapter_preview",
            step_key=f"chapter_{self.chapter_index}_draft",
            status="completed",
            title=f"第 {self.chapter_index} 章正文已生成",
            message=f"正文约 {len(text)} 个字符，正在执行后处理",
            chapter_id=chapter_id,
            chapter_index=self.chapter_index,
            payload={"attempt": attempt, "text": text, "char_count": len(text), "final": True},
        )


class ModelThinkingPublisher:
    """把模型 reasoning_content 合并为低频增量事件，供前端实时展示。"""

    def __init__(
        self,
        task: GenerationTask | UUID,
        *,
        source_step_key: str,
        model_role: str,
        model: str,
        title: str,
        chapter_index: int | None = None,
        min_chars: int = 480,
        min_interval_seconds: float = 1.2,
    ) -> None:
        self.task_id = task.id if isinstance(task, GenerationTask) else task
        self.source_step_key = source_step_key
        self.model_role = "reviewer" if model_role == "reviewer" else "writer"
        self.model = str(model or "")
        self.title = str(title or "模型思考")
        self.chapter_index = chapter_index
        self.min_chars = max(80, int(min_chars))
        self.min_interval_seconds = max(0.2, float(min_interval_seconds))
        self.stream_id = str(uuid4())
        self.attempt = 0
        self.pending = ""
        self.total_chars = 0
        self.output_chars = 0
        self.started = False
        self.finished = False
        self.last_emitted_at = time.monotonic()
        self._lock = threading.RLock()

    def _emit(
        self,
        *,
        event_type: str,
        status: str,
        delta: str = "",
        activity: dict[str, Any] | None = None,
    ) -> None:
        # 事件可能来自并行 LLM 线程；每次使用独立 Session，避免跨线程复用事务。
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            task = db.get(GenerationTask, self.task_id)
            if task is None:
                return
            emit_task_event(
                db,
                task,
                event_type=event_type,
                step_key=f"model_thinking_{self.stream_id}",
                status=status,
                title=self.title,
                message=(
                    f"已接收 {self.total_chars} 个思考字符"
                    if self.total_chars
                    else "等待模型返回思考内容"
                ),
                chapter_index=self.chapter_index,
                payload={
                    "stream_id": self.stream_id,
                    "source_step_key": self.source_step_key,
                    "model_role": self.model_role,
                    "model": self.model,
                    "label": self.title,
                    "attempt": self.attempt,
                    "delta": delta,
                    "total_chars": self.total_chars,
                    "output_chars": self.output_chars,
                    "elapsed_seconds": (activity or {}).get("elapsed_seconds"),
                },
            )

    def start(self, *, attempt: int = 1) -> None:
        with self._lock:
            if self.started and not self.finished and self.attempt == attempt:
                return
            self.attempt = max(1, int(attempt))
            self.pending = ""
            self.total_chars = 0
            self.output_chars = 0
            self.started = True
            self.finished = False
            self.last_emitted_at = time.monotonic()
            self._emit(event_type="model_thinking_reset", status="running")

    def append_activity(self, activity: dict[str, Any]) -> None:
        delta = str(activity.get("reasoning_delta") or "")
        transport_attempt = max(
            1,
            int(activity.get("generation_attempt") or activity.get("attempt") or 1),
        )
        with self._lock:
            if not self.started:
                self.start(attempt=transport_attempt)
            elif transport_attempt != self.attempt:
                self._flush_locked(activity=activity)
                self.attempt = transport_attempt
                self.pending = ""
                self.total_chars = 0
                self.output_chars = 0
                self.last_emitted_at = time.monotonic()
                self._emit(event_type="model_thinking_reset", status="running", activity=activity)
            self.output_chars = int(activity.get("output_chars") or self.output_chars)
            if delta:
                self.pending += delta
                self.total_chars += len(delta)
            elapsed = time.monotonic() - self.last_emitted_at
            if self.pending and (
                len(self.pending) >= self.min_chars
                or elapsed >= self.min_interval_seconds
            ):
                self._flush_locked(activity=activity)

    def _flush_locked(self, *, activity: dict[str, Any] | None = None) -> None:
        if not self.pending:
            return
        delta = self.pending
        self.pending = ""
        self.last_emitted_at = time.monotonic()
        self._emit(
            event_type="model_thinking_delta",
            status="running",
            delta=delta,
            activity=activity,
        )

    def finish(self, *, status: str = "completed") -> None:
        with self._lock:
            if self.finished:
                return
            if not self.started:
                self.attempt = 1
                self.started = True
                self._emit(event_type="model_thinking_reset", status="running")
            self._flush_locked()
            self.finished = True
            self._emit(event_type="model_thinking_end", status=status)


def initialize_task_todo(
    db: Session,
    task: GenerationTask,
    *,
    start_chapter_index: int = 1,
    chapter_count: int = 0,
    plan_only: bool = False,
) -> None:
    """为事件生成任务预先写入可见待办，后续同 step_key 事件会覆盖其状态。"""
    if task.task_type != "generate_story_event":
        return
    steps: list[tuple[str, str, int | None]] = [
        ("event_plan", "规划剧情事件", None),
        ("event_research", "检索情节写法与搞笑话术", None),
    ]
    if plan_only:
        steps.append(("event_plan_confirmation", "等待确认章节计划", None))
    else:
        steps.append(("memory_sync", "后台记忆同步总进度", None))
        for offset in range(max(0, chapter_count)):
            chapter_index = start_chapter_index + offset
            steps.extend(
                [
                    (f"chapter_{chapter_index}_draft", f"生成第 {chapter_index} 章", chapter_index),
                    (f"chapter_{chapter_index}_review", f"审校第 {chapter_index} 章", chapter_index),
                    (f"chapter_{chapter_index}_revision", f"定向修改第 {chapter_index} 章", chapter_index),
                    (f"chapter_{chapter_index}_fact_delta", f"同步第 {chapter_index} 章轻量事实", chapter_index),
                    (f"chapter_{chapter_index}_memory_sync", f"后台同步第 {chapter_index} 章记忆", chapter_index),
                ]
            )
        steps.extend(
            [
                ("event_review_initial", "执行事件级统一审校", None),
                ("event_review_final", "复检局部修订结果", None),
            ]
        )
    for step_key, title, chapter_index in steps:
        emit_task_event(
            db,
            task,
            event_type="step",
            step_key=step_key,
            status="pending",
            title=title,
            message="等待前置步骤完成",
            progress=0,
            chapter_index=chapter_index,
        )
