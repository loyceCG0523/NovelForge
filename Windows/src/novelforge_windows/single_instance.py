"""Single-instance guard shared by the desktop app and its installer."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QLockFile


APP_MUTEX_NAME = "NovelForge.Windows.App"
LOCK_FILE_NAME = "novelforge.lock"


def try_acquire_instance_lock(data_dir: str | Path) -> QLockFile | None:
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(directory / LOCK_FILE_NAME))
    # This lock lives for the full GUI session, so elapsed time must never make
    # a healthy process look stale. Do not force-remove a failed lock here.
    lock.setStaleLockTime(0)
    if lock.tryLock(0):
        return lock
    return None


def create_installer_mutex() -> int | None:
    """Create the Windows mutex checked by Inno Setup's AppMutex setting."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
    return int(handle) if handle else None
