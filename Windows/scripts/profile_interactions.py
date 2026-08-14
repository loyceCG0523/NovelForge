"""Measure real interaction performance of the NovelForge desktop shell.

Drives the actual Qt WebEngine window through Chrome DevTools Protocol and
records, for every sidebar navigation:

* click -> route commit (pathname change) and commit -> first stable paint;
* long tasks on the main thread between click and paint;
* the full network waterfall (route chunks, RSC payloads, API requests);
* window grabs during rapid-fire clicking to detect compositing artifacts.

This is a development tool. It never ships with the installer and keeps the
remote debugging port on the loopback interface only.

Usage:
    .venv/Scripts/python.exe scripts/profile_interactions.py --output .runtime/perf/before
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

NAV_ROUTES = [
    ("workbench", "创作工作台"),
    ("story-events", "剧情事件"),
    ("chapters", "章节管理"),
    ("projects", "作品管理"),
    ("memory", "结构化记忆"),
    ("research", "资料检索"),
    ("meme-library", "热梗库"),
    ("sample-analysis", "本地样本"),
]

INSTRUMENT_SCRIPT = r"""
(() => {
  if (window.__nfPerf) return;
  const perf = { nav: [], longtasks: [], resources: [], inflight: 0 };
  window.__nfPerf = perf;
  const round = (value) => Math.round(value * 100) / 100;
  const pendingKey = "__novelforgePerfPendingNav";
  const readPersistedPending = () => {
    try {
      const value = JSON.parse(sessionStorage.getItem(pendingKey) || "null");
      return value && value.token && Number.isFinite(value.startEpoch) ? value : null;
    } catch { return null; }
  };
  const clearPending = (token) => {
    window.__nfPendingNav = null;
    const stored = readPersistedPending();
    if (!token || stored?.token === token) sessionStorage.removeItem(pendingKey);
  };
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        perf.longtasks.push({ start: entry.startTime, duration: entry.duration });
      }
    }).observe({ entryTypes: ["longtask"] });
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        perf.resources.push({
          name: entry.name, start: entry.startTime, duration: entry.duration,
          size: entry.transferSize, type: entry.initiatorType
        });
      }
    }).observe({ entryTypes: ["resource"] });
  } catch (error) { /* older engines */ }

  const originalFetch = window.fetch;
  window.fetch = (...args) => {
    perf.inflight += 1;
    return originalFetch(...args).finally(() => { perf.inflight -= 1; });
  };

  document.addEventListener("click", (event) => {
    const link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!link) return;
    const targetPath = new URL(link.href, location.href).pathname.replace(/\/+$/, "") || "/";
    const currentPath = location.pathname.replace(/\/+$/, "") || "/";
    if (targetPath === currentPath) return;
    const startedAt = performance.now();
    const pending = {
      token: window.__nfNextToken || `${Date.now()}-${Math.random()}`,
      href: link.getAttribute("href"),
      from: location.pathname,
      start: startedAt,
      startEpoch: performance.timeOrigin + startedAt
    };
    window.__nfNextToken = null;
    window.__nfPendingNav = pending;
    try { sessionStorage.setItem(pendingKey, JSON.stringify(pending)); } catch {}
  }, true);

  const finishNavigation = (pending, changedPath, commitAt, navigationType) => {
    const waitStart = performance.now();
    let firstPaintAt = 0;
    const startedAt = navigationType === "hard"
      ? pending.startEpoch - performance.timeOrigin
      : pending.start;
    const waitIdle = () => {
      const idleStart = performance.now();
      const check = () => {
        if (perf.inflight > 0) { setTimeout(waitIdle, 40); return; }
        if (performance.now() - idleStart < 180 && performance.now() - waitStart < 9000) {
          setTimeout(check, 30);
          return;
        }
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const readyAt = performance.now();
          perf.nav.push({
            token: pending.token,
            navigationType,
            from: changedPath,
            href: pending.href,
            to: location.pathname,
            clickToCommit: round(Math.max(0, commitAt - startedAt)),
            commitToFirstPaint: round(Math.max(0, firstPaintAt - commitAt)),
            clickToReady: round(Math.max(0, readyAt - startedAt)),
            perfStart: startedAt,
            perfReady: readyAt,
            at: Date.now()
          });
          clearPending(pending.token);
        }));
      };
      check();
    };
    const waitContent = () => {
      const content = document.querySelector(".content") || document.querySelector("main");
      const ready = content && content.children.length > 0 && document.readyState !== "loading";
      if (!ready && performance.now() - waitStart < 9000) { setTimeout(waitContent, 16); return; }
      requestAnimationFrame(() => requestAnimationFrame(() => {
        firstPaintAt = performance.now();
        waitIdle();
      }));
    };
    waitContent();
  };

  // A failed App Router resource may fall back to a full document navigation.
  // sessionStorage lets that navigation retain the original click timestamp.
  const persistedPending = readPersistedPending();
  if (persistedPending && persistedPending.from !== location.pathname) {
    finishNavigation(persistedPending, persistedPending.from, performance.now(), "hard");
  }

  let lastPath = location.pathname;
  const poll = () => {
    if (location.pathname === lastPath) return;
    const now = performance.now();
    const pending = window.__nfPendingNav || readPersistedPending() || {
      token: `unknown-${Date.now()}`,
      href: location.pathname,
      from: lastPath,
      start: now,
      startEpoch: performance.timeOrigin + now
    };
    const changedPath = lastPath;
    lastPath = location.pathname;
    finishNavigation(pending, changedPath, now, "soft");
  };
  setInterval(poll, 8);
})();
"""


def _json_request(url: str, payload: dict | None = None, method: str | None = None) -> object:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method or ("POST" if payload is not None else "GET"))
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def seed_demo_data(base_url: str) -> None:
    """Create one realistic project with chapters so pages render real content."""
    novels = _json_request(f"{base_url}/api/novels")
    if novels:
        return
    brief = {
        "work_type": "长篇小说",
        "story_era": "近未来都市",
        "story_location": "海城",
        "selling_points": "时间循环与悬疑",
        "plot_direction": "主角在循环中追查真相",
        "worldview": "近未来海城，记忆可以被提取",
        "chapter_word_min": 2500,
        "chapter_word_max": 3500,
        "event_chapter_count": 6,
        "style_reference": "",
        "forbidden_content": "",
        "automation_strategy": "",
        "sample_reference_ids": [],
        "characters": [
            {
                "key": "protagonist",
                "name": "林澈",
                "gender": "男",
                "age": "28",
                "occupation": "记忆调查员",
                "is_protagonist": True,
                "goal": "查明妹妹失踪真相",
                "detailed_setting": "冷静克制，擅长记忆取证。",
            }
        ],
        "planned_events": [],
    }
    novel = _json_request(
        f"{base_url}/api/novels",
        {
            "title": "循环之海",
            "genre": "科幻悬疑",
            "target_words": 300000,
            "premise": "记忆调查员在时间循环中追查妹妹失踪的真相。",
            "brief": brief,
        },
    )
    novel_id = novel["id"]
    for index in range(1, 81):
        _json_request(
            f"{base_url}/api/novels/{novel_id}/chapters",
            {
                "chapter_index": index,
                "title": f"第{index}章 潮声",
                "summary": "主角进入新的循环，在海城旧区找到一条关键线索。",
                "content": "海城的雨下了整夜，记忆调查员穿梭在潮湿的旧城区。" * 130,
            },
        )


class CdpSession:
    """Minimal synchronous CDP client over a websocket."""

    def __init__(self, ws_url: str) -> None:
        from websockets.sync.client import connect

        self.ws = connect(ws_url, max_size=64 * 1024 * 1024)
        self._next_id = 0
        self._lock = threading.Lock()
        self._responses: dict[int, queue.Queue] = {}
        self.events: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for raw in self.ws:
                message = json.loads(raw)
                if "id" in message:
                    pending = self._responses.get(message["id"])
                    if pending is not None:
                        pending.put(message)
                else:
                    self.events.put((time.monotonic(), message))
        except Exception:
            return

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        with self._lock:
            self._next_id += 1
            message_id = self._next_id
            response_queue: queue.Queue = queue.Queue()
            self._responses[message_id] = response_queue
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        try:
            message = response_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError(f"CDP call timed out: {method}") from error
        finally:
            self._responses.pop(message_id, None)
        if "error" in message:
            raise RuntimeError(f"CDP error in {method}: {message['error']}")
        return message.get("result", {})

    def drain_events(self) -> list[tuple[float, dict]]:
        drained: list[tuple[float, dict]] = []
        while True:
            try:
                drained.append(self.events.get_nowait())
            except queue.Empty:
                return drained

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")


def analyze_png(path: Path) -> dict:
    """Sample a compositor grab without spending seconds decoding it in Python."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    image = QImage(str(path))
    if image.isNull():
        return {"file": path.name, "valid": False}
    sample = image.convertToFormat(QImage.Format.Format_RGBA8888).scaled(
        180,
        112,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    pixels = sample.bits().tobytes()
    buckets: dict[tuple[int, int, int], int] = {}
    luminance_sum = 0.0
    luminance_squared_sum = 0.0
    for i in range(0, len(pixels) - 3, 4):
        red, green, blue = pixels[i], pixels[i + 1], pixels[i + 2]
        key = (red >> 4, green >> 4, blue >> 4)
        buckets[key] = buckets.get(key, 0) + 1
        luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        luminance_sum += luminance
        luminance_squared_sum += luminance * luminance
    total = sum(buckets.values()) or 1
    dominant_key, dominant_count = max(buckets.items(), key=lambda item: item[1])
    share = dominant_count / total
    brightness = sum(dominant_key) / 3 * 16
    luminance_mean = luminance_sum / total
    luminance_variance = max(0.0, luminance_squared_sum / total - luminance_mean * luminance_mean)
    return {
        "file": path.name,
        "valid": True,
        "analyzed": True,
        "width": image.width(),
        "height": image.height(),
        "dominant_share": round(share, 3),
        "luminance_stddev": round(luminance_variance ** 0.5, 2),
        "dominant_is_blank": bool(
            share > 0.92 and luminance_variance ** 0.5 < 4 and (brightness > 235 or brightness < 20)
        ),
    }


def _reserve_debugging_port() -> int:
    """Choose an unused loopback port so an older profiler cannot steal CDP."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _configure_profile_rendering(mode: str) -> None:
    """Select rendering before the first PySide/WebEngine import."""
    flag_name = "QTWEBENGINE_CHROMIUM_FLAGS"
    flags = os.getenv(flag_name, "").split()
    flags = [flag for flag in flags if flag != "--disable-gpu"]
    if mode == "software":
        flags.append("--disable-gpu")
    if flags:
        os.environ[flag_name] = " ".join(flags)
    else:
        os.environ.pop(flag_name, None)


def _performance_metrics(cdp: CdpSession) -> dict[str, float]:
    values = cdp.call("Performance.getMetrics").get("metrics", [])
    return {str(item["name"]): float(item["value"]) for item in values}


def _metric_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float | int]:
    durations = {
        "taskMs": "TaskDuration",
        "scriptMs": "ScriptDuration",
        "layoutMs": "LayoutDuration",
        "styleMs": "RecalcStyleDuration",
    }
    counts = {
        "layoutCount": "LayoutCount",
        "styleCount": "RecalcStyleCount",
    }
    result: dict[str, float | int] = {
        label: round(max(0.0, after.get(metric, 0.0) - before.get(metric, 0.0)) * 1000, 2)
        for label, metric in durations.items()
    }
    result.update(
        {
            label: int(max(0.0, after.get(metric, 0.0) - before.get(metric, 0.0)))
            for label, metric in counts.items()
        }
    )
    result["jsHeapDeltaBytes"] = int(after.get("JSHeapUsedSize", 0.0) - before.get("JSHeapUsedSize", 0.0))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="Directory for the JSON report and frames")
    parser.add_argument("--rounds", type=int, default=3, help="Warm navigation rounds per route")
    parser.add_argument("--rapid-clicks", type=int, default=48, help="Clicks in the rapid-fire phase")
    parser.add_argument("--rapid-interval-ms", type=int, default=130)
    parser.add_argument("--data-dir", default="", help="Override NovelForge data dir (default: fresh temp)")
    parser.add_argument(
        "--rendering",
        choices=("software", "gpu"),
        default="software",
        help="Reproduce the software compatibility path or the normal GPU path",
    )
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    frames_dir = output_dir / "frames"
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
    else:
        import tempfile

        data_dir = Path(tempfile.mkdtemp(prefix="novelforge-profile-"))
    data_dir.mkdir(parents=True, exist_ok=True)

    debugging_port = _reserve_debugging_port()
    os.environ["NOVELFORGE_WINDOWS_DATA_DIR"] = str(data_dir)
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = f"127.0.0.1:{debugging_port}"
    _configure_profile_rendering(args.rendering)

    from PySide6.QtCore import QTimer
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
    window = WebMainWindow(server.url, paths, server.port)
    window.resize(1440, 900)
    window.show()
    web_profile = window.web_view.page().profile()
    web_profile_info = {
        "offTheRecord": web_profile.isOffTheRecord(),
        "httpCacheType": web_profile.httpCacheType().name,
        "hasCachePath": bool(web_profile.cachePath()),
        "hasPersistentStoragePath": bool(web_profile.persistentStoragePath()),
    }

    state = {"phase": "init", "done": False, "report": None}
    frame_counter = {"value": 0}
    frame_routes: dict[str, str] = {}

    def grab_frame() -> None:
        if state["phase"] == "rapid-visual":
            frame_counter["value"] += 1
            filename = f"rapid-{frame_counter['value']:03d}.png"
            frame_routes[filename] = window.web_view.url().path()
            window.grab().save(str(frames_dir / filename))
        if state["done"]:
            application.quit()

    grab_timer = QTimer()
    grab_timer.timeout.connect(grab_frame)
    grab_timer.start(110)

    def wait_for_cdp() -> CdpSession:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{debugging_port}/json", timeout=2
                ) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                for target in targets:
                    if (
                        target.get("type") == "page"
                        and f"127.0.0.1:{server.port}/" in target.get("url", "")
                    ):
                        return CdpSession(target["webSocketDebuggerUrl"])
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("CDP endpoint did not come up")

    def collect_since(cdp: CdpSession, started: float) -> list[dict]:
        """Network events drained so far, timed with CDP monotonic timestamps."""
        requests: dict[str, dict] = {}
        for _received_at, event in cdp.drain_events():
            method = event.get("method", "")
            params = event.get("params", {})
            timestamp_ms = float(params.get("timestamp", 0)) * 1000
            if method == "Network.requestWillBeSent":
                requests[params["requestId"]] = {
                    "url": params["request"]["url"],
                    "method": params["request"]["method"],
                    "type": params.get("type", ""),
                    "startTs": timestamp_ms,
                }
            elif method == "Network.responseReceived":
                entry = requests.get(params["requestId"])
                if entry is not None:
                    entry["status"] = params["response"]["status"]
                    entry["fromCache"] = bool(
                        params["response"].get("fromDiskCache") or params["response"].get("fromServiceWorker") or params["response"].get("fromPrefetchCache")
                    )
                    entry["mime"] = params["response"].get("mimeType", "")
                    entry["ttfbMs"] = round(timestamp_ms - entry["startTs"], 2)
            elif method == "Network.loadingFinished":
                entry = requests.get(params["requestId"])
                if entry is not None:
                    entry["durationMs"] = round(timestamp_ms - entry.pop("startTs"), 2)
                    entry["bytes"] = params.get("encodedDataLength", 0)
            elif method == "Network.loadingFailed":
                entry = requests.get(params["requestId"])
                if entry is not None:
                    entry["failed"] = params.get("errorText", "")
        return [entry for entry in requests.values() if "durationMs" in entry or "failed" in entry]

    def drive() -> None:
        report: dict = {
            "data_dir": str(data_dir),
            "rendering": args.rendering,
            "chromiumFlags": os.getenv("QTWEBENGINE_CHROMIUM_FLAGS", ""),
            "debuggingPort": debugging_port,
            "webProfile": web_profile_info,
            "routes": [route for route, _ in NAV_ROUTES],
        }
        try:
            cdp = wait_for_cdp()
            try:
                version = _json_request(f"http://127.0.0.1:{debugging_port}/json/version")
                browser_cdp = CdpSession(version["webSocketDebuggerUrl"])
                system_info = browser_cdp.call("SystemInfo.getInfo")
                gpu = system_info.get("gpu", {})
                report["gpu"] = {
                    "devices": [
                        {
                            key: device.get(key)
                            for key in ("vendorId", "deviceId", "vendorString", "deviceString", "driverVendor", "driverVersion")
                            if device.get(key) not in (None, "")
                        }
                        for device in gpu.get("devices", [])
                    ],
                    "featureStatus": gpu.get("featureStatus", {}),
                }
                browser_cdp.ws.close()
            except Exception as gpu_error:
                report["gpuProbeError"] = repr(gpu_error)
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Network.enable")
            cdp.call("Performance.enable")
            cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": INSTRUMENT_SCRIPT})

            # ---- cold load -------------------------------------------------
            state["phase"] = "cold"
            cold_start = time.monotonic()
            cdp.drain_events()
            cdp.call("Page.navigate", {"url": f"{base_url}/workbench/"})
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ready = cdp.evaluate(
                    "document.readyState === 'complete' && !!document.querySelector('.content')"
                )
                if ready:
                    break
                time.sleep(0.1)
            time.sleep(1.0)  # let late resources settle
            nav_timing = cdp.evaluate(
                "JSON.stringify(performance.getEntriesByType('navigation')[0] || {})"
            )
            report["cold"] = {
                "wallMs": round((time.monotonic() - cold_start) * 1000, 1),
                "navigation": json.loads(nav_timing or "{}"),
                "network": collect_since(cdp, cold_start),
                "resources": json.loads(
                    cdp.evaluate("JSON.stringify(window.__nfPerf ? window.__nfPerf.resources : [])") or "[]"
                ),
                "longtasks": json.loads(
                    cdp.evaluate("JSON.stringify(window.__nfPerf ? window.__nfPerf.longtasks : [])") or "[]"
                ),
            }

            # ---- warm navigations ------------------------------------------
            state["phase"] = "warm"
            warm_results: list[dict] = []
            for round_index in range(args.rounds):
                for route, _label in NAV_ROUTES:
                    current_path = cdp.evaluate("location.pathname") or ""
                    if current_path.strip("/") == route:
                        continue  # same-route clicks do not change the pathname
                    token = f"{round_index}-{route}-{time.time_ns()}"
                    cdp.drain_events()
                    metrics_before = _performance_metrics(cdp)
                    click_start = time.monotonic()
                    route_json = json.dumps(f"/{route}")
                    token_json = json.dumps(token)
                    clicked = cdp.evaluate(
                        "(() => { const route = %s; window.__nfNextToken = %s;"
                        " const link = Array.from(document.querySelectorAll('a[href]')).find((item) =>"
                        "   (new URL(item.href, location.href).pathname.replace(/\\/+$/, '') || '/') === route);"
                        " if (!link) return false; link.click(); return true; })()"
                        % (route_json, token_json)
                    )
                    if not clicked:
                        warm_results.append({"route": route, "round": round_index, "error": "link not found"})
                        continue
                    deadline = time.monotonic() + 15
                    record = None
                    while time.monotonic() < deadline:
                        value = cdp.evaluate(
                            "(() => { const item = window.__nfPerf?.nav.find((entry) => entry.token === %s);"
                            " return item ? JSON.stringify(item) : ''; })()" % token_json
                        )
                        if value:
                            record = json.loads(value)
                            break
                        time.sleep(0.05)
                    time.sleep(0.12)
                    metrics_after = _performance_metrics(cdp)
                    if record:
                        perf_start = max(0.0, float(record.get("perfStart", 0.0)))
                        perf_ready = float(record.get("perfReady", 0.0))
                        longtasks = json.loads(
                            cdp.evaluate(
                                "JSON.stringify(window.__nfPerf.longtasks.filter((item) => item.start >= %f && item.start <= %f))"
                                % (perf_start, perf_ready)
                            ) or "[]"
                        )
                        resources = json.loads(
                            cdp.evaluate(
                                "JSON.stringify(window.__nfPerf.resources.filter((item) => item.start >= %f && item.start <= %f))"
                                % (perf_start, perf_ready)
                            ) or "[]"
                        )
                    else:
                        longtasks = []
                        resources = []
                    network = collect_since(cdp, click_start)
                    warm_results.append(
                        {
                            "route": route,
                            "round": round_index,
                            "record": record,
                            "wallMs": round((time.monotonic() - click_start) * 1000, 1),
                            "longtasks": longtasks,
                            "network": network,
                            "resourceCount": len(resources),
                            "mainThread": _metric_delta(metrics_before, metrics_after),
                        }
                    )
            report["warm"] = warm_results

            def run_rapid_clicks() -> None:
                for index in range(args.rapid_clicks):
                    route = NAV_ROUTES[index % len(NAV_ROUTES)][0]
                    route_json = json.dumps(f"/{route}")
                    cdp.evaluate(
                        "(() => { const route = %s; const link = Array.from(document.querySelectorAll('a[href]')).find((item) =>"
                        " (new URL(item.href, location.href).pathname.replace(/\\/+$/, '') || '/') === route);"
                        " if (link) link.click(); return true; })()" % route_json
                    )
                    time.sleep(args.rapid_interval_ms / 1000)

            def wait_for_rapid_settle() -> None:
                settle_deadline = time.monotonic() + 8
                while time.monotonic() < settle_deadline:
                    time.sleep(0.15)
                    pending = cdp.evaluate(
                        "window.__nfPendingNav || sessionStorage.getItem('__novelforgePerfPendingNav') ? 1 : 0"
                    )
                    if not pending:
                        break
                time.sleep(0.5)

            # First run without compositor grabs so the long-task numbers are not
            # distorted by screenshot capture itself.
            state["phase"] = "rapid-metrics"
            rapid_start = time.monotonic()
            cdp.evaluate("window.__nfPerf && (window.__nfPerf.longtasks = [])")
            run_rapid_clicks()
            wait_for_rapid_settle()
            rapid_longtasks = json.loads(
                cdp.evaluate("JSON.stringify(window.__nfPerf.longtasks)") or "[]"
            )
            final_url = cdp.evaluate("location.pathname")
            report["rapid"] = {
                "clicks": args.rapid_clicks,
                "intervalMs": args.rapid_interval_ms,
                "wallMs": round((time.monotonic() - rapid_start) * 1000, 1),
                "longtaskCount": len(rapid_longtasks),
                "longtaskTotalMs": round(sum(t["duration"] for t in rapid_longtasks), 1),
                "longtaskMaxMs": round(max((t["duration"] for t in rapid_longtasks), default=0), 1),
                "finalPath": final_url,
            }

            # A second identical pass samples the actual Qt compositor for blank
            # or invalid frames. Its timing is reported separately by design.
            state["phase"] = "rapid-visual"
            visual_start = time.monotonic()
            run_rapid_clicks()
            wait_for_rapid_settle()
            state["phase"] = "settle"
            report["rapidVisual"] = {
                "clicks": args.rapid_clicks,
                "intervalMs": args.rapid_interval_ms,
                "wallMs": round((time.monotonic() - visual_start) * 1000, 1),
                "finalPath": cdp.evaluate("location.pathname"),
            }
            report["diagnostics"] = cdp.evaluate(
                "window.__novelforgeDiagnostics ? JSON.parse(JSON.stringify(window.__novelforgeDiagnostics)) : null"
            )
            report["frames"] = []
            for path in sorted(frames_dir.glob("rapid-*.png")):
                frame = analyze_png(path)
                frame["route"] = frame_routes.get(path.name, "")
                report["frames"].append(frame)
            report["blankFrames"] = [f["file"] for f in report["frames"] if f.get("dominant_is_blank")]
            report["invalidFrames"] = [f["file"] for f in report["frames"] if not f.get("valid")]
            state["report"] = report
        except Exception as error:  # noqa: BLE001 - report must always be written
            report["error"] = repr(error)
            state["report"] = report
        finally:
            state["done"] = True

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    exit_code = application.exec()
    driver.join(timeout=5)
    server.stop()

    report = state.get("report") or {"error": "driver did not finish"}
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # Console summary ---------------------------------------------------------
    print(f"data dir: {data_dir}")
    if "cold" in report:
        cold_nav = report["cold"].get("navigation") or {}
        print(f"cold load wall: {report['cold']['wallMs']} ms; domContentLoaded: {round(cold_nav.get('domContentLoadedEventEnd', 0))} ms")
    warm = [item for item in report.get("warm", []) if item.get("record")]
    if warm:
        totals = [item["record"]["clickToReady"] for item in warm]
        commits = [item["record"]["clickToCommit"] for item in warm]
        firsts = [item["record"]["commitToFirstPaint"] for item in warm]
        paints = [commit + first for commit, first in zip(commits, firsts)]
        task_times = [item.get("mainThread", {}).get("taskMs", 0) for item in warm]
        lt_total = sum(sum(t["duration"] for t in item["longtasks"]) for item in warm)
        api_counts = [len([n for n in item["network"] if "/api/" in n.get("url", "")]) for item in warm]
        api_bytes = [sum(n.get("bytes", 0) for n in item["network"] if "/api/" in n.get("url", "")) for item in warm]
        print(
            "warm nav click->paint ms: median {:.0f} p90 {:.0f}; click->ready median {:.0f} p90 {:.0f} (commit median {:.0f}, commit->firstPaint median {:.0f}, task median {:.0f}); longtask total {:.0f} ms; api reqs/nav median {:.0f}, api bytes/nav median {:.0f}".format(
                statistics.median(paints),
                sorted(paints)[max(0, int(len(paints) * 0.9) - 1)],
                statistics.median(totals),
                sorted(totals)[max(0, int(len(totals) * 0.9) - 1)],
                statistics.median(commits),
                statistics.median(firsts),
                statistics.median(task_times),
                lt_total,
                statistics.median(api_counts),
                statistics.median(api_bytes),
            )
        )
        per_route: dict[str, list[float]] = {}
        for item in warm:
            per_route.setdefault(item["route"], []).append(item["record"]["clickToReady"])
        for route, values in per_route.items():
            print(f"  {route:<16} median {statistics.median(values):>7.0f} ms  n={len(values)}")
    if "rapid" in report:
        rapid = report["rapid"]
        print(
            f"rapid: {rapid['clicks']} clicks in {rapid['wallMs']} ms; longtasks {rapid['longtaskCount']} ({rapid['longtaskTotalMs']} ms total, max {rapid['longtaskMaxMs']} ms); blank frames: {report.get('blankFrames')}; invalid frames: {report.get('invalidFrames')}"
        )
    if "error" in report:
        print("ERROR:", report["error"])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
