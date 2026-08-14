"""NovelForge 本地项目控制台。

这个进程独立于业务 API/Web/Worker，负责用固定命令启动、停止、监控四个服务，
并通过 localhost 网页展示健康状态、Redis 队列和日志。
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from redis import Redis


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
RUNTIME_DIR = ROOT / ".runtime" / "supervisor"
LOG_DIR = RUNTIME_DIR / "logs"
STATE_PATH = RUNTIME_DIR / "state.json"
SUPERVISOR_LOG = LOG_DIR / "supervisor.log"
CONSOLE_URL = "http://127.0.0.1:3900"
APP_URL = "http://127.0.0.1:3000"
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
STILL_ACTIVE = 259

RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging_handlers: list[logging.Handler] = [
    logging.FileHandler(SUPERVISOR_LOG, encoding="utf-8"),
]
if sys.stderr is not None:
    logging_handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=logging_handlers,
)
LOGGER = logging.getLogger("novelforge-supervisor")


def _python_executable() -> str:
    configured = os.environ.get("NOVELFORGE_PYTHON", "").strip()
    candidates = [
        configured,
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        sys.executable,
        r"D:\Anaconda3\envs\novelforge-api\python.exe",
        shutil.which("python") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            resolved = Path(candidate).resolve()
            # pythonw 没有标准输出句柄，Uvicorn/Worker 的日志初始化可能直接失败。
            if resolved.name.lower() == "pythonw.exe":
                console_python = resolved.with_name("python.exe")
                if console_python.exists():
                    resolved = console_python
            return str(resolved)
    raise RuntimeError("找不到可用的 Python，请设置 NOVELFORGE_PYTHON")


def _npm_executable() -> str:
    candidate = shutil.which("npm.cmd") or shutil.which("npm")
    if not candidate:
        raise RuntimeError("找不到 npm，请确认 Node.js 已加入 PATH")
    return candidate


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    title: str
    description: str
    command: tuple[str, ...]
    cwd: str
    health_url: str = ""
    open_url: str = ""


@dataclass
class ProcessRecord:
    pid: int
    started_at: str
    log_path: str


@dataclass
class OperationStep:
    key: str
    label: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    message: str = ""


@dataclass
class OperationRecord:
    id: str
    action: Literal["start", "stop", "restart"]
    status: Literal["running", "completed", "failed"]
    steps: list[OperationStep]
    current_step: str
    started_at: str
    updated_at: str
    error: str = ""


def _build_specs() -> dict[str, ServiceSpec]:
    python = _python_executable()
    npm = _npm_executable()
    return {
        "web": ServiceSpec(
            key="web",
            title="前端 Web",
            description="Next.js 用户界面",
            command=(npm, "run", "dev"),
            cwd=str(ROOT / "apps" / "web"),
            health_url=APP_URL,
            open_url=APP_URL,
        ),
        "api": ServiceSpec(
            key="api",
            title="后端 API",
            description="FastAPI、数据库与业务接口",
            command=(
                python,
                "-m",
                "uvicorn",
                "app.main:app",
                "--reload",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ),
            cwd=str(ROOT / "apps" / "api"),
            health_url="http://127.0.0.1:8000/api/health",
            open_url="http://127.0.0.1:8000/docs",
        ),
        "agent_worker": ServiceSpec(
            key="agent_worker",
            title="正文 Worker",
            description="生成、审校与事件任务队列",
            command=(python, "-m", "worker.main", "--queue", "agent"),
            cwd=str(ROOT / "apps" / "worker"),
        ),
        "memory_worker": ServiceSpec(
            key="memory_worker",
            title="Memory Worker",
            description="结构化记忆与时间线后台队列",
            command=(python, "-m", "worker.main", "--queue", "memory"),
            cwd=str(ROOT / "apps" / "worker"),
        ),
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _pid_creation_time(pid: int) -> datetime | None:
    """读取 Windows 进程创建时间，避免状态文件中的旧 PID 命中无关进程。"""
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        unix_seconds = ticks / 10_000_000 - 11_644_473_600
        return datetime.fromtimestamp(unix_seconds, timezone.utc)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _pid_matches_record(record: ProcessRecord) -> bool:
    if not _pid_alive(record.pid):
        return False
    recorded_at = _parse_time(record.started_at)
    created_at = _pid_creation_time(record.pid)
    if recorded_at is None or created_at is None:
        return False
    return abs((recorded_at - created_at).total_seconds()) <= 10


def _health_ok(url: str, timeout: float = 1.2) -> bool:
    if not url:
        return False
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "NovelForge-Supervisor"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class ProcessSupervisor:
    def __init__(self) -> None:
        self.specs = _build_specs()
        self.records: dict[str, ProcessRecord] = {}
        self.transitions: dict[str, str] = {}
        self.lock = threading.RLock()
        self._load_state()

    def _load_state(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key, item in (raw.get("services") or {}).items():
            if key not in self.specs or not isinstance(item, dict):
                continue
            try:
                record = ProcessRecord(
                    pid=int(item["pid"]),
                    started_at=str(item["started_at"]),
                    log_path=str(item["log_path"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if _pid_matches_record(record):
                self.records[key] = record
        self._save_state()

    def _save_state(self) -> None:
        payload = {
            "updated_at": _utc_now(),
            "services": {key: asdict(record) for key, record in self.records.items()},
        }
        temp_path = STATE_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(STATE_PATH)

    def _log_path(self, key: str) -> Path:
        return LOG_DIR / f"{key}.log"

    def _append_lifecycle(self, key: str, message: str) -> None:
        spec = self._require_spec(key)
        path = self._log_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{timestamp}] lifecycle · {spec.title} · {message}\n")

    def _record_alive(self, key: str) -> bool:
        record = self.records.get(key)
        if record is None:
            return False
        if _pid_matches_record(record):
            return True
        self._append_lifecycle(key, f"进程意外退出（PID {record.pid}）")
        self.records.pop(key, None)
        self._save_state()
        return False

    def start(self, key: str) -> dict[str, Any]:
        with self.lock:
            spec = self._require_spec(key)
            if self._record_alive(key):
                current = self.service_status(key)
                record = self.records.get(key)
                started_at = _parse_time(record.started_at) if record else None
                age_seconds = (
                    (datetime.now(timezone.utc) - started_at).total_seconds()
                    if started_at
                    else 0
                )
                if not spec.health_url or current["status"] == "running" or age_seconds < 30:
                    return current
                LOGGER.warning(
                    "%s pid=%s remained unhealthy for %.1fs; restarting",
                    key,
                    record.pid if record else "?",
                    age_seconds,
                )
                self.stop(key)
            if spec.health_url and _health_ok(spec.health_url):
                raise RuntimeError(f"{spec.title} 端口已被控制台外部进程占用，请先停止旧进程")

            log_path = self._log_path(key)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._append_lifecycle(key, "正在启动")
            log_handle = log_path.open("a", encoding="utf-8", buffering=1)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            try:
                process = subprocess.Popen(
                    list(spec.command),
                    cwd=spec.cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
                    close_fds=False,
                )
            except Exception as exc:
                self._append_lifecycle(key, f"启动失败：{exc}")
                raise
            finally:
                log_handle.close()
            self.records[key] = ProcessRecord(
                pid=process.pid,
                started_at=_utc_now(),
                log_path=str(log_path),
            )
            self._save_state()
            LOGGER.info("Started %s pid=%s", key, process.pid)
            return self.service_status(key)

    def stop(self, key: str) -> dict[str, Any]:
        with self.lock:
            spec = self._require_spec(key)
            record = self.records.get(key)
            if record is None or not _pid_matches_record(record):
                self.records.pop(key, None)
                self._save_state()
                return self.service_status(key)
            LOGGER.info("Stopping %s pid=%s", key, record.pid)
            self.transitions[key] = "stopping"
            subprocess.run(
                ["taskkill", "/PID", str(record.pid), "/T"],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            deadline = time.time() + 6
            while _pid_alive(record.pid) and time.time() < deadline:
                time.sleep(0.15)
            forced = False
            if _pid_alive(record.pid):
                forced = True
                subprocess.run(
                    ["taskkill", "/PID", str(record.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=12,
                    check=False,
                )
                time.sleep(0.25)
            if _pid_alive(record.pid):
                self.transitions.pop(key, None)
                self._append_lifecycle(key, f"停止失败，进程仍在运行（PID {record.pid}）")
                raise RuntimeError(f"{spec.title} 停止失败，进程仍在运行")
            self.records.pop(key, None)
            self._save_state()
            self.transitions.pop(key, None)
            stop_mode = "已强制停止" if forced else "已停止"
            self._append_lifecycle(key, f"{stop_mode}（原 PID {record.pid}）")
            return self.service_status(key)

    def restart(self, key: str) -> dict[str, Any]:
        self.stop(key)
        time.sleep(0.35)
        return self.start(key)

    def start_all(self) -> list[dict[str, Any]]:
        results = []
        infrastructure_error = self.ensure_infrastructure()
        if infrastructure_error:
            LOGGER.error("Infrastructure startup failed: %s", infrastructure_error)
        # API 先于 Web 和 Worker 启动，日志更容易判断依赖问题。
        for key in ("api", "web", "agent_worker", "memory_worker"):
            try:
                results.append(self.start(key))
            except RuntimeError as exc:
                results.append({**self.service_status(key), "error": str(exc)})
        return results

    def ensure_infrastructure(self) -> str:
        """确保 PostgreSQL、Redis、MinIO 已由 Docker Compose 拉起。"""
        docker = shutil.which("docker.exe") or shutil.which("docker")
        if not docker:
            return "找不到 docker 命令，请先启动 Docker Desktop"
        try:
            result = subprocess.run(
                [docker, "compose", "up", "-d"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=135,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        if output:
            LOGGER.info("docker compose up -d:\n%s", output)
        if result.returncode != 0:
            return output or f"docker compose exited with {result.returncode}"
        return ""

    def wait_until_ready(self, key: str, timeout: float = 45) -> bool:
        spec = self._require_spec(key)
        if not spec.health_url:
            time.sleep(1)
            return self.service_status(key)["status"] == "running"
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.service_status(key)["status"]
            if status == "running":
                return True
            if status == "stopped":
                return False
            time.sleep(0.35)
        return False

    def stop_all(self) -> list[dict[str, Any]]:
        results = []
        for key in ("memory_worker", "agent_worker", "web", "api"):
            results.append(self.stop(key))
        return results

    def restart_all(self) -> list[dict[str, Any]]:
        self.stop_all()
        time.sleep(0.5)
        return self.start_all()

    def _require_spec(self, key: str) -> ServiceSpec:
        spec = self.specs.get(key)
        if spec is None:
            raise HTTPException(status_code=404, detail="Unknown service")
        return spec

    def service_status(self, key: str) -> dict[str, Any]:
        with self.lock:
            spec = self._require_spec(key)
            managed_alive = self._record_alive(key)
            record = self.records.get(key)
            health_ok = _health_ok(spec.health_url) if spec.health_url else managed_alive
            if managed_alive:
                status = "running" if health_ok else ("starting" if spec.health_url else "running")
            elif health_ok:
                status = "external"
            else:
                status = "stopped"
            uptime_seconds = 0
            if record and managed_alive:
                started_at = _parse_time(record.started_at)
                if started_at:
                    uptime_seconds = max(
                        0,
                        int((datetime.now(timezone.utc) - started_at).total_seconds()),
                    )
            log_path = self._log_path(key)
            stat = log_path.stat() if log_path.exists() else None
            return {
                "key": key,
                "title": spec.title,
                "description": spec.description,
                "status": status,
                "pid": record.pid if record and managed_alive else None,
                "uptime_seconds": uptime_seconds,
                "health_url": spec.health_url,
                "open_url": spec.open_url,
                "managed": managed_alive,
                "log_bytes": stat.st_size if stat else 0,
                "log_updated_at": (
                    datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                    if stat
                    else ""
                ),
            }

    def overview(self) -> dict[str, Any]:
        services = [self.service_status(key) for key in self.specs]
        running = sum(item["status"] == "running" for item in services)
        external = sum(item["status"] == "external" for item in services)
        overall = (
            "running"
            if running == len(services)
            else "partial"
            if running or external
            else "stopped"
        )
        return {
            "overall": overall,
            "services": services,
            "queues": self.queue_status(),
            "updated_at": _utc_now(),
            "app_url": APP_URL,
        }

    def queue_status(self) -> dict[str, Any]:
        env = dotenv_values(ROOT / ".env")
        redis_url = str(env.get("REDIS_URL") or "redis://127.0.0.1:6379/0")
        agent_queue = str(env.get("AGENT_TASK_QUEUE") or "novelforge:agent_tasks")
        memory_queue = str(env.get("MEMORY_TASK_QUEUE") or "novelforge:memory_tasks")
        try:
            client = Redis.from_url(redis_url, decode_responses=True, socket_timeout=0.9)
            return {
                "status": "connected",
                "agent": int(client.llen(agent_queue)),
                "memory": int(client.llen(memory_queue)),
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "agent": None,
                "memory": None,
                "message": str(exc)[:160],
            }

    def tail_log(self, key: str, lines: int) -> dict[str, Any]:
        spec = self._require_spec(key)
        transition = self.transitions.get(key)
        service_status = (
            {"status": transition}
            if transition
            else self.service_status(key)
        )
        path = self._log_path(key)
        if not path.exists():
            return {
                "service": key,
                "title": spec.title,
                "status": service_status["status"],
                "lines": [],
                "text": "",
                "updated_at": "",
            }
        safe_lines = max(20, min(lines, 1000))
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            block_size = min(size, 256_000)
            handle.seek(max(0, size - block_size))
            text = handle.read().decode("utf-8", errors="replace")
        selected = text.splitlines()[-safe_lines:]
        stat = path.stat()
        return {
            "service": key,
            "title": spec.title,
            "status": service_status["status"],
            "lines": selected,
            "text": "\n".join(selected),
            "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    def clear_log(self, key: str) -> None:
        self._require_spec(key)
        path = self._log_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


SUPERVISOR = ProcessSupervisor()


class OperationManager:
    """将整组启停放到后台线程，并向前端暴露可轮询的真实步骤。"""

    def __init__(self, supervisor: ProcessSupervisor) -> None:
        self.supervisor = supervisor
        self.lock = threading.RLock()
        self.current: OperationRecord | None = None

    def start(self, action: Literal["start", "stop", "restart"]) -> dict[str, Any]:
        with self.lock:
            if self.current and self.current.status == "running":
                raise RuntimeError("已有整组操作正在执行，请等待当前操作完成")
            steps = [
                OperationStep(key=key, label=label)
                for key, label, _operation in self._build_steps(action)
            ]
            now = _utc_now()
            self.current = OperationRecord(
                id=uuid4().hex,
                action=action,
                status="running",
                steps=steps,
                current_step=steps[0].label if steps else "",
                started_at=now,
                updated_at=now,
            )
            snapshot = self._snapshot_unlocked()
        threading.Thread(
            target=self._run,
            args=(action,),
            daemon=True,
            name=f"novelforge-{action}-operation",
        ).start()
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            if self.current is None:
                return {
                    "id": "",
                    "action": "",
                    "status": "idle",
                    "steps": [],
                    "current_step": "",
                    "started_at": "",
                    "updated_at": _utc_now(),
                    "error": "",
                    "percent": 0,
                    "completed_steps": 0,
                    "total_steps": 0,
                }
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        assert self.current is not None
        processed = sum(
            step.status in {"completed", "failed"} for step in self.current.steps
        )
        running = any(step.status == "running" for step in self.current.steps)
        total = len(self.current.steps)
        fractional = processed + (0.35 if running else 0)
        percent = round((fractional / total) * 100) if total else 0
        if self.current.status in {"completed", "failed"}:
            percent = 100
        return {
            **asdict(self.current),
            "percent": min(percent, 100),
            "completed_steps": processed,
            "total_steps": total,
        }

    def _build_steps(
        self,
        action: Literal["start", "stop", "restart"],
    ) -> list[tuple[str, str, Any]]:
        start_steps: list[tuple[str, str, Any]] = [
            ("infrastructure", "启动基础服务", self._start_infrastructure),
            ("api", "启动后端 API", lambda: self._start_service("api")),
            ("web", "启动前端 Web", lambda: self._start_service("web")),
            (
                "agent_worker",
                "启动正文 Worker",
                lambda: self._start_service("agent_worker"),
            ),
            (
                "memory_worker",
                "启动 Memory Worker",
                lambda: self._start_service("memory_worker"),
            ),
        ]
        stop_steps: list[tuple[str, str, Any]] = [
            (
                "memory_worker",
                "停止 Memory Worker",
                lambda: self._stop_service("memory_worker"),
            ),
            (
                "agent_worker",
                "停止正文 Worker",
                lambda: self._stop_service("agent_worker"),
            ),
            ("web", "停止前端 Web", lambda: self._stop_service("web")),
            ("api", "停止后端 API", lambda: self._stop_service("api")),
        ]
        if action == "start":
            return start_steps
        if action == "stop":
            return stop_steps
        return stop_steps + start_steps

    def _start_infrastructure(self) -> str:
        error = self.supervisor.ensure_infrastructure()
        if error:
            raise RuntimeError(error)
        return "PostgreSQL、Redis、MinIO 已就绪"

    def _start_service(self, key: str) -> str:
        status = self.supervisor.start(key)
        if not self.supervisor.wait_until_ready(key):
            raise RuntimeError(f"{status['title']} 未在限定时间内就绪")
        return f"{status['title']} 已运行"

    def _stop_service(self, key: str) -> str:
        status = self.supervisor.stop(key)
        if status["status"] != "stopped":
            raise RuntimeError(f"{status['title']} 由外部进程运行，控制台无法停止")
        return f"{status['title']} 已停止"

    def _run(self, action: Literal["start", "stop", "restart"]) -> None:
        errors: list[str] = []
        for index, (_key, _label, operation) in enumerate(self._build_steps(action)):
            with self.lock:
                if self.current is None:
                    return
                step = self.current.steps[index]
                step.status = "running"
                self.current.current_step = step.label
                self.current.updated_at = _utc_now()
            try:
                message = str(operation() or "")
            except Exception as exc:
                message = str(exc)
                errors.append(f"{step.label}：{message}")
                LOGGER.exception("Operation %s step failed: %s", action, step.label)
                step_status: Literal["completed", "failed"] = "failed"
            else:
                step_status = "completed"
            with self.lock:
                if self.current is None:
                    return
                step = self.current.steps[index]
                step.status = step_status
                step.message = message
                self.current.updated_at = _utc_now()
        with self.lock:
            if self.current is None:
                return
            self.current.status = "failed" if errors else "completed"
            self.current.current_step = "操作完成" if not errors else "操作完成，但有步骤失败"
            self.current.error = "\n".join(errors)
            self.current.updated_at = _utc_now()


OPERATIONS = OperationManager(SUPERVISOR)
app = FastAPI(title="NovelForge Local Supervisor", docs_url=None, redoc_url=None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")


@app.get("/app.js")
def javascript() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@app.get("/api/status")
def status() -> dict[str, Any]:
    return SUPERVISOR.overview()


@app.post("/api/all/{action}")
def control_all(action: Literal["start", "stop", "restart"]) -> dict[str, Any]:
    try:
        return {"operation": OPERATIONS.start(action)}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/operations/current")
def current_operation() -> dict[str, Any]:
    return OPERATIONS.snapshot()


@app.post("/api/services/{key}/{action}")
def control_service(
    key: str,
    action: Literal["start", "stop", "restart"],
) -> dict[str, Any]:
    try:
        result = {
            "start": SUPERVISOR.start,
            "stop": SUPERVISOR.stop,
            "restart": SUPERVISOR.restart,
        }[action](key)
        return {"action": action, "service": result}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/logs/{key}")
def logs(key: str, lines: int = Query(300, ge=20, le=1000)) -> dict[str, Any]:
    return SUPERVISOR.tail_log(key, lines)


@app.post("/api/logs/{key}/clear")
def clear_logs(key: str) -> dict[str, str]:
    SUPERVISOR.clear_log(key)
    return {"status": "cleared"}


def _open_browser_later() -> None:
    time.sleep(1.2)
    webbrowser.open(CONSOLE_URL)


def main() -> None:
    parser = argparse.ArgumentParser(description="NovelForge 本地项目控制台")
    parser.add_argument("--auto-start", action="store_true", help="控制台启动时拉起全部服务")
    parser.add_argument("--open-browser", action="store_true", help="自动打开控制台网页")
    args = parser.parse_args()
    if args.auto_start:
        OPERATIONS.start("start")
    if args.open_browser:
        threading.Thread(target=_open_browser_later, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=3900, log_level="warning")


if __name__ == "__main__":
    main()
