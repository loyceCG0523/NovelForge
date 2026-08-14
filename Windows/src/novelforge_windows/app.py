"""Desktop application bootstrap."""

from __future__ import annotations

import ctypes
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from novelforge_windows.config import resolve_app_paths
from novelforge_windows.local_runtime import LocalApplicationServer
from novelforge_windows.rendering import consume_gpu_recovery_request, recovery_rendering_active
from novelforge_windows.resources import resource_path
from novelforge_windows.single_instance import (
    create_installer_mutex,
    try_acquire_instance_lock,
)
from novelforge_windows.ui.web_window import WebMainWindow


def run() -> int:
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "NovelForge.Windows"
        )
    application = QApplication(sys.argv)
    application.setApplicationName("NovelForge")
    application.setOrganizationName("NovelForge")
    icon_path = resource_path("assets/novelforge.ico")
    if icon_path.is_file():
        application.setWindowIcon(QIcon(str(icon_path)))
    server = None
    instance_lock = None
    installer_mutex = None
    try:
        paths = resolve_app_paths()
        instance_lock = try_acquire_instance_lock(paths.data_dir)
        if instance_lock is None:
            QMessageBox.information(None, "NovelForge", "NovelForge 已在运行。")
            return 0
        installer_mutex = create_installer_mutex()
        if recovery_rendering_active():
            consume_gpu_recovery_request(paths.data_dir)
        server = LocalApplicationServer(paths)
        server.start()
        window = WebMainWindow(server.url, paths, server.port)
    except Exception as exc:
        if server is not None:
            server.stop()
        if instance_lock is not None and instance_lock.isLocked():
            instance_lock.unlock()
        QMessageBox.critical(None, "NovelForge 启动失败", str(exc))
        return 1
    try:
        application.aboutToQuit.connect(window.close)
        application.aboutToQuit.connect(server.stop)
        window.show()
        return application.exec()
    finally:
        if server is not None and server.thread.is_alive():
            server.stop()
        if instance_lock is not None and instance_lock.isLocked():
            instance_lock.unlock()
        # Keep the Inno AppMutex handle open until Windows tears down the
        # process, so Setup cannot race Python/Qt DLL unloading.
        _ = installer_mutex
