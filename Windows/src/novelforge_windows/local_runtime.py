"""Embedded loopback API and static Web UI runtime for NovelForge Windows."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import Depends
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.responses import PlainTextResponse

from novelforge_windows.config import AppPaths, resolve_app_paths
from novelforge_windows.resources import resource_path


LOCAL_EMAIL = "local@novelforge.desktop"
NEXT_PAGE_RSC_SUFFIX = ".__PAGE__.txt"
LOOPBACK_HOST = "127.0.0.1"
PREFERRED_LOCAL_PORT = 47831


class NextExportStaticFiles(StaticFiles):
    """Serve Next.js export RSC page files using the URLs requested by its client."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 404 and path.endswith(NEXT_PAGE_RSC_SUFFIX):
            nested_path = f"{path[:-len(NEXT_PAGE_RSC_SUFFIX)]}/__PAGE__.txt"
            return await super().get_response(nested_path, scope)
        return response


class DesktopOriginGuard:
    """Reject browser cross-origin access to the privileged desktop API."""

    def __init__(self, app, allowed_origin: str) -> None:
        self.app = app
        self.allowed_origin = allowed_origin

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/api"):
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", ())
            }
            origin = headers.get("origin")
            fetch_site = headers.get("sec-fetch-site", "").lower()
            if (origin and origin != self.allowed_origin) or fetch_site == "cross-site":
                response = PlainTextResponse("Forbidden", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _configure_environment(paths: AppPaths) -> tuple[Path, Path, Path]:
    api_dir = resource_path("backend/api").resolve()
    worker_dir = resource_path("backend/worker").resolve()
    frontend_dir = resource_path("frontend/out").resolve()
    bundled = bool(getattr(sys, "_MEIPASS", None))
    if not bundled and (not api_dir.is_dir() or not worker_dir.is_dir()):
        raise RuntimeError("Windows 本地业务引擎文件不完整，请重新安装 NovelForge。")
    if not (frontend_dir / "index.html").is_file():
        raise RuntimeError("Windows 前端资源尚未构建，请先运行 scripts/build.ps1。")
    for directory in (str(worker_dir), str(api_dir)):
        if Path(directory).is_dir() and directory not in sys.path:
            sys.path.insert(0, directory)

    os.environ.update(
        {
            "NOVELFORGE_DESKTOP": "1",
            "NOVELFORGE_OBJECTS_DIR": str(paths.objects_dir),
            "DATABASE_URL": f"sqlite:///{paths.database_path.as_posix()}",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT": "http://127.0.0.1:1",
            "S3_BUCKET": "novelforge-local",
            "S3_ACCESS_KEY_ID": "local",
            "S3_SECRET_ACCESS_KEY": "local",
            "JWT_SECRET": "novelforge-windows-local",
        }
    )
    return api_dir, worker_dir, frontend_dir


def _ensure_local_user(db):
    from app.models.user import User

    user = db.scalar(select(User).where(User.email == LOCAL_EMAIL))
    if user is None:
        user = User(
            email=LOCAL_EMAIL,
            display_name="本地创作者",
            password_hash="desktop-local",
            plan="local",
            preferences={
                "appearance": {"theme": "ink"},
                "llm": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1-mini",
                    "context_window_tokens": 128000,
                    "api_key": "",
                },
                "review_llm": {"enabled": False},
                "embedding": {"enabled": False},
                "web_search": {"enabled": False},
            },
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _json_dict(value: str) -> dict:
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, dict) else {"legacy_text": str(value or "")}
    except (TypeError, ValueError):
        return {"legacy_text": str(value or "")} if value else {}


def _uuid(value: str) -> UUID:
    return UUID(str(value))


def _migrate_legacy_database(paths: AppPaths, db, user) -> None:
    """Import data created by the first lightweight Windows prototype once."""
    legacy_path = paths.legacy_database_path
    if not legacy_path.is_file() or legacy_path == paths.database_path:
        return
    connection = sqlite3.connect(legacy_path)
    connection.row_factory = sqlite3.Row
    try:
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "projects" not in table_names:
            return

        from app.models.chapter import Chapter
        from app.models.novel import Novel
        from app.models.sample_analysis import SampleAnalysis
        from app.models.story_bible import StoryBible
        from app.core.desktop_secrets import PREFIX

        for row in connection.execute("SELECT * FROM projects ORDER BY created_at"):
            project_id = _uuid(row["id"])
            if db.get(Novel, project_id) is not None:
                continue
            chapter_rows = list(
                connection.execute(
                    "SELECT * FROM chapters WHERE project_id=? ORDER BY sequence_no",
                    (row["id"],),
                )
            )
            protagonist_name = str(row["author"] or "未命名主角").strip()
            novel = Novel(
                id=project_id,
                owner_id=user.id,
                title=row["title"],
                genre=row["genre"],
                status=row["status"],
                target_words=row["target_words"],
                current_chapter_index=max([int(item["sequence_no"]) for item in chapter_rows] or [0]),
                premise=row["brief"],
                brief={
                    "work_type": "长篇小说",
                    "story_era": "未设置（从旧版 Windows 迁移）",
                    "story_location": "",
                    "selling_points": "",
                    "plot_direction": row["brief"],
                    "worldview": "",
                    "chapter_word_min": row["chapter_min_words"],
                    "chapter_word_max": row["chapter_max_words"],
                    "event_chapter_count": 6,
                    "style_reference": "",
                    "forbidden_content": "",
                    "automation_strategy": "",
                    "sample_reference_ids": [],
                    "characters": [
                        {
                            "key": "legacy-protagonist",
                            "name": protagonist_name,
                            "gender": "未设置",
                            "age": "未设置",
                            "occupation": "未设置",
                            "is_protagonist": True,
                            "goal": "",
                            "detailed_setting": "从早期 Windows 本地版迁移",
                        }
                    ],
                    "planned_events": [],
                },
            )
            db.add(novel)
            db.flush()
            for chapter_row in chapter_rows:
                content = str(chapter_row["content"] or "")
                db.add(
                    Chapter(
                        id=_uuid(chapter_row["id"]),
                        novel_id=novel.id,
                        chapter_index=int(chapter_row["sequence_no"]),
                        title=chapter_row["title"],
                        status=chapter_row["status"],
                        word_count=len("".join(content.split())),
                        summary=chapter_row["summary"],
                        content=content,
                        context_snapshot=_json_dict(chapter_row["context_snapshot"]),
                    )
                )
            if row["story_bible"]:
                db.add(
                    StoryBible(
                        novel_id=novel.id,
                        status="draft",
                        source="legacy_windows_migration",
                        summary="从早期 Windows 本地版迁移",
                        content={"legacy_text": row["story_bible"]},
                        locked_fields={},
                    )
                )

        if "sample_documents" in table_names:
            for row in connection.execute("SELECT * FROM sample_documents ORDER BY created_at"):
                sample_id = _uuid(row["id"])
                if db.get(SampleAnalysis, sample_id) is not None:
                    continue
                source_path = Path(row["local_path"])
                object_key = f"users/{user.id}/sample-analyses/{sample_id}/{source_path.name or 'sample.txt'}"
                target = (paths.objects_dir / object_key).resolve()
                target.parent.mkdir(parents=True, exist_ok=True)
                if source_path.is_file() and not target.exists():
                    shutil.copy2(source_path, target)
                payload = target.read_bytes() if target.is_file() else b""
                db.add(
                    SampleAnalysis(
                        id=sample_id,
                        owner_id=user.id,
                        status="completed",
                        sample_title=row["title"],
                        source_author="",
                        source_genre="",
                        source_file_name=row["source_name"],
                        source_object_key=object_key,
                        source_file_size=len(payload),
                        source_word_count=int(row["character_count"] or 0),
                        summary="从早期 Windows 本地版迁移；可点击重新分析并章节化。",
                        metrics={},
                        report={},
                        visibility="private",
                        publication_status="private",
                        reuse_policy="reference_only",
                        rights_declared=False,
                        content_hash=hashlib.sha256(payload).hexdigest() if payload else "",
                    )
                )

        if "app_settings" in table_names:
            settings = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM app_settings")
            }
            encrypted_key = str(settings.get("llm_api_key_dpapi") or "")
            if encrypted_key:
                preferences = dict(user.preferences or {})
                llm = dict(preferences.get("llm") or {})
                llm.update(
                    {
                        "base_url": settings.get("llm_base_url") or llm.get("base_url", ""),
                        "model": settings.get("llm_model") or llm.get("model", ""),
                        "temperature": float(settings.get("llm_temperature") or 0.8),
                        "api_key": PREFIX + encrypted_key,
                    }
                )
                preferences["llm"] = llm
                user.preferences = preferences
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        connection.close()


def _recover_local_tasks(db) -> None:
    from app.models.generation_task import GenerationTask
    from app.services.local_task_queue import submit_local_task

    tasks = db.scalars(
        select(GenerationTask).where(GenerationTask.status.in_(["queued", "running"]))
    ).all()
    for task in tasks:
        task.status = "queued"
        task.error_message = ""
    db.commit()
    for task in tasks:
        submit_local_task(str(task.id), memory=task.task_type == "sync_chapter_memory")


def create_local_app(paths: AppPaths | None = None):
    resolved_paths = paths or resolve_app_paths()
    _, _, frontend_dir = _configure_environment(resolved_paths)

    from app.core.desktop_compat import configure_sqlite_engine  # noqa: F401
    from app.db.base import Base
    from app.db.session import SessionLocal, engine, get_db
    import app.models  # noqa: F401
    from app.core.security import get_current_user
    from app.main import app

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        local_user = _ensure_local_user(db)
        _migrate_legacy_database(resolved_paths, db, local_user)
        _recover_local_tasks(db)

    def get_desktop_user(db=Depends(get_db)):
        return _ensure_local_user(db)

    app.dependency_overrides[get_current_user] = get_desktop_user
    app.routes[:] = [route for route in app.routes if getattr(route, "path", None) != "/"]
    if not any(getattr(route, "name", "") == "desktop-frontend" for route in app.routes):
        app.mount("/", NextExportStaticFiles(directory=frontend_dir, html=True), name="desktop-frontend")
    return app


def bind_local_listener(
    preferred_port: int = PREFERRED_LOCAL_PORT,
) -> tuple[socket.socket, int, bool]:
    """Bind an exclusive loopback listener, falling back to an ephemeral port."""

    candidate_ports = (preferred_port, 0) if preferred_port else (0,)
    last_error: OSError | None = None
    for port in candidate_ports:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LOOPBACK_HOST, port))
            listener.listen(128)
        except OSError as exc:
            listener.close()
            last_error = exc
            continue

        bound_port = int(listener.getsockname()[1])
        return listener, bound_port, bound_port == preferred_port

    assert last_error is not None
    raise last_error


class LocalApplicationServer:
    def __init__(
        self,
        paths: AppPaths | None = None,
        preferred_port: int = PREFERRED_LOCAL_PORT,
    ) -> None:
        self.paths = paths or resolve_app_paths()
        self.socket, self.port, self.using_preferred_port = bind_local_listener(preferred_port)
        self.origin = f"http://{LOOPBACK_HOST}:{self.port}"
        self.url = f"{self.origin}/workbench/"
        desktop_app = DesktopOriginGuard(create_local_app(self.paths), self.origin)
        self.server = uvicorn.Server(
            uvicorn.Config(
                desktop_app,
                host=LOOPBACK_HOST,
                port=self.port,
                log_level="warning",
                log_config=None,
                access_log=False,
            )
        )
        self.thread = threading.Thread(target=self._run, name="NovelForgeLocalApi", daemon=True)

    def _run(self) -> None:
        self.server.run(sockets=[self.socket])

    def start(self, timeout_seconds: float = 15.0) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout_seconds
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.03)
        if not self.server.started:
            raise RuntimeError("NovelForge 本地服务启动失败。")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
