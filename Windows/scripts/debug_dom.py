"""One-off: dump actual DOM anchors of the running desktop app via CDP."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

os.environ["NOVELFORGE_WINDOWS_DATA_DIR"] = tempfile.mkdtemp(prefix="novelforge-debug-")
os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "127.0.0.1:9230"

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from novelforge_windows.config import resolve_app_paths  # noqa: E402
from novelforge_windows.local_runtime import LocalApplicationServer  # noqa: E402
from novelforge_windows.ui.web_window import WebMainWindow  # noqa: E402


def main() -> int:
    paths = resolve_app_paths()
    server = LocalApplicationServer(paths)
    server.start()
    application = QApplication([])
    window = WebMainWindow(server.url, paths, server.port)
    window.show()
    QTimer.singleShot(45000, application.quit)  # hard stop no matter what

    def probe() -> None:
        try:
            from websockets.sync.client import connect

            ws_url = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen("http://127.0.0.1:9230/json", timeout=2) as response:
                        targets = json.loads(response.read().decode("utf-8"))
                    print("TARGETS:", json.dumps([(t.get("type"), t.get("url")) for t in targets], ensure_ascii=False), flush=True)
                    pages = [t for t in targets if t.get("type") == "page"]
                    if pages:
                        ws_url = pages[0]["webSocketDebuggerUrl"]
                        break
                except Exception as error:
                    print("cdp list retry:", error, flush=True)
                    time.sleep(0.3)
            if not ws_url:
                print("ERROR: no CDP page target", flush=True)
                return
            ws = connect(ws_url, open_timeout=10)
            counter = 0

            def call(method, params=None):
                nonlocal counter
                counter += 1
                ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
                while True:
                    message = json.loads(ws.recv(timeout=10))
                    if message.get("id") == counter:
                        if "error" in message:
                            raise RuntimeError(message["error"])
                        return message.get("result", {})

            def evaluate(expr):
                outcome = call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                if outcome.get("exceptionDetails"):
                    return f"JS-EXCEPTION: {json.dumps(outcome['exceptionDetails'])[:400]}"
                return outcome.get("result", {}).get("value")

            time.sleep(4)
            for label, expr in [
                ("url", "location.href"),
                ("title", "document.title"),
                ("anchors", "Array.from(document.querySelectorAll('a')).map(a => a.getAttribute('href')).slice(0, 30)"),
                ("hasContent", "!!document.querySelector('.content')"),
                ("selectorTest", "!!document.querySelector(\"a[href='/workbench']\")"),
                ("bodyText", "document.body ? document.body.innerText.slice(0, 200) : null"),
            ]:
                print(f"{label}: {json.dumps(evaluate(expr), ensure_ascii=False)}", flush=True)
        except Exception:
            traceback.print_exc()
        finally:
            application.quit()

    threading.Thread(target=probe, daemon=True).start()
    code = application.exec()
    server.stop()
    return code


if __name__ == "__main__":
    sys.exit(main())
