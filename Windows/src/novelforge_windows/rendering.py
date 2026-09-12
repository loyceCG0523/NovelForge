"""Qt WebEngine rendering policy for the Windows desktop shell.

The packaged application deliberately uses Chromium software rendering on every
machine. This avoids the Qt WebEngine -> DWM shared-texture presentation path,
which can leave torn or stale frames on otherwise healthy GPU drivers.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import MutableSequence

from novelforge_windows.config import APP_NAME, DATA_DIR_ENV


CHROMIUM_FLAGS_ENV = "QTWEBENGINE_CHROMIUM_FLAGS"
EFFECTIVE_RENDERING_ENV = "NOVELFORGE_EFFECTIVE_RENDERING"
RECOVERY_RENDERING_ENV = "NOVELFORGE_RECOVERY_RENDERING"
SAFE_RENDERING_ARGUMENT = "--safe-rendering"
RECOVERY_MARKER_NAME = "webengine-safe-rendering-next-launch.json"

SOFTWARE_MODE = "software"


def _contains_disable_gpu(flags: str) -> bool:
    return "--disable-gpu" in flags.split()


def _resolve_data_dir(data_dir: str | Path | None = None) -> Path | None:
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    override = os.getenv(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return None
    return (Path(local_app_data) / APP_NAME).resolve()


def recovery_marker_path(data_dir: str | Path | None = None) -> Path | None:
    resolved = _resolve_data_dir(data_dir)
    return resolved / RECOVERY_MARKER_NAME if resolved is not None else None


def has_gpu_recovery_request(data_dir: str | Path | None = None) -> bool:
    marker = recovery_marker_path(data_dir)
    if marker is None:
        return False
    try:
        return marker.is_file()
    except OSError:
        return False


def request_safe_rendering_once(
    data_dir: str | Path,
    *,
    reason: str,
    exit_code: int,
) -> bool:
    """Request software rendering for one future launch without changing user settings."""
    marker = recovery_marker_path(data_dir)
    if marker is None:
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        if marker.is_file():
            return False
        temporary = marker.with_suffix(marker.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "reason": reason,
                    "exit_code": int(exit_code),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, marker)
        return True
    except OSError:
        return False


def consume_gpu_recovery_request(data_dir: str | Path | None = None) -> bool:
    """Consume the one-shot marker after this process owns the single-instance lock."""
    marker = recovery_marker_path(data_dir)
    if marker is None:
        return False
    try:
        marker.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def configure_webengine_rendering(
    argv: MutableSequence[str] | None = None,
    *,
    data_dir: str | Path | None = None,
) -> str:
    """Force Chromium software rendering before importing Qt WebEngine.

    ``--safe-rendering`` remains accepted so existing shortcuts keep working,
    but it is now equivalent to the normal startup path. No environment switch
    can re-enable GPU rendering for the packaged application.
    """
    arguments = sys.argv if argv is None else argv
    safe_argument = SAFE_RENDERING_ARGUMENT in arguments
    if safe_argument:
        arguments[:] = [item for item in arguments if item != SAFE_RENDERING_ARGUMENT]

    flags = os.getenv(CHROMIUM_FLAGS_ENV, "").strip()
    if not _contains_disable_gpu(flags):
        flags = f"{flags} --disable-gpu".strip()
    os.environ[CHROMIUM_FLAGS_ENV] = flags
    os.environ[EFFECTIVE_RENDERING_ENV] = SOFTWARE_MODE

    # Keep one-shot recovery markers consumable after upgrades from releases
    # that used GPU rendering, even though every launch is now already safe.
    recovery_requested = has_gpu_recovery_request(data_dir)
    if recovery_requested:
        os.environ[RECOVERY_RENDERING_ENV] = "1"
    else:
        os.environ.pop(RECOVERY_RENDERING_ENV, None)
    return SOFTWARE_MODE


def software_rendering_active() -> bool:
    if os.getenv(EFFECTIVE_RENDERING_ENV) == SOFTWARE_MODE:
        return True
    return _contains_disable_gpu(os.getenv(CHROMIUM_FLAGS_ENV, ""))


def recovery_rendering_active() -> bool:
    return os.getenv(RECOVERY_RENDERING_ENV) == "1"
