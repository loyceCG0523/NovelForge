"""SQLite concurrency coverage for persistent task-event sequencing."""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


API_DIR = Path(__file__).resolve().parents[1] / "backend" / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

def test_sqlite_task_event_sequence_is_safe_across_parallel_sessions(tmp_path, monkeypatch):
    # Do not import app modules during test collection: the desktop full-stack
    # test installs its own temporary DATABASE_URL later in the same process.
    existing_app_modules = {
        name for name in sys.modules if name == "app" or name.startswith("app.")
    }
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("S3_BUCKET", "test")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("JWT_SECRET", "test-secret")

    from app.core.desktop_compat import configure_sqlite_engine
    from app.db.base import Base
    from app.models.generation_task import GenerationTask
    from app.models.generation_task_event import GenerationTaskEvent
    from app.services.task_events import emit_task_event
    import app.models  # noqa: F401

    database_path = tmp_path / "task-events.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_size=8,
        max_overflow=8,
    )
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    task_id = uuid4()
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    try:
        with session_factory() as db:
            db.add(
                GenerationTask(
                    id=task_id,
                    novel_id=None,
                    task_type="generate_story_event",
                    status="running",
                    progress=25,
                    result_payload={},
                )
            )
            db.commit()

        callers = []
        for index in range(worker_count):
            db = session_factory()
            task = db.get(GenerationTask, task_id)
            assert task is not None
            callers.append((index, db, task))

        def emit_from_parallel_session(source) -> int:
            index, db, task = source
            try:
                barrier.wait(timeout=30)
                event = emit_task_event(
                    db,
                    task,
                    step_key=f"parallel_{index}",
                    title=f"并发事件 {index}",
                )
                return event.sequence_no
            finally:
                db.close()

        with patch("app.db.session.SessionLocal", session_factory):
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                sequences = list(executor.map(emit_from_parallel_session, callers))

        assert sorted(sequences) == list(range(1, worker_count + 1))
        with session_factory() as db:
            persisted = db.scalars(
                select(GenerationTaskEvent)
                .where(GenerationTaskEvent.task_id == task_id)
                .order_by(GenerationTaskEvent.sequence_no)
            ).all()
        assert [event.sequence_no for event in persisted] == list(range(1, worker_count + 1))
    finally:
        engine.dispose()
        for name in list(sys.modules):
            if (name == "app" or name.startswith("app.")) and name not in existing_app_modules:
                sys.modules.pop(name, None)
