"""In-process persistent task queue for the standalone desktop edition."""

from __future__ import annotations

import queue
import threading


_agent_queue: queue.Queue[str] = queue.Queue()
_memory_queue: queue.Queue[str] = queue.Queue()
_started = False
_lock = threading.Lock()


def _run(selected_queue: queue.Queue[str]) -> None:
    from app.db.session import SessionLocal
    from worker.main import execute_task

    while True:
        task_id = selected_queue.get()
        try:
            with SessionLocal() as db:
                execute_task(db, task_id)
        except Exception as exc:
            print(f"Local task failed before completion: {task_id} - {exc}")
        finally:
            selected_queue.task_done()


def start_local_workers() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_run, args=(_agent_queue,), name="NovelForgeAgent", daemon=True).start()
        threading.Thread(target=_run, args=(_memory_queue,), name="NovelForgeMemory", daemon=True).start()


def submit_local_task(task_id: str, *, memory: bool = False) -> None:
    start_local_workers()
    (_memory_queue if memory else _agent_queue).put(str(task_id))
