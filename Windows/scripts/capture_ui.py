"""Capture desktop routes for visual QA."""

from __future__ import annotations

import argparse
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication

from novelforge_windows.config import resolve_app_paths
from novelforge_windows.local_runtime import LocalApplicationServer
from novelforge_windows.ui.web_window import WebMainWindow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--routes", default="workbench,projects,chapters,memory,sample-analysis,personalization,appearance")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    routes = [route.strip().strip("/") for route in args.routes.split(",") if route.strip()]
    application = QApplication([])
    paths = resolve_app_paths()
    server = LocalApplicationServer(paths)
    server.start()
    window = WebMainWindow(server.url, paths, server.port)
    window.resize(1440, 900)
    window.show()
    index = 0
    capturing = False

    def capture_loaded(ok: bool) -> None:
        nonlocal index, capturing
        if capturing:
            return
        loaded_route = window.web_view.url().path().strip("/")
        if index >= len(routes) or loaded_route != routes[index]:
            return
        if not ok:
            application.exit(2)
            return
        capturing = True

        def save_and_continue() -> None:
            nonlocal index, capturing
            route = routes[index]
            if not window.grab().save(str(output_dir / f"{index + 1:02d}-{route}.png")):
                application.exit(3)
                return
            index += 1
            if index >= len(routes):
                application.quit()
                return
            capturing = False
            window.web_view.load(QUrl(f"http://127.0.0.1:{server.port}/{routes[index]}/"))

        QTimer.singleShot(1800, save_and_continue)

    window.web_view.loadFinished.connect(capture_loaded)
    window.web_view.load(QUrl(f"http://127.0.0.1:{server.port}/{routes[0]}/"))
    QTimer.singleShot(60000, lambda: application.exit(4))
    exit_code = application.exec()
    server.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
