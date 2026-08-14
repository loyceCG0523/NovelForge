"""Reproduce stuck/torn presentation artifacts during mixed real-world input.

``diagnose_rapid_tabs.py`` stresses route changes only. The reported failure is
different: after ordinary clicking/scrolling (e.g. the personalization page),
the visible window keeps a torn frame — sidebar texture bleeding diagonally
into the content area, missing-tile checkerboards — long enough to screenshot.

This tool drives the visible window with Qt-synthesized input (no real cursor
movement, no foreground stealing) in short mixed bursts (navigate / toggle the
animated sidebar / scroll / resize), lets each burst settle, then compares the
REAL desktop pixels against a fresh CDP screenshot of the same page state. A
mismatch seconds after the last input means the presentation path kept a
torn/stale frame. When a mismatch is found, the tool then calls
``QWebEngineView.update()`` once and re-measures: if a single explicit repaint
heals the visible pixels, pushing repaints after paint activity is a valid fix.

The window floats above other apps (WindowStaysOnTopHint) so screen capture
sees it, but it never activates, so the user's keyboard and mouse are left
alone. Diagnostic-only; never ships in the installer.

Examples::

    .venv/Scripts/python.exe scripts/diagnose_present_artifacts.py \
        --output .runtime/perf/present-baseline --rendering gpu --scenario toggle
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import json
import os
import queue
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_interactions import (  # noqa: E402
    NAV_ROUTES,
    _configure_profile_rendering,
    _reserve_debugging_port,
    seed_demo_data,
)
from diagnose_rapid_tabs import (  # noqa: E402
    StreamingCdpSession,
    _wait_for_json,
)

# Mean absolute luminance distance between the settled screen and a fresh CDP
# screenshot on a 120x70 signature. The run calibrates against its own noise
# floor measured before any input is sent.
SETTLED_MISMATCH_MIN = 6.0
HARD_STOP_MS = 300_000
WHEEL_DELTA = 120

PROBE_EXPRESSION = r"""
(() => {
  const rect = (node) => {
    if (!node) return null;
    const item = node.getBoundingClientRect();
    return { x: item.x, y: item.y, width: item.width, height: item.height };
  };
  return {
    viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
    path: location.pathname,
    collapsed: !!document.querySelector(".app-shell.sidebar-collapsed"),
    sidebar: rect(document.querySelector(".sidebar")),
    content: rect(document.querySelector(".content")),
    toggle: rect(document.querySelector(".sidebar-toggle")),
    sidebarUser: rect(document.querySelector(".sidebar-user")),
    menuItems: Array.from(document.querySelectorAll(".sidebar-user-menu button")).map((node) => ({
      label: node.innerText.trim(),
      rect: rect(node),
    })),
    navItems: Array.from(document.querySelectorAll("a.nav-item[href]")).map((node) => ({
      route: new URL(node.href, location.href).pathname,
      rect: rect(node),
    })),
  };
})()
"""


def capture_client_hwnd(hwnd: int, target_width: int = 960) -> dict[str, Any]:
    """One-shot capture of a window client area, clipped to the visible screen.

    A window may extend past this machine's work area; an unclipped desktop-DC
    read would fold the taskbar or other windows into the image. Only the
    visible intersection is what the user actually sees, so capture that.
    """
    rect = wintypes.RECT()
    point = wintypes.POINT(0, 0)
    user32 = ctypes.windll.user32
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    screen_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
    screen_h = user32.GetSystemMetrics(1)  # SM_CYSCREEN
    src_x = max(0, point.x)
    src_y = max(0, point.y)
    src_w = min(rect.right - rect.left, screen_w - src_x)
    src_h = min(rect.bottom - rect.top, screen_h - src_y)
    if src_w <= 0 or src_h <= 0:
        raise RuntimeError("window client area is not visible on screen")
    width = min(target_width, src_w)
    height = max(1, round(src_h * width / src_w))

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    gdi32 = ctypes.windll.gdi32
    desktop_dc = user32.GetDC(0)
    memory_dc = gdi32.CreateCompatibleDC(desktop_dc)
    bits = ctypes.c_void_p()
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    bitmap = gdi32.CreateDIBSection(memory_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        gdi32.SetStretchBltMode(memory_dc, 3)  # COLORONCOLOR
        if not gdi32.StretchBlt(memory_dc, 0, 0, width, height, desktop_dc, src_x, src_y, src_w, src_h, 0x00CC0020):
            raise ctypes.WinError()
        return {
            "rawBgra": ctypes.string_at(bits.value, width * height * 4),
            "width": width,
            "height": height,
            "visibleSource": {"x": src_x, "y": src_y, "width": src_w, "height": src_h},
            "clientSize": {"width": rect.right - rect.left, "height": rect.bottom - rect.top},
        }
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, desktop_dc)


def _lum_signature_bgra(raw: bytes, width: int, height: int, out_w: int = 120, out_h: int = 70) -> bytes:
    """Nearest-neighbor downsample to a small luminance signature."""
    result = bytearray(out_w * out_h)
    for oy in range(out_h):
        sy = min(height - 1, oy * height // out_h)
        row = sy * width * 4
        base = oy * out_w
        for ox in range(out_w):
            sx = min(width - 1, ox * width // out_w)
            offset = row + sx * 4
            result[base + ox] = (raw[offset] * 2 + raw[offset + 1] * 5 + raw[offset + 2]) // 8
    return bytes(result)


def _signature_distance(left: bytes, right: bytes) -> float:
    size = min(len(left), len(right))
    if not size:
        return 255.0
    return sum(abs(left[index] - right[index]) for index in range(size)) / size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--rendering", choices=("gpu", "software"), default="gpu")
    parser.add_argument("--scenario", choices=("mixed", "toggle"), default="toggle")
    parser.add_argument("--iterations", type=int, default=16)
    parser.add_argument("--settle-ms", type=int, default=1200)
    parser.add_argument("--data-dir", default="")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
    else:
        import tempfile

        data_dir = Path(tempfile.mkdtemp(prefix="novelforge-present-diagnostic-"))
    data_dir.mkdir(parents=True, exist_ok=True)

    debugging_port = _reserve_debugging_port()
    os.environ["NOVELFORGE_WINDOWS_DATA_DIR"] = str(data_dir)
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = f"127.0.0.1:{debugging_port}"
    _configure_profile_rendering(args.rendering)

    from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
    from PySide6.QtGui import QImage, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from novelforge_windows.config import resolve_app_paths
    from novelforge_windows.local_runtime import LocalApplicationServer
    from novelforge_windows.ui.web_window import WebMainWindow

    paths = resolve_app_paths()
    server = LocalApplicationServer(paths)
    server.start()
    base_url = f"http://127.0.0.1:{server.port}"
    seed_demo_data(base_url)

    application = QApplication([])
    window = WebMainWindow(f"{base_url}/workbench/", paths, server.port)
    # Stay visible above other windows for screen capture, but never steal the
    # user's focus. All input below is synthesized inside Qt, not Win32.
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    window.resize(1200, 700)
    window.move(30, 20)
    window.show()

    gui_requests: queue.Queue = queue.Queue()

    def pump_gui() -> None:
        try:
            item = gui_requests.get_nowait()
        except queue.Empty:
            return
        try:
            action = item["action"]
            point = QPoint(int(item.get("x", 0)), int(item.get("y", 0)))
            if action == "resize":
                window.resize(int(item["width"]), int(item["height"]))
            elif action == "capture":
                item["frame"] = capture_client_hwnd(int(window.winId()))
            elif action == "repaint":
                window.web_view.update()
            else:
                target = window.web_view.childAt(point) or window.web_view.focusProxy() or window.web_view
                target_point = target.mapFrom(window.web_view, point)
                if action == "click":
                    QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target_point)
                elif action == "wheel":
                    global_point = window.web_view.mapToGlobal(point)
                    event = QWheelEvent(
                        QPointF(target_point),
                        QPointF(global_point),
                        QPoint(0, 0),
                        QPoint(0, int(item["delta"])),
                        Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase,
                        False,
                    )
                    QApplication.sendEvent(target, event)
        except Exception as error:  # noqa: BLE001 - return exact input failure to driver
            item["error"] = repr(error)
        finally:
            item["done"].set()

    gui_timer = QTimer()
    gui_timer.setInterval(1)
    gui_timer.timeout.connect(pump_gui)
    gui_timer.start()

    def gui_call(**kwargs: Any) -> Any:
        done = threading.Event()
        kwargs["done"] = done
        gui_requests.put(kwargs)
        if not done.wait(5):
            raise TimeoutError(f"GUI action did not run: {kwargs.get('action')}")
        if kwargs.get("error"):
            raise RuntimeError(kwargs["error"])
        return kwargs.get("frame")

    def wait_for_cdp() -> StreamingCdpSession:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            targets = _wait_for_json(f"http://127.0.0.1:{debugging_port}/json", timeout=3)
            for target in targets:
                if target.get("type") == "page" and f"127.0.0.1:{server.port}/" in target.get("url", ""):
                    return StreamingCdpSession(target["webSocketDebuggerUrl"])
            time.sleep(0.2)
        raise RuntimeError("The NovelForge CDP page target did not appear")

    state: dict[str, Any] = {"done": False, "report": None}
    report: dict[str, Any] = {
        "rendering": args.rendering,
        "scenario": args.scenario,
        "dataDir": str(data_dir),
        "chromiumFlags": os.getenv("QTWEBENGINE_CHROMIUM_FLAGS", ""),
    }

    def save_raw_frame(frame: dict[str, Any], target: Path) -> None:
        image = QImage(
            frame["rawBgra"],
            int(frame["width"]),
            int(frame["height"]),
            int(frame["width"]) * 4,
            QImage.Format.Format_RGB32,
        ).copy()
        image.save(str(target), "PNG")

    def cdp_truth_signature(cdp: StreamingCdpSession) -> bytes:
        shot = cdp.call(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            timeout=15,
        )
        image = QImage.fromData(base64.b64decode(shot.get("data", "")))
        if image.isNull():
            return b""
        scaled = image.scaled(120, 70).convertToFormat(QImage.Format.Format_RGB32)
        raw = scaled.constBits().tobytes()
        signature = bytearray(120 * 70)
        for index in range(120 * 70):
            offset = index * 4
            signature[index] = (raw[offset] * 2 + raw[offset + 1] * 5 + raw[offset + 2]) // 8
        return bytes(signature)

    def click_rect(rect: dict[str, float]) -> None:
        gui_call(action="click", x=float(rect["x"]) + float(rect["width"]) / 2, y=float(rect["y"]) + float(rect["height"]) / 2)

    def drive() -> None:
        cdp: StreamingCdpSession | None = None
        try:
            cdp = wait_for_cdp()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                probe = cdp.evaluate(PROBE_EXPRESSION)
                if probe and probe.get("sidebar") and probe.get("navItems"):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("workbench page did not render")
            time.sleep(1.2)
            report["initialProbe"] = cdp.evaluate(PROBE_EXPRESSION)

            def settled_distance() -> tuple[float, dict[str, Any]]:
                frame = gui_call(action="capture")
                truth = cdp_truth_signature(cdp)
                return _signature_distance(
                    _lum_signature_bgra(frame["rawBgra"], int(frame["width"]), int(frame["height"])),
                    truth,
                ), frame

            # Noise floor with zero input.
            calibration: list[float] = []
            for _ in range(3):
                distance, _frame = settled_distance()
                calibration.append(distance)
                time.sleep(0.4)
            noise_floor = max(calibration)
            mismatch_threshold = max(SETTLED_MISMATCH_MIN, noise_floor * 3)
            report["calibrationDistances"] = [round(value, 3) for value in calibration]
            report["mismatchThreshold"] = round(mismatch_threshold, 3)

            iterations: list[dict[str, Any]] = []
            nav_routes = [f"/{route}/" for route, _title in NAV_ROUTES]
            for iteration in range(args.iterations):
                probe = cdp.evaluate(PROBE_EXPRESSION)
                record: dict[str, Any] = {"iteration": iteration, "startPath": probe.get("path")}

                if args.scenario == "toggle":
                    # Collapse + expand: the 180 ms grid-template-columns
                    # transition relayouts and re-rasterizes both the sticky
                    # sidebar layer and the content layer every frame. The
                    # toggle button moves between layouts, so re-probe.
                    click_rect(probe["toggle"])
                    time.sleep(0.45)
                    probe = cdp.evaluate(PROBE_EXPRESSION)
                    click_rect(probe["toggle"])
                    time.sleep(0.45)
                    record["target"] = "sidebar-toggle"
                else:
                    mode = iteration % 6
                    if mode in (3, 5) and probe.get("sidebarUser"):
                        click_rect(probe["sidebarUser"])
                        time.sleep(0.35)
                        menu_probe = cdp.evaluate(PROBE_EXPRESSION)
                        items = menu_probe.get("menuItems") or []
                        if items:
                            target = items[0 if mode == 3 else min(1, len(items) - 1)]
                            click_rect(target["rect"])
                            record["target"] = f"menu:{target['label']}"
                    else:
                        target_route = nav_routes[iteration % len(nav_routes)]
                        nav_target = next(
                            (item for item in probe["navItems"] if item["route"] == target_route),
                            probe["navItems"][0],
                        )
                        click_rect(nav_target["rect"])
                        record["target"] = f"nav:{target_route}"

                # Scroll immediately afterwards, while transitions and initial
                # rasterization are still in flight.
                probe = cdp.evaluate(PROBE_EXPRESSION)
                content = probe["content"]
                scroll_x = float(content["x"]) + float(content["width"]) / 2
                scroll_y = min(float(probe["viewport"]["height"]) - 60, float(content["y"]) + 320)
                for _notch in range(8):
                    gui_call(action="wheel", x=scroll_x, y=scroll_y, delta=-WHEEL_DELTA)
                    time.sleep(0.04)
                if iteration % 5 == 2:
                    gui_call(action="resize", width=1232, height=700)
                for _notch in range(6):
                    gui_call(action="wheel", x=scroll_x, y=scroll_y, delta=WHEEL_DELTA)
                    time.sleep(0.04)
                if iteration % 5 == 2:
                    gui_call(action="resize", width=1200, height=700)

                time.sleep(args.settle_ms / 1000)
                distance, frame = settled_distance()
                record.update(
                    {
                        "endPath": cdp.evaluate("location.pathname"),
                        "collapsed": cdp.evaluate(PROBE_EXPRESSION).get("collapsed"),
                        "settledDistance": round(distance, 3),
                        "settledMismatch": distance > mismatch_threshold,
                    }
                )
                if record["settledMismatch"]:
                    save_raw_frame(frame, artifacts_dir / f"iter-{iteration:02d}-dist{distance:.1f}.png")
                    shot = cdp.call(
                        "Page.captureScreenshot",
                        {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
                        timeout=15,
                    )
                    (artifacts_dir / f"iter-{iteration:02d}-truth.png").write_bytes(
                        base64.b64decode(shot.get("data", ""))
                    )
                    # Heal probe: one explicit repaint of the real view.
                    gui_call(action="repaint")
                    time.sleep(0.2)
                    healed_distance, healed_frame = settled_distance()
                    record["healedDistance"] = round(healed_distance, 3)
                    record["healedByRepaint"] = healed_distance <= mismatch_threshold
                    save_raw_frame(healed_frame, artifacts_dir / f"iter-{iteration:02d}-healed.png")
                iterations.append(record)

            report["finalProbe"] = cdp.evaluate(PROBE_EXPRESSION)
            distances = [item["settledDistance"] for item in iterations if "settledDistance" in item]
            report["iterations"] = iterations
            report["summary"] = {
                "iterationCount": len(iterations),
                "settledMismatchCount": sum(1 for item in iterations if item.get("settledMismatch")),
                "settledMismatchHealedCount": sum(1 for item in iterations if item.get("healedByRepaint")),
                "settledDistanceMedian": round(statistics.median(distances), 3) if distances else 0,
                "settledDistanceMax": round(max(distances), 3) if distances else 0,
                "noiseFloor": round(noise_floor, 3),
            }
        except Exception as error:  # noqa: BLE001 - preserve partial evidence
            import traceback

            report["error"] = repr(error)
            report["traceback"] = traceback.format_exc()
        finally:
            if cdp is not None:
                cdp.close()
            state["report"] = report
            state["done"] = True

    driver = threading.Thread(target=drive, name="novelforge-present-driver", daemon=True)
    driver.start()

    quit_timer = QTimer()
    quit_timer.setInterval(100)
    quit_timer.timeout.connect(lambda: state["done"] and application.quit())
    quit_timer.start()
    hard_stop = QTimer()
    hard_stop.setSingleShot(True)
    hard_stop.timeout.connect(application.quit)
    hard_stop.start(HARD_STOP_MS)
    application.exec()
    driver.join(timeout=10)
    server.stop()

    final_report = state.get("report") or {"error": "diagnostic driver did not finish"}
    (output_dir / "report.json").write_text(
        json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = final_report.get("summary", {})
    lines = [
        "# NovelForge presentation artifact diagnosis",
        "",
        f"- Rendering: `{args.rendering}`; scenario: `{args.scenario}`; iterations: {summary.get('iterationCount', 0)}; settle: {args.settle_ms} ms",
        f"- Noise floor / mismatch threshold: {summary.get('noiseFloor')} / {final_report.get('mismatchThreshold')}",
        f"- Settled mismatches (screen != Chromium after idle): **{summary.get('settledMismatchCount')}**",
        f"- Of which healed by one explicit repaint: **{summary.get('settledMismatchHealedCount')}**",
        f"- Settled distance median/max: {summary.get('settledDistanceMedian')}/{summary.get('settledDistanceMax')}",
    ]
    if final_report.get("error"):
        lines += ["", f"- Driver error: `{final_report['error']}`"]
    lines += ["", "See `report.json` and `artifacts/`."]
    (output_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if final_report.get("error"):
        print(final_report.get("traceback", ""))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
