"""Reproduce and diagnose rapid sidebar switching in the real Qt window.

Unlike ``profile_interactions.py``, this tool does not call ``HTMLElement.click``.
It sends Qt or Win32 mouse input to the visible ``QWebEngineView`` and records
Chromium compositor frames while clicks are arriving.  Stable screenshots of
every route are used as visual templates so the report can tell whether the
sidebar highlight, top title and page body shown in one frame belong to the
same route.

The script is diagnostic-only and never ships in the installer.

Examples::

    .venv/Scripts/python.exe scripts/diagnose_rapid_tabs.py \
        --output .runtime/perf/rapid-gpu-20 --rendering gpu \
        --input win32 --clicks 160 --interval-ms 20

    .venv/Scripts/python.exe scripts/diagnose_rapid_tabs.py \
        --output .runtime/perf/rapid-software-80 --rendering software \
        --input qt --clicks 120 --interval-ms 80
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import json
import math
import os
import queue
import socket
import statistics
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from profile_interactions import (  # diagnostic helpers; imports no Qt at module load
    NAV_ROUTES,
    _configure_profile_rendering,
    _metric_delta,
    _performance_metrics,
    _reserve_debugging_port,
    seed_demo_data,
)


ROUTE_TITLES = {f"/{route}": title for route, title in NAV_ROUTES}


class Win32ScreenRecorder:
    """Capture the pixels DWM presents for the Qt client area.

    CDP screencasts stop at Chromium's compositor and therefore cannot detect a
    stale Qt backing store or a bad Windows presentation surface.  This helper
    samples the real desktop DC while the same Win32 clicks are being sent.
    Frames are downscaled in GDI before copying them to Python so a long stress
    run stays bounded in memory and does not materially load Blink's main
    thread.
    """

    _SRCCOPY = 0x00CC0020
    _COLORONCOLOR = 3
    _DIB_RGB_COLORS = 0

    def __init__(self, hwnd: int, *, interval_ms: int = 20, target_width: int = 480) -> None:
        self.hwnd = int(hwnd)
        self.interval_ms = max(8, int(interval_ms))
        self.target_width = max(240, int(target_width))
        self.frames: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _client_bounds(self) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        point = wintypes.POINT(0, 0)
        user32 = ctypes.windll.user32
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL
        if not user32.GetClientRect(self.hwnd, ctypes.byref(rect)):
            raise ctypes.WinError()
        if not user32.ClientToScreen(self.hwnd, ctypes.byref(point)):
            raise ctypes.WinError()
        return point.x, point.y, rect.right - rect.left, rect.bottom - rect.top

    def capture_once(self) -> dict[str, Any]:
        if sys.platform != "win32":
            raise RuntimeError("Win32 screen capture is available only on Windows")

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

        source_x, source_y, source_width, source_height = self._client_bounds()
        if source_width <= 0 or source_height <= 0:
            raise RuntimeError("Qt window has an empty client area")
        target_width = min(self.target_width, source_width)
        target_height = max(1, round(source_height * target_width / source_width))
        stride = target_width * 4
        size = stride * target_height

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(BITMAPINFO),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
        gdi32.SetStretchBltMode.restype = ctypes.c_int
        gdi32.StretchBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.StretchBlt.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        desktop_dc = user32.GetDC(0)
        memory_dc = gdi32.CreateCompatibleDC(desktop_dc)
        bits = ctypes.c_void_p()
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = target_width
        info.bmiHeader.biHeight = -target_height  # top-down DIB
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        info.bmiHeader.biSizeImage = size
        bitmap = gdi32.CreateDIBSection(
            memory_dc,
            ctypes.byref(info),
            self._DIB_RGB_COLORS,
            ctypes.byref(bits),
            None,
            0,
        )
        if not desktop_dc or not memory_dc or not bitmap or not bits.value:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            if desktop_dc:
                user32.ReleaseDC(0, desktop_dc)
            raise ctypes.WinError()

        previous = gdi32.SelectObject(memory_dc, bitmap)
        try:
            gdi32.SetStretchBltMode(memory_dc, self._COLORONCOLOR)
            copied = gdi32.StretchBlt(
                memory_dc,
                0,
                0,
                target_width,
                target_height,
                desktop_dc,
                source_x,
                source_y,
                source_width,
                source_height,
                self._SRCCOPY,
            )
            if not copied:
                raise ctypes.WinError()
            return {
                "rawBgra": ctypes.string_at(bits.value, size),
                "width": target_width,
                "height": target_height,
                "receivedEpoch": time.time() * 1000,
                "sourceBounds": {
                    "x": source_x,
                    "y": source_y,
                    "width": source_width,
                    "height": source_height,
                },
            }
        finally:
            gdi32.SelectObject(memory_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(0, desktop_dc)

    def start(self) -> None:
        self.frames.clear()
        self._stop.clear()

        def capture_loop() -> None:
            next_deadline = time.perf_counter()
            while not self._stop.is_set():
                try:
                    self.frames.append(self.capture_once())
                except Exception as error:  # noqa: BLE001 - preserve evidence gathered so far
                    self.frames.append({"captureError": repr(error), "receivedEpoch": time.time() * 1000})
                    break
                next_deadline += self.interval_ms / 1000
                remaining = next_deadline - time.perf_counter()
                if remaining > 0:
                    self._stop.wait(remaining)

        self._thread = threading.Thread(target=capture_loop, name="novelforge-screen-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None


INSTRUMENT_SCRIPT = r"""
(() => {
  if (window.__nfRapidDiagnostic) return;
  const normalize = (value) => `/${String(value || "").split("?")[0].split("#")[0].replace(/^\/+|\/+$/g, "")}`;
  const diagnostic = {
    version: 2,
    recording: false,
    clicks: [],
    samples: [],
    routeChanges: [],
    mutations: [],
    longtasks: [],
    eventLoopGaps: [],
    fetches: [],
    inflight: 0,
    maxInflight: 0,
    referenceStartEpoch: 0,
  };
  window.__nfRapidDiagnostic = diagnostic;
  const epoch = () => performance.timeOrigin + performance.now();
  const compactText = (node, limit) => String(node?.innerText || "").replace(/\s+/g, " ").trim().slice(0, limit);
  const snapshot = (reason) => {
    if (!diagnostic.recording) return;
    const active = Array.from(document.querySelectorAll('.nav-item[aria-current="page"]'));
    const titleNodes = Array.from(document.querySelectorAll(".title-block h1"));
    const markerNodes = Array.from(document.querySelectorAll("[data-novelforge-page-title]"));
    const content = document.querySelector(".content");
    diagnostic.samples.push({
      epoch: epoch(),
      perf: performance.now(),
      reason,
      path: normalize(location.pathname),
      title: titleNodes.map((item) => compactText(item, 80)).join(" | "),
      titleCount: titleNodes.length,
      marker: markerNodes.map((item) => item.getAttribute("data-novelforge-page-title") || "").join(" | "),
      markerCount: markerNodes.length,
      active: active.map((item) => compactText(item.querySelector(".nav-label") || item, 80)).join(" | "),
      activeCount: active.length,
      bodyText: compactText(content, 900),
      bodyHeadings: Array.from(content?.querySelectorAll("h1,h2,h3") || []).slice(0, 12).map((item) => compactText(item, 100)),
      contentChildCount: content?.children?.length || 0,
      inflight: diagnostic.inflight,
    });
  };
  diagnostic.snapshot = snapshot;

  try {
    new PerformanceObserver((list) => {
      if (!diagnostic.recording) return;
      for (const item of list.getEntries()) {
        diagnostic.longtasks.push({
          start: item.startTime,
          epoch: performance.timeOrigin + item.startTime,
          duration: item.duration,
          name: item.name,
        });
      }
    }).observe({ entryTypes: ["longtask"] });
  } catch {}

  const originalFetch = window.fetch.bind(window);
  window.fetch = (...args) => {
    const url = String(args[0]?.url || args[0] || "");
    const item = { url, startEpoch: epoch(), startPerf: performance.now(), endEpoch: 0, outcome: "pending" };
    if (diagnostic.recording) diagnostic.fetches.push(item);
    diagnostic.inflight += 1;
    diagnostic.maxInflight = Math.max(diagnostic.maxInflight, diagnostic.inflight);
    return originalFetch(...args).then((response) => {
      item.outcome = `http-${response.status}`;
      return response;
    }, (error) => {
      item.outcome = error?.name || "rejected";
      throw error;
    }).finally(() => {
      diagnostic.inflight = Math.max(0, diagnostic.inflight - 1);
      item.endEpoch = epoch();
      item.durationMs = item.endEpoch - item.startEpoch;
    });
  };

  document.addEventListener("pointerdown", (event) => {
    const link = event.target?.closest?.("a.nav-item[href]");
    if (!link || !diagnostic.recording) return;
    diagnostic.clicks.push({
      phase: "pointerdown",
      epoch: epoch(),
      pathAtInput: normalize(location.pathname),
      desired: normalize(new URL(link.href, location.href).pathname),
      trusted: event.isTrusted,
      x: event.clientX,
      y: event.clientY,
    });
    snapshot("pointerdown");
  }, true);
  document.addEventListener("click", (event) => {
    const link = event.target?.closest?.("a.nav-item[href]");
    if (!link || !diagnostic.recording) return;
    diagnostic.clicks.push({
      phase: "click",
      epoch: epoch(),
      pathAtInput: normalize(location.pathname),
      desired: normalize(new URL(link.href, location.href).pathname),
      trusted: event.isTrusted,
      x: event.clientX,
      y: event.clientY,
    });
    snapshot("click");
  }, true);

  let lastPath = normalize(location.pathname);
  const pollPath = () => {
    const path = normalize(location.pathname);
    if (path !== lastPath) {
      diagnostic.routeChanges.push({ epoch: epoch(), from: lastPath, to: path, inflight: diagnostic.inflight });
      lastPath = path;
      requestAnimationFrame(() => snapshot("route-change-raf"));
    }
  };
  setInterval(pollPath, 4);

  let mutationPending = false;
  const mutationObserver = new MutationObserver((records) => {
    if (!diagnostic.recording || mutationPending) return;
    mutationPending = true;
    const mutationEpoch = epoch();
    requestAnimationFrame(() => {
      mutationPending = false;
      diagnostic.mutations.push({ epoch: mutationEpoch, records: records.length });
      snapshot("mutation-raf");
    });
  });
  const observeDocument = () => {
    if (!document.documentElement) {
      setTimeout(observeDocument, 0);
      return;
    }
    mutationObserver.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
  };
  observeDocument();

  let expectedTimer = performance.now() + 16;
  setInterval(() => {
    const now = performance.now();
    const lateBy = now - expectedTimer;
    if (diagnostic.recording && lateBy > 20) {
      diagnostic.eventLoopGaps.push({ epoch: performance.timeOrigin + now, lateBy });
    }
    expectedTimer = now + 16;
  }, 16);

  const sampleFrame = () => {
    snapshot("raf");
    requestAnimationFrame(sampleFrame);
  };
  requestAnimationFrame(sampleFrame);

  diagnostic.reset = () => {
    diagnostic.clicks = [];
    diagnostic.samples = [];
    diagnostic.routeChanges = [];
    diagnostic.mutations = [];
    diagnostic.longtasks = [];
    diagnostic.eventLoopGaps = [];
    diagnostic.fetches = [];
    diagnostic.maxInflight = diagnostic.inflight;
    diagnostic.referenceStartEpoch = epoch();
    diagnostic.recording = true;
    snapshot("recording-start");
    return diagnostic.referenceStartEpoch;
  };
  diagnostic.stop = () => {
    snapshot("recording-stop");
    diagnostic.recording = false;
    return epoch();
  };
})();
"""


class StreamingCdpSession:
    """Small synchronous CDP client with non-blocking screencast acknowledgements."""

    def __init__(self, ws_url: str) -> None:
        from websockets.sync.client import connect

        self.ws = connect(ws_url, max_size=128 * 1024 * 1024)
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._responses: dict[int, queue.Queue] = {}
        self.events: queue.Queue = queue.Queue()
        self.frames: list[dict[str, Any]] = []
        self.capture_frames = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _allocate_id(self, expect_response: bool) -> tuple[int, queue.Queue | None]:
        with self._id_lock:
            self._next_id += 1
            message_id = self._next_id
            response_queue = queue.Queue() if expect_response else None
            if response_queue is not None:
                self._responses[message_id] = response_queue
            return message_id, response_queue

    def _send(self, message: dict[str, Any]) -> None:
        with self._send_lock:
            self.ws.send(json.dumps(message))

    def _send_no_reply(self, method: str, params: dict[str, Any]) -> None:
        message_id, _ = self._allocate_id(False)
        self._send({"id": message_id, "method": method, "params": params})

    def _read_loop(self) -> None:
        try:
            for raw in self.ws:
                message = json.loads(raw)
                if "id" in message:
                    pending = self._responses.get(message["id"])
                    if pending is not None:
                        pending.put(message)
                    continue
                if message.get("method") == "Page.screencastFrame":
                    params = message.get("params", {})
                    self._send_no_reply(
                        "Page.screencastFrameAck",
                        {"sessionId": params.get("sessionId")},
                    )
                    if self.capture_frames:
                        metadata = dict(params.get("metadata", {}))
                        self.frames.append(
                            {
                                "data": params.get("data", ""),
                                "metadata": metadata,
                                "receivedEpoch": time.time() * 1000,
                            }
                        )
                    continue
                self.events.put((time.monotonic(), message))
        except Exception as error:  # pragma: no cover - diagnostic failure is reported by caller
            self.events.put((time.monotonic(), {"method": "Diagnostic.socketError", "error": repr(error)}))

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        message_id, response_queue = self._allocate_id(True)
        assert response_queue is not None
        self._send({"id": message_id, "method": method, "params": params or {}})
        try:
            message = response_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError(f"CDP call timed out: {method}") from error
        finally:
            self._responses.pop(message_id, None)
        if "error" in message:
            raise RuntimeError(f"CDP error in {method}: {message['error']}")
        return message.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"JavaScript evaluation failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def drain_events(self) -> list[tuple[float, dict]]:
        result: list[tuple[float, dict]] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def _wait_for_json(url: str, timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:  # noqa: BLE001 - retry endpoint startup
            last_error = error
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error!r}")


def _normalize_path(value: str) -> str:
    stripped = str(value or "").split("?", 1)[0].split("#", 1)[0].strip("/")
    return f"/{stripped}" if stripped else "/"


def _dom_probe_expression() -> str:
    return r"""
(() => {
  const rect = (node) => {
    if (!node) return null;
    const item = node.getBoundingClientRect();
    return { x: item.x, y: item.y, width: item.width, height: item.height };
  };
  const normalize = (value) => `/${String(value || "").replace(/^\/+|\/+$/g, "")}`;
  const content = document.querySelector(".content");
  return {
    path: normalize(location.pathname),
    viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
    title: document.querySelector(".title-block h1")?.innerText?.trim() || "",
    active: document.querySelector('.nav-item[aria-current="page"] .nav-label')?.innerText?.trim() || "",
    marker: document.querySelector("[data-novelforge-page-title]")?.getAttribute("data-novelforge-page-title") || "",
    bodyText: String(content?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1400),
    bodyHeadings: Array.from(content?.querySelectorAll("h1,h2,h3") || []).slice(0, 14).map((node) => node.innerText.trim()),
    projectCharacters: (() => {
      const toggles = Array.from(content?.querySelectorAll('.structured-card button[aria-expanded]') || []);
      return {
        count: toggles.length,
        expandedCount: toggles.filter((node) => node.getAttribute('aria-expanded') === 'true').length,
        states: toggles.map((node) => node.getAttribute('aria-expanded')),
      };
    })(),
    projectBriefEditor: (() => {
      const buttons = Array.from(content?.querySelectorAll('button') || []);
      const editButton = buttons.find((node) => node.innerText.trim() === '编辑起始需求');
      const collapseButton = buttons.find((node) => node.innerText.trim() === '收起编辑');
      return {
        summaryVisible: !!editButton && !collapseButton,
        fullFormMounted: !!content?.querySelector('.brief-section'),
        editButtonVisible: !!editButton,
        collapseButtonVisible: !!collapseButton,
        editButtonRect: rect(editButton),
        collapseButtonRect: rect(collapseButton),
      };
    })(),
    titleRect: rect(document.querySelector(".title-block")),
    contentRect: rect(content),
    navRect: rect(document.querySelector(".nav")),
    navItems: Array.from(document.querySelectorAll("a.nav-item[href]")).map((node) => ({
      route: normalize(new URL(node.href, location.href).pathname),
      label: node.querySelector(".nav-label")?.innerText?.trim() || "",
      rect: rect(node),
    })),
  };
})()
"""


def _parse_network(events: list[tuple[float, dict]]) -> dict[str, Any]:
    requests: dict[str, dict[str, Any]] = {}
    timeline: list[tuple[float, int]] = []
    socket_errors: list[str] = []
    for _received, event in events:
        method = event.get("method", "")
        params = event.get("params", {})
        if method == "Diagnostic.socketError":
            socket_errors.append(event.get("error", "unknown"))
        elif method == "Network.requestWillBeSent":
            request_id = params.get("requestId")
            timestamp = float(params.get("timestamp", 0.0)) * 1000
            request = params.get("request", {})
            requests[request_id] = {
                "id": request_id,
                "url": request.get("url", ""),
                "method": request.get("method", ""),
                "type": params.get("type", ""),
                "startTs": timestamp,
            }
            timeline.append((timestamp, 1))
        elif method == "Network.responseReceived":
            item = requests.get(params.get("requestId"))
            if item is not None:
                response = params.get("response", {})
                item.update(
                    {
                        "status": response.get("status"),
                        "mime": response.get("mimeType", ""),
                        "protocol": response.get("protocol", ""),
                        "fromCache": bool(
                            response.get("fromDiskCache")
                            or response.get("fromPrefetchCache")
                            or response.get("fromServiceWorker")
                        ),
                        "responseTs": float(params.get("timestamp", 0.0)) * 1000,
                    }
                )
        elif method == "Network.loadingFinished":
            item = requests.get(params.get("requestId"))
            if item is not None and not item.get("finished"):
                finish_ts = float(params.get("timestamp", 0.0)) * 1000
                item.update(
                    {
                        "finished": True,
                        "finishTs": finish_ts,
                        "durationMs": round(finish_ts - item["startTs"], 2),
                        "ttfbMs": round(item.get("responseTs", finish_ts) - item["startTs"], 2),
                        "bytes": params.get("encodedDataLength", 0),
                    }
                )
                timeline.append((finish_ts, -1))
        elif method == "Network.loadingFailed":
            item = requests.get(params.get("requestId"))
            if item is not None and not item.get("finished"):
                finish_ts = float(params.get("timestamp", 0.0)) * 1000
                item.update(
                    {
                        "finished": True,
                        "finishTs": finish_ts,
                        "durationMs": round(finish_ts - item["startTs"], 2),
                        "failed": params.get("errorText", "unknown"),
                        "canceled": bool(params.get("canceled")),
                        "blockedReason": params.get("blockedReason", ""),
                    }
                )
                timeline.append((finish_ts, -1))

    active = 0
    max_active = 0
    for _timestamp, delta in sorted(timeline, key=lambda item: (item[0], -item[1])):
        active += delta
        max_active = max(max_active, active)
    result = list(requests.values())
    result.sort(key=lambda item: item.get("startTs", 0.0))
    urls = Counter(item["url"] for item in result)
    next_requests = [item for item in result if "__next" in item.get("url", "") or ".txt" in item.get("url", "")]
    api_requests = [item for item in result if "/api/" in item.get("url", "")]
    return {
        "requestCount": len(result),
        "completedCount": sum(bool(item.get("finished")) for item in result),
        "pendingCount": sum(not item.get("finished") for item in result),
        "failedCount": sum(bool(item.get("failed")) for item in result),
        "canceledCount": sum(bool(item.get("canceled")) for item in result),
        "maxConcurrent": max_active,
        "duplicateRequestCount": sum(count - 1 for count in urls.values() if count > 1),
        "nextRouteRequestCount": len(next_requests),
        "apiRequestCount": len(api_requests),
        "apiBytes": sum(int(item.get("bytes", 0)) for item in api_requests),
        "socketErrors": socket_errors,
        "requests": result,
    }


def _text_similarity(left: str, right: str) -> float:
    """Dice coefficient over two-character shingles; robust for Chinese text."""
    def shingles(value: str) -> set[str]:
        compact = "".join(str(value or "").split())
        return {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}

    left_items = shingles(left)
    right_items = shingles(right)
    if not left_items or not right_items:
        return 0.0
    return 2 * len(left_items & right_items) / (len(left_items) + len(right_items))


def _classify_dom_samples(samples: list[dict], references: dict[str, dict]) -> dict[str, Any]:
    expected_by_title: dict[str, str] = {}
    expected_by_nav: dict[str, str] = {}
    for route, reference in references.items():
        for value in (reference.get("title", ""), reference.get("marker", "")):
            if value:
                expected_by_title[value] = route
        if reference.get("active"):
            expected_by_nav[reference["active"]] = route
    inconsistent: list[dict] = []
    duplicate_title_samples: list[dict] = []
    duplicate_marker_samples: list[dict] = []
    duplicate_active_samples: list[dict] = []
    url_pending_samples: list[dict] = []
    classified: list[dict] = []
    for sample in samples:
        path = _normalize_path(sample.get("path", ""))
        title_route = expected_by_title.get(sample.get("title", ""), "unknown")
        marker_route = expected_by_title.get(sample.get("marker", ""), "unknown")
        nav_route = expected_by_nav.get(sample.get("active", ""), "unknown")
        similarities = sorted(
            (
                (_text_similarity(sample.get("bodyText", ""), reference.get("bodyText", "")), route)
                for route, reference in references.items()
            ),
            reverse=True,
        )
        body_score, body_route = similarities[0] if similarities else (0.0, "unknown")
        runner_score = similarities[1][0] if len(similarities) > 1 else 0.0
        if body_score < 0.18 or body_score - runner_score < 0.025:
            body_route = "unknown"
        item = {
            "epoch": sample.get("epoch"),
            "reason": sample.get("reason"),
            "path": path,
            "titleRoute": title_route,
            "markerRoute": marker_route,
            "navRoute": nav_route,
            "bodyRoute": body_route,
            "bodyScore": round(body_score, 4),
            "titleCount": sample.get("titleCount"),
            "markerCount": sample.get("markerCount"),
            "activeCount": sample.get("activeCount"),
            "inflight": sample.get("inflight"),
        }
        classified.append(item)
        if sample.get("titleCount", 0) != 1:
            duplicate_title_samples.append(item)
        if sample.get("markerCount", 0) != 1:
            duplicate_marker_samples.append(item)
        if sample.get("activeCount", 0) != 1:
            duplicate_active_samples.append(item)
        # URL can advance before a concurrent React transition has committed.
        # That is navigation pending, not visual corruption.  A real semantic
        # inconsistency is disagreement among the page marker, topbar title,
        # active nav and identifiable body.
        semantic_routes = [route for route in (marker_route, title_route, nav_route, body_route) if route != "unknown"]
        if len(set(semantic_routes)) > 1:
            inconsistent.append(item)
        agreed_semantic = marker_route if marker_route != "unknown" else title_route
        if agreed_semantic != "unknown" and path != agreed_semantic:
            url_pending_samples.append(item)
    return {
        "sampleCount": len(samples),
        "inconsistentCount": len(inconsistent),
        "duplicateOrMissingTitleCount": len(duplicate_title_samples),
        "duplicateOrMissingMarkerCount": len(duplicate_marker_samples),
        "duplicateOrMissingActiveCount": len(duplicate_active_samples),
        "urlAheadOfSemanticCount": len(url_pending_samples),
        "inconsistentSamples": inconsistent[:500],
        "duplicateTitleSamples": duplicate_title_samples[:100],
        "duplicateMarkerSamples": duplicate_marker_samples[:100],
        "duplicateActiveSamples": duplicate_active_samples[:100],
        "urlAheadOfSemanticSamples": url_pending_samples[:500],
        "classified": classified,
    }


def _scaled_crop(image, rect: dict, viewport: dict, target_width: int, target_height: int):
    from PySide6.QtCore import Qt

    width_scale = image.width() / max(1.0, float(viewport.get("width", image.width())))
    height_scale = image.height() / max(1.0, float(viewport.get("height", image.height())))
    x = max(0, round(float(rect.get("x", 0)) * width_scale))
    y = max(0, round(float(rect.get("y", 0)) * height_scale))
    width = max(1, round(float(rect.get("width", 1)) * width_scale))
    height = max(1, round(float(rect.get("height", 1)) * height_scale))
    width = min(width, image.width() - x)
    height = min(height, image.height() - y)
    return image.copy(x, y, width, height).scaled(
        target_width,
        target_height,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


def _rgb_signature(image) -> bytes:
    from PySide6.QtGui import QCursor, QImage

    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = converted.bits().tobytes()
    # One-byte approximate luminance keeps template matching cheap enough for
    # hundreds of compositor frames without adding a NumPy dependency.
    result = bytearray(converted.width() * converted.height())
    target = 0
    for source in range(0, len(raw) - 3, 4):
        result[target] = (raw[source] * 2 + raw[source + 1] * 5 + raw[source + 2]) // 8
        target += 1
    return bytes(result)


def _signature_distance(left: bytes, right: bytes) -> float:
    size = min(len(left), len(right))
    if not size:
        return 255.0
    return sum(abs(left[index] - right[index]) for index in range(size)) / size


def _luminance(image) -> float:
    from PySide6.QtGui import QImage

    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = converted.bits().tobytes()
    total = 0.0
    count = 0
    for index in range(0, len(raw) - 3, 4):
        total += 0.2126 * raw[index] + 0.7152 * raw[index + 1] + 0.0722 * raw[index + 2]
        count += 1
    return total / max(1, count)


def _component_rects(geometry: dict) -> dict[str, dict]:
    title = dict(geometry["titleRect"])
    content = dict(geometry["contentRect"])
    nav = dict(geometry["navRect"])
    title["width"] = min(float(title["width"]), 520.0)
    content["x"] = float(content["x"]) + 18
    content["y"] = float(content["y"]) + 18
    content["width"] = max(1.0, float(content["width"]) - 36)
    content["height"] = min(420.0, max(1.0, float(content["height"]) - 18))
    return {"nav": nav, "title": title, "body": content}


def _build_visual_templates(reference_images: dict[str, Any], geometry: dict) -> tuple[dict, dict]:
    viewport = geometry["viewport"]
    rects = _component_rects(geometry)
    sizes = {"nav": (48, 120), "title": (96, 24), "body": (100, 60)}
    templates: dict[str, dict[str, bytes]] = {}
    for route, image in reference_images.items():
        templates[route] = {
            name: _rgb_signature(_scaled_crop(image, rects[name], viewport, *sizes[name]))
            for name in sizes
        }
    cross_distances: dict[str, list[float]] = defaultdict(list)
    routes = list(reference_images)
    for name in sizes:
        for left_index, left_route in enumerate(routes):
            for right_route in routes[left_index + 1 :]:
                cross_distances[name].append(
                    _signature_distance(templates[left_route][name], templates[right_route][name])
                )
    thresholds = {
        name: max(1.5, min(values or [8.0]) * 0.72)
        for name, values in cross_distances.items()
    }
    diagnostics = {
        "thresholds": {name: round(value, 4) for name, value in thresholds.items()},
        "crossDistance": {
            name: {
                "min": round(min(values or [0]), 4),
                "median": round(statistics.median(values or [0]), 4),
                "max": round(max(values or [0]), 4),
            }
            for name, values in cross_distances.items()
        },
    }
    return {"signatures": templates, "thresholds": thresholds, "rects": rects, "sizes": sizes}, diagnostics


def _nearest_template(signature: bytes, templates: dict[str, dict[str, bytes]], component: str, threshold: float) -> dict:
    distances = sorted(
        (_signature_distance(signature, route_templates[component]), route)
        for route, route_templates in templates.items()
    )
    best_distance, best_route = distances[0]
    runner_distance = distances[1][0] if len(distances) > 1 else 255.0
    recognized = best_distance <= threshold
    return {
        "route": best_route if recognized else "unknown",
        "nearestRoute": best_route,
        "distance": round(best_distance, 4),
        "runnerDistance": round(runner_distance, 4),
        "threshold": round(threshold, 4),
    }


def _classify_frames(
    raw_frames: list[dict],
    output_dir: Path,
    references: dict[str, dict],
    geometry: dict,
    visual_templates: dict,
    dom_classification: dict,
    clicks: list[dict],
    *,
    frames_directory: str = "frames",
    frame_prefix: str = "frame",
) -> dict[str, Any]:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage

    frames_dir = output_dir / frames_directory
    frames_dir.mkdir(parents=True, exist_ok=True)
    viewport = geometry["viewport"]
    rects = visual_templates["rects"]
    sizes = visual_templates["sizes"]
    templates = visual_templates["signatures"]
    thresholds = visual_templates["thresholds"]
    nav_items = geometry["navItems"]
    dom_samples = dom_classification["classified"]
    click_events = [item for item in clicks if item.get("phase") == "click"]
    classified_frames: list[dict] = []
    previous_full_signature: bytes | None = None

    def preceding(items: list[dict], epoch: float) -> dict | None:
        candidate = None
        for item in items:
            if float(item.get("epoch", 0)) <= epoch:
                candidate = item
            else:
                break
        return candidate

    def stale_details(epoch: float, visual_route: str, dom_route: str) -> tuple[float | None, int]:
        if visual_route in ("unknown", dom_route) or dom_route == "unknown":
            return None, 0
        prior = [item for item in dom_samples if float(item.get("epoch", 0)) <= epoch]
        last_visual_index = -1
        for item_index, item in enumerate(prior):
            semantic = item.get("markerRoute", "unknown")
            if semantic == "unknown":
                semantic = item.get("titleRoute", "unknown")
            if semantic == visual_route:
                last_visual_index = item_index
        if last_visual_index < 0:
            return None, 0
        departure_epoch = None
        commits: list[str] = []
        previous_route = visual_route
        for item in prior[last_visual_index + 1 :]:
            semantic = item.get("markerRoute", "unknown")
            if semantic == "unknown":
                semantic = item.get("titleRoute", "unknown")
            if semantic == "unknown" or semantic == previous_route:
                continue
            if departure_epoch is None:
                departure_epoch = float(item.get("epoch", epoch))
            commits.append(semantic)
            previous_route = semantic
        if departure_epoch is None:
            return None, len(commits)
        return max(0.0, epoch - departure_epoch), len(commits)

    for index, raw_frame in enumerate(raw_frames, 1):
        payload = base64.b64decode(raw_frame.get("data", ""))
        raw_bgra = raw_frame.get("rawBgra")
        if raw_bgra:
            width = int(raw_frame.get("width", 0))
            height = int(raw_frame.get("height", 0))
            image = QImage(raw_bgra, width, height, width * 4, QImage.Format.Format_RGB32).copy()
        else:
            image = QImage.fromData(QByteArray(payload))
        if image.isNull():
            classified_frames.append({"index": index, "valid": False})
            continue
        filename = f"{frame_prefix}-{index:04d}.png"
        # Keep exact compositor bytes when Chromium emitted PNG. QImage save is
        # the fallback for engines that silently return a different format.
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            (frames_dir / filename).write_bytes(payload)
        else:
            image.save(str(frames_dir / filename), "PNG")

        metadata = raw_frame.get("metadata", {})
        frame_epoch = float(metadata.get("timestamp", 0.0)) * 1000
        if frame_epoch < 1_000_000_000_000:
            frame_epoch = float(raw_frame.get("receivedEpoch", 0.0))
        component_results: dict[str, dict] = {}
        for component in ("nav", "title", "body"):
            signature = _rgb_signature(
                _scaled_crop(image, rects[component], viewport, *sizes[component])
            )
            component_results[component] = _nearest_template(
                signature, templates, component, thresholds[component]
            )

        nav_scores: dict[str, float] = {}
        for nav_item in nav_items:
            item_rect = dict(nav_item["rect"])
            # Sample a text-free strip near the right edge of every nav row.
            item_rect["x"] = float(item_rect["x"]) + max(5.0, float(item_rect["width"]) - 34.0)
            item_rect["y"] = float(item_rect["y"]) + 9.0
            item_rect["width"] = 20.0
            item_rect["height"] = max(2.0, float(item_rect["height"]) - 18.0)
            nav_scores[_normalize_path(nav_item["route"])] = round(
                _luminance(_scaled_crop(image, item_rect, viewport, 10, 24)), 2
            )
        sorted_nav = sorted((score, route) for route, score in nav_scores.items())
        brightest_score, brightest_route = sorted_nav[-1]
        second_score = sorted_nav[-2][0] if len(sorted_nav) > 1 else 0.0
        visual_routes = {
            "nav": brightest_route,
            "title": component_results["title"]["route"],
            "body": component_results["body"]["route"],
        }
        known_visual = [value for value in visual_routes.values() if value != "unknown"]
        mixed = len(set(known_visual)) > 1
        dom = preceding(dom_samples, frame_epoch)
        click = preceding(click_events, frame_epoch)
        dom_path = dom.get("path") if dom else "unknown"
        dom_semantic_route = dom.get("markerRoute", "unknown") if dom else "unknown"
        if dom_semantic_route == "unknown" and dom:
            dom_semantic_route = dom.get("titleRoute", "unknown")
        latest_intent = click.get("desired") if click else "unknown"
        coherent_visual_route = known_visual[0] if known_visual and len(set(known_visual)) == 1 else "unknown"
        stale_ms, commits_behind = stale_details(frame_epoch, coherent_visual_route, dom_semantic_route)

        full_signature = _rgb_signature(
            _scaled_crop(
                image,
                {"x": 0, "y": 0, "width": viewport["width"], "height": viewport["height"]},
                viewport,
                90,
                56,
            )
        )
        previous_distance = (
            _signature_distance(full_signature, previous_full_signature)
            if previous_full_signature is not None
            else None
        )
        previous_full_signature = full_signature
        classified_frames.append(
            {
                "index": index,
                "file": filename,
                "valid": True,
                "epoch": round(frame_epoch, 3),
                "receivedEpoch": round(float(raw_frame.get("receivedEpoch", 0.0)), 3),
                "width": image.width(),
                "height": image.height(),
                "domPath": dom_path,
                "domSemanticRoute": dom_semantic_route,
                "latestIntent": latest_intent,
                "visualRoutes": visual_routes,
                "mixed": mixed,
                "staleCompositor": stale_ms is not None,
                "staleMs": round(stale_ms, 3) if stale_ms is not None else None,
                "commitsBehind": commits_behind,
                "visualVsDomMismatch": any(
                    route != "unknown" and dom_semantic_route != "unknown" and route != dom_semantic_route
                    for route in visual_routes.values()
                ),
                "visualVsLatestIntentMismatch": any(
                    route != "unknown" and latest_intent != "unknown" and route != latest_intent
                    for route in visual_routes.values()
                ),
                "navScores": nav_scores,
                "navBrightnessGap": round(brightest_score - second_score, 2),
                "components": component_results,
                "previousFrameDistance": round(previous_distance, 4) if previous_distance is not None else None,
            }
        )

    valid = [item for item in classified_frames if item.get("valid")]
    mixed_frames = [item for item in valid if item.get("mixed")]
    dom_mismatch = [item for item in valid if item.get("visualVsDomMismatch")]
    stale_frames = [item for item in valid if item.get("staleCompositor")]
    repeated = [item for item in valid if (item.get("previousFrameDistance") or 999) < 0.08]
    frame_distances = [float(item["previousFrameDistance"]) for item in valid if item.get("previousFrameDistance") is not None]
    frame_intervals = [
        max(0.0, float(current["epoch"]) - float(previous["epoch"]))
        for previous, current in zip(valid, valid[1:])
    ]
    click_phase_frames: list[dict] = []
    if click_events:
        first_click_epoch = float(click_events[0]["epoch"])
        last_click_epoch = float(click_events[-1]["epoch"])
        click_phase_frames = [
            item for item in valid if first_click_epoch <= float(item["epoch"]) <= last_click_epoch + 120
        ]

    def percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))]

    return {
        "frameCount": len(classified_frames),
        "validFrameCount": len(valid),
        "mixedFrameCount": len(mixed_frames),
        "staleCompositorFrameCount": len(stale_frames),
        "staleCompositorMedianMs": round(statistics.median(item["staleMs"] for item in stale_frames), 3) if stale_frames else 0,
        "staleCompositorMaxMs": round(max((item["staleMs"] for item in stale_frames), default=0), 3),
        "maxCommitsBehind": max((int(item.get("commitsBehind", 0)) for item in stale_frames), default=0),
        "visualVsDomMismatchCount": len(dom_mismatch),
        "visualVsLatestIntentMismatchCount": sum(item.get("visualVsLatestIntentMismatch", False) for item in valid),
        "repeatedFrameCount": len(repeated),
        "clickPhaseFrameCount": len(click_phase_frames),
        "clickPhaseStaleFrameCount": sum(item.get("staleCompositor", False) for item in click_phase_frames),
        "frameIntervalMedianMs": round(statistics.median(frame_intervals), 3) if frame_intervals else 0,
        "frameIntervalP90Ms": round(percentile(frame_intervals, 0.9), 3),
        "frameIntervalMaxMs": round(max(frame_intervals, default=0), 3),
        "frameDiffMedian": round(statistics.median(frame_distances), 4) if frame_distances else 0,
        "frameDiffP90": round(percentile(frame_distances, 0.9), 4),
        "frameDiffMax": round(max(frame_distances, default=0), 4),
        "mixedFrames": [item["file"] for item in mixed_frames],
        "staleCompositorFrames": [item["file"] for item in stale_frames],
        "visualVsDomMismatchFrames": [item["file"] for item in dom_mismatch],
        "frames": classified_frames,
    }


def _gpu_info(debugging_port: int) -> dict[str, Any]:
    version = _wait_for_json(f"http://127.0.0.1:{debugging_port}/json/version")
    session = StreamingCdpSession(version["webSocketDebuggerUrl"])
    try:
        system_info = session.call("SystemInfo.getInfo")
        gpu = system_info.get("gpu", {})
        return {
            "devices": gpu.get("devices", []),
            "featureStatus": gpu.get("featureStatus", {}),
            "auxAttributes": gpu.get("auxAttributes", {}),
            "driverBugWorkarounds": gpu.get("driverBugWorkarounds", []),
        }
    finally:
        session.close()


def _summarize_intent_latency(diagnostic: dict) -> dict[str, Any]:
    clicks = [item for item in diagnostic.get("clicks", []) if item.get("phase") == "click"]
    samples = diagnostic.get("samples", [])
    route_changes = diagnostic.get("routeChanges", [])
    final_click = clicks[-1] if clicks else None
    final_match_ms = None
    if final_click:
        for sample in samples:
            if sample.get("epoch", 0) >= final_click["epoch"] and _normalize_path(sample.get("path", "")) == final_click["desired"]:
                final_match_ms = float(sample["epoch"]) - float(final_click["epoch"])
                break
    trusted_count = sum(bool(item.get("trusted")) for item in clicks)
    return {
        "pointerClickCount": len(clicks),
        "trustedClickCount": trusted_count,
        "routeCommitCount": len(route_changes),
        "coalescedOrSupersededCount": max(0, len(clicks) - len(route_changes)),
        "finalIntent": final_click.get("desired") if final_click else None,
        "finalIntentDomMatchMs": round(final_match_ms, 2) if final_match_ms is not None else None,
        "finalPath": _normalize_path(samples[-1].get("path", "")) if samples else None,
    }


def _summarize_each_intent(diagnostic: dict, dom: dict, visual: dict, stop_epoch: float) -> dict[str, Any]:
    clicks = [item for item in diagnostic.get("clicks", []) if item.get("phase") == "click"]
    dom_samples = dom.get("classified", [])
    frames = [item for item in visual.get("frames", []) if item.get("valid")]

    def semantic_route(item: dict) -> str:
        route = item.get("markerRoute", "unknown")
        return item.get("titleRoute", "unknown") if route == "unknown" else route

    def visual_route(item: dict) -> str:
        routes = [route for route in item.get("visualRoutes", {}).values() if route != "unknown"]
        return routes[0] if routes and len(set(routes)) == 1 else "unknown"

    dom_latencies: list[float] = []
    visual_latencies: list[float] = []
    intent_records: list[dict[str, Any]] = []
    for index, click in enumerate(clicks):
        start = float(click["epoch"])
        end = float(clicks[index + 1]["epoch"]) if index + 1 < len(clicks) else float(stop_epoch)
        desired = click.get("desired", "unknown")
        dom_match = next(
            (
                item for item in dom_samples
                if start <= float(item.get("epoch", 0)) < end and semantic_route(item) == desired
            ),
            None,
        )
        visual_match = next(
            (
                item for item in frames
                if start <= float(item.get("epoch", 0)) < end and visual_route(item) == desired
            ),
            None,
        )
        dom_ms = float(dom_match["epoch"]) - start if dom_match else None
        visual_ms = float(visual_match["epoch"]) - start if visual_match else None
        if dom_ms is not None:
            dom_latencies.append(dom_ms)
        if visual_ms is not None:
            visual_latencies.append(visual_ms)
        intent_records.append(
            {
                "index": index,
                "desired": desired,
                "epoch": start,
                "supersededAt": end,
                "domBeforeNextClickMs": round(dom_ms, 3) if dom_ms is not None else None,
                "visualBeforeNextClickMs": round(visual_ms, 3) if visual_ms is not None else None,
            }
        )

    def aggregate(values: list[float]) -> dict[str, float | int]:
        ordered = sorted(values)
        return {
            "matched": len(values),
            "medianMs": round(statistics.median(values), 3) if values else 0,
            "p90Ms": round(ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.9) - 1))], 3) if values else 0,
            "maxMs": round(max(values, default=0), 3),
        }

    return {
        "intentCount": len(clicks),
        "dom": aggregate(dom_latencies),
        "visual": aggregate(visual_latencies),
        "records": intent_records,
    }


def _write_summary(output_dir: Path, report: dict) -> None:
    visual = report.get("visual", {})
    screen_visual = report.get("screenVisual", {})
    screen_intent_response = report.get("screenIntentResponse", {})
    dom = report.get("dom", {})
    rapid = report.get("rapid", {})
    intent_response = report.get("intentResponse", {})
    network = report.get("network", {})
    main = report.get("mainThread", {})
    longtasks = report.get("diagnostic", {}).get("longtasks", [])
    feature_status = report.get("gpu", {}).get("featureStatus", {})
    lines = [
        "# NovelForge rapid sidebar diagnosis",
        "",
        f"- Rendering: `{report.get('rendering')}`; input: `{report.get('input')}`",
        f"- Runtime injection: `{json.dumps(report.get('runtimeConfig'), ensure_ascii=False)}`",
        f"- Input: {rapid.get('pointerClickCount', 0)} trusted clicks, {report.get('intervalMs')} ms interval",
        f"- Route commits: {rapid.get('routeCommitCount', 0)}; superseded/coalesced: {rapid.get('coalescedOrSupersededCount', 0)}",
        f"- Final intent DOM match: {rapid.get('finalIntentDomMatchMs')} ms",
        f"- Before next click, DOM/visual intent matches: {intent_response.get('dom', {}).get('matched', 0)}/{intent_response.get('visual', {}).get('matched', 0)} of {intent_response.get('intentCount', 0)}; visual median/P90 {intent_response.get('visual', {}).get('medianMs', 0)}/{intent_response.get('visual', {}).get('p90Ms', 0)} ms",
        f"- Visible-pixel intent matches: {screen_intent_response.get('visual', {}).get('matched', 0)} of {screen_intent_response.get('intentCount', 0)}; median/P90 {screen_intent_response.get('visual', {}).get('medianMs', 0)}/{screen_intent_response.get('visual', {}).get('p90Ms', 0)} ms",
        f"- Compositor frames: {visual.get('validFrameCount', 0)} valid; mixed-component: {visual.get('mixedFrameCount', 0)}; stale-vs-DOM: {visual.get('staleCompositorFrameCount', 0)} (median/max {visual.get('staleCompositorMedianMs', 0)}/{visual.get('staleCompositorMaxMs', 0)} ms, max {visual.get('maxCommitsBehind', 0)} commits behind)",
        f"- Visible Win32 pixels: {screen_visual.get('validFrameCount', 0)} valid; mixed-component: {screen_visual.get('mixedFrameCount', 0)}; stale-vs-DOM: {screen_visual.get('staleCompositorFrameCount', 0)} (median/max {screen_visual.get('staleCompositorMedianMs', 0)}/{screen_visual.get('staleCompositorMaxMs', 0)} ms, max {screen_visual.get('maxCommitsBehind', 0)} commits behind)",
        f"- Compositor update interval median/P90/max: {visual.get('frameIntervalMedianMs', 0)}/{visual.get('frameIntervalP90Ms', 0)}/{visual.get('frameIntervalMaxMs', 0)} ms; screenshot diff median/P90/max: {visual.get('frameDiffMedian', 0)}/{visual.get('frameDiffP90', 0)}/{visual.get('frameDiffMax', 0)}",
        f"- DOM samples: {dom.get('sampleCount', 0)}; semantic inconsistent: {dom.get('inconsistentCount', 0)}; URL-ahead/pending: {dom.get('urlAheadOfSemanticCount', 0)}; duplicate/missing marker: {dom.get('duplicateOrMissingMarkerCount', 0)}; title: {dom.get('duplicateOrMissingTitleCount', 0)}; active nav: {dom.get('duplicateOrMissingActiveCount', 0)}",
        f"- Long tasks: {len(longtasks)}, total {round(sum(float(item.get('duration', 0)) for item in longtasks), 1)} ms, max {round(max((float(item.get('duration', 0)) for item in longtasks), default=0), 1)} ms",
        f"- Main thread task/script/layout/style: {main.get('taskMs', 0)} / {main.get('scriptMs', 0)} / {main.get('layoutMs', 0)} / {main.get('styleMs', 0)} ms",
        f"- Network: {network.get('requestCount', 0)} requests; max concurrent {network.get('maxConcurrent', 0)}; pending {network.get('pendingCount', 0)}; canceled {network.get('canceledCount', 0)}; duplicate {network.get('duplicateRequestCount', 0)}",
        f"- GPU compositing/rasterization: `{feature_status.get('gpu_compositing')}` / `{feature_status.get('rasterization')}`",
        "",
        "See `report.json`, `references/`, and `frames/` for complete evidence.",
    ]
    (output_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--rendering", choices=("gpu", "software"), default="gpu")
    parser.add_argument(
        "--runtime-rendering",
        choices=("gpu", "software"),
        default="",
        help="Runtime hint/gate mode when custom Chromium flags require --rendering gpu",
    )
    parser.add_argument("--input", choices=("qt", "win32"), default="win32" if sys.platform == "win32" else "qt")
    parser.add_argument("--clicks", type=int, default=160)
    parser.add_argument("--interval-ms", type=int, default=30)
    parser.add_argument("--settle-ms", type=int, default=2500)
    parser.add_argument(
        "--screen-capture-interval-ms",
        type=int,
        default=20,
        help="Interval for real Win32 desktop-pixel capture (0 disables it)",
    )
    parser.add_argument("--data-dir", default="")
    parser.add_argument(
        "--verify-projects-editor",
        action="store_true",
        help="Open and collapse the Projects brief editor without saving during reference capture",
    )
    parser.add_argument(
        "--verify-current-page-last",
        action="store_true",
        help="Finish with another-page/current-page clicks and require the final page to match the latter",
    )
    args = parser.parse_args()
    if args.clicks < 100:
        parser.error("--clicks must be at least 100 for a meaningful rapid-switch reproduction")
    if not 10 <= args.interval_ms <= 250:
        parser.error("--interval-ms must be between 10 and 250")
    if args.input == "win32" and sys.platform != "win32":
        parser.error("--input win32 is available only on Windows")

    output_dir = Path(args.output).resolve()
    references_dir = output_dir / "references"
    output_dir.mkdir(parents=True, exist_ok=True)
    references_dir.mkdir(parents=True, exist_ok=True)
    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
    else:
        import tempfile

        data_dir = Path(tempfile.mkdtemp(prefix="novelforge-rapid-diagnostic-"))
    data_dir.mkdir(parents=True, exist_ok=True)

    debugging_port = _reserve_debugging_port()
    os.environ["NOVELFORGE_WINDOWS_DATA_DIR"] = str(data_dir)
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = f"127.0.0.1:{debugging_port}"
    if args.runtime_rendering:
        os.environ["NOVELFORGE_EFFECTIVE_RENDERING"] = args.runtime_rendering
    _configure_profile_rendering(args.rendering)

    from PySide6.QtCore import QPoint, QTimer, Qt
    from PySide6.QtGui import QCursor, QImage
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
    window = WebMainWindow(server.url, paths, server.port)
    window.resize(1440, 900)
    window.move(40, 30)
    window.show()
    window.raise_()
    window.activateWindow()
    screen_recorder = (
        Win32ScreenRecorder(int(window.winId()), interval_ms=args.screen_capture_interval_ms)
        if sys.platform == "win32" and args.screen_capture_interval_ms > 0
        else None
    )

    input_requests: queue.Queue = queue.Queue()

    def pump_input() -> None:
        try:
            item = input_requests.get_nowait()
        except queue.Empty:
            return
        try:
            point = QPoint(int(item["x"]), int(item["y"]))
            if args.input == "qt":
                # QWebEngineView renders through an internal delegate widget;
                # sending the event to the public wrapper does not reach Blink
                # on current Qt 6.  Resolve the actual child under the CSS
                # coordinate and map the point into that widget.
                target = window.web_view.childAt(point) or window.web_view.focusProxy() or window.web_view
                target_point = target.mapFrom(window.web_view, point)
                QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target_point)
            else:
                global_point = window.web_view.mapToGlobal(point)
                # A user can focus another desktop window while the long
                # diagnostic is collecting route references. Real Win32 mouse
                # input targets the visible window under the cursor, so bring
                # the test window back to the foreground before every click.
                # Without this, an unrelated foreground window can receive a
                # valid injected click and look like a navigation timeout.
                hwnd = int(window.winId())
                ctypes.windll.user32.BringWindowToTop(hwnd)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                # QCursor performs Qt's per-monitor DPI conversion before the
                # Win32 button events are injected.
                QCursor.setPos(global_point)
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        except Exception as error:  # noqa: BLE001 - return exact input failure to driver
            item["error"] = repr(error)
        finally:
            item["done"].set()

    input_timer = QTimer()
    input_timer.timeout.connect(pump_input)
    input_timer.start(1)

    state: dict[str, Any] = {"done": False, "report": None}

    def request_click(x: float, y: float, timeout: float = 3.0) -> None:
        done = threading.Event()
        item: dict[str, Any] = {"x": x, "y": y, "done": done}
        input_requests.put(item)
        if not done.wait(timeout):
            raise TimeoutError("Qt input queue did not process the click")
        if item.get("error"):
            raise RuntimeError(item["error"])

    def wait_for_cdp() -> StreamingCdpSession:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            targets = _wait_for_json(f"http://127.0.0.1:{debugging_port}/json", timeout=3)
            for target in targets:
                if target.get("type") == "page" and f"127.0.0.1:{server.port}/" in target.get("url", ""):
                    return StreamingCdpSession(target["webSocketDebuggerUrl"])
            time.sleep(0.2)
        raise RuntimeError("The NovelForge CDP page target did not appear")

    def wait_path(cdp: StreamingCdpSession, route: str, timeout: float = 12.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _normalize_path(cdp.evaluate("location.pathname")) == route:
                return
            time.sleep(0.025)
        raise TimeoutError(f"route did not commit: {route}")

    def drive() -> None:
        report: dict[str, Any] = {
            "rendering": args.rendering,
            "runtimeRenderingRequested": args.runtime_rendering or args.rendering,
            "input": args.input,
            "clicksRequested": args.clicks,
            "intervalMs": args.interval_ms,
            "settleMs": args.settle_ms,
            "dataDir": str(data_dir),
            "baseUrl": base_url,
            "debuggingPort": debugging_port,
            "chromiumFlags": os.getenv("QTWEBENGINE_CHROMIUM_FLAGS", ""),
        }
        cdp: StreamingCdpSession | None = None
        try:
            cdp = wait_for_cdp()
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Network.enable", {"maxTotalBufferSize": 100_000_000, "maxResourceBufferSize": 10_000_000})
            cdp.call("Performance.enable")
            cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": INSTRUMENT_SCRIPT})
            cdp.call("Page.navigate", {"url": f"{base_url}/workbench/"})
            wait_path(cdp, "/workbench")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                ready = cdp.evaluate(
                    "document.readyState === 'complete' && !!document.querySelector('.nav-item') && !!window.__nfRapidDiagnostic"
                )
                if ready:
                    break
                time.sleep(0.05)
            else:
                raise TimeoutError("instrumented page did not become ready")
            time.sleep(1.0)

            report["gpu"] = _gpu_info(debugging_port)
            report["runtimeConfig"] = cdp.evaluate(
                "window.__NOVELFORGE_RUNTIME__ ? JSON.parse(JSON.stringify(window.__NOVELFORGE_RUNTIME__)) : null"
            )
            report["runtimeRenderingMatches"] = (
                str((report["runtimeConfig"] or {}).get("rendering", "")).lower()
                == report["runtimeRenderingRequested"]
            )
            if not report["runtimeRenderingMatches"]:
                raise RuntimeError(
                    "runtime rendering hint mismatch: "
                    f"requested={report['runtimeRenderingRequested']!r}, "
                    f"injected={report['runtimeConfig']!r}"
                )
            profile = window.web_view.page().profile()
            report["webProfile"] = {
                "offTheRecord": profile.isOffTheRecord(),
                "httpCacheType": profile.httpCacheType().name,
                "cachePath": profile.cachePath(),
                "persistentStoragePath": profile.persistentStoragePath(),
            }

            # Establish stable visual and DOM references for all eight routes.
            reference_dom: dict[str, dict] = {}
            reference_images: dict[str, Any] = {}
            screen_reference_images: dict[str, Any] = {}
            screen_references_dir = output_dir / "screen-references"
            if screen_recorder is not None:
                screen_references_dir.mkdir(parents=True, exist_ok=True)
            geometry: dict | None = None
            for route_name, _title in NAV_ROUTES:
                route = f"/{route_name}"
                if _normalize_path(cdp.evaluate("location.pathname")) != route:
                    nav_items = cdp.evaluate(_dom_probe_expression())["navItems"]
                    nav_item = next(item for item in nav_items if _normalize_path(item["route"]) == route)
                    rect = nav_item["rect"]
                    request_click(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
                    wait_path(cdp, route)
                # Let async page data and CSS transitions settle. References
                # deliberately include real data, not empty skeletons.
                time.sleep(0.85)
                probe = cdp.evaluate(_dom_probe_expression())
                reference_dom[route] = probe
                if geometry is None:
                    geometry = probe
                screenshot = cdp.call(
                    "Page.captureScreenshot",
                    {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
                )
                payload = base64.b64decode(screenshot["data"])
                reference_path = references_dir / f"{route_name}.png"
                reference_path.write_bytes(payload)
                image = QImage.fromData(payload)
                if image.isNull():
                    raise RuntimeError(f"invalid reference screenshot: {route}")
                reference_images[route] = image
                if screen_recorder is not None:
                    screen_frame = screen_recorder.capture_once()
                    screen_raw = screen_frame["rawBgra"]
                    screen_width = int(screen_frame["width"])
                    screen_height = int(screen_frame["height"])
                    screen_image = QImage(
                        screen_raw,
                        screen_width,
                        screen_height,
                        screen_width * 4,
                        QImage.Format.Format_RGB32,
                    ).copy()
                    screen_image.save(str(screen_references_dir / f"{route_name}.png"), "PNG")
                    screen_reference_images[route] = screen_image
                if route == "/projects" and args.verify_projects_editor:
                    pre_scroll_probe = probe
                    cdp.evaluate(
                        "window.__nfProjectEditorClicks = [];"
                        "document.addEventListener('pointerdown', (event) => {"
                        " const button = event.target?.closest?.('button');"
                        " if (button) window.__nfProjectEditorClicks.push({phase:'pointerdown', text:button.innerText.trim(), trusted:event.isTrusted, x:event.clientX, y:event.clientY, epoch:performance.timeOrigin+performance.now()});"
                        "}, {capture:true});"
                        "document.addEventListener('click', (event) => {"
                        " const button = event.target?.closest?.('button');"
                        " if (button) window.__nfProjectEditorClicks.push({phase:'click', text:button.innerText.trim(), trusted:event.isTrusted, x:event.clientX, y:event.clientY, epoch:performance.timeOrigin+performance.now()});"
                        "}, {capture:true});"
                    )
                    scrolled = cdp.evaluate(
                        "(() => { const button = Array.from(document.querySelectorAll('button')).find((node) => node.innerText.trim() === '编辑起始需求');"
                        " if (!button) return false; button.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'}); return true; })()"
                    )
                    if not scrolled:
                        raise RuntimeError("Projects summary did not expose the edit button")
                    time.sleep(0.2)
                    probe = cdp.evaluate(_dom_probe_expression())
                    initial_editor = probe.get("projectBriefEditor", {})
                    report["projectsEditorVerification"] = {
                        "preScroll": pre_scroll_probe.get("projectBriefEditor"),
                        "afterScroll": initial_editor,
                        "inputEvents": [],
                        "saved": False,
                    }
                    edit_rect = initial_editor.get("editButtonRect")
                    if not edit_rect:
                        raise RuntimeError("Projects summary did not expose the edit button")
                    request_click(
                        edit_rect["x"] + edit_rect["width"] / 2,
                        edit_rect["y"] + edit_rect["height"] / 2,
                    )
                    time.sleep(0.1)
                    report["projectsEditorVerification"]["inputEvents"] = cdp.evaluate(
                        "JSON.parse(JSON.stringify(window.__nfProjectEditorClicks || []))"
                    )
                    expand_deadline = time.monotonic() + 5
                    expanded_probe = None
                    while time.monotonic() < expand_deadline:
                        expanded_probe = cdp.evaluate(_dom_probe_expression())
                        if expanded_probe.get("projectBriefEditor", {}).get("fullFormMounted"):
                            break
                        time.sleep(0.025)
                    if not expanded_probe or not expanded_probe.get("projectBriefEditor", {}).get("fullFormMounted"):
                        raise TimeoutError("Projects brief editor did not expand")
                    time.sleep(0.25)
                    expanded_screenshot = cdp.call(
                        "Page.captureScreenshot",
                        {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
                    )
                    (references_dir / "projects-expanded.png").write_bytes(
                        base64.b64decode(expanded_screenshot["data"])
                    )
                    cdp.evaluate(
                        "(() => { const button = Array.from(document.querySelectorAll('button')).find((node) => node.innerText.trim() === '收起编辑');"
                        " if (!button) return false; button.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'auto'}); return true; })()"
                    )
                    time.sleep(0.15)
                    expanded_probe = cdp.evaluate(_dom_probe_expression())
                    collapse_rect = expanded_probe.get("projectBriefEditor", {}).get("collapseButtonRect")
                    if not collapse_rect:
                        raise RuntimeError("Expanded Projects editor did not expose the collapse button")
                    request_click(
                        collapse_rect["x"] + collapse_rect["width"] / 2,
                        collapse_rect["y"] + collapse_rect["height"] / 2,
                    )
                    time.sleep(0.1)
                    collapse_deadline = time.monotonic() + 5
                    collapsed_probe = None
                    while time.monotonic() < collapse_deadline:
                        collapsed_probe = cdp.evaluate(_dom_probe_expression())
                        collapsed_editor = collapsed_probe.get("projectBriefEditor", {})
                        if collapsed_editor.get("summaryVisible") and not collapsed_editor.get("fullFormMounted"):
                            break
                        time.sleep(0.025)
                    if not collapsed_probe or not collapsed_probe.get("projectBriefEditor", {}).get("summaryVisible"):
                        raise TimeoutError("Projects brief editor did not return to summary mode")
                    report["projectsEditorVerification"]["inputEvents"] = cdp.evaluate(
                        "JSON.parse(JSON.stringify(window.__nfProjectEditorClicks || []))"
                    )
                    report["projectsEditorVerification"].update({
                        "initial": {
                            "editor": initial_editor,
                            "characters": probe.get("projectCharacters"),
                        },
                        "expanded": {
                            "editor": expanded_probe.get("projectBriefEditor"),
                            "characters": expanded_probe.get("projectCharacters"),
                        },
                        "collapsedAgain": {
                            "editor": collapsed_probe.get("projectBriefEditor"),
                            "characters": collapsed_probe.get("projectCharacters"),
                        },
                    })
            assert geometry is not None
            report["geometry"] = geometry
            report["references"] = reference_dom
            visual_templates, template_diagnostics = _build_visual_templates(reference_images, geometry)
            report["visualTemplateDiagnostics"] = template_diagnostics
            screen_visual_templates = None
            if screen_reference_images:
                screen_visual_templates, screen_template_diagnostics = _build_visual_templates(
                    screen_reference_images,
                    geometry,
                )
                report["screenVisualTemplateDiagnostics"] = screen_template_diagnostics

            # Drain all reference navigation traffic. Only the rapid phase is
            # counted below.
            cdp.drain_events()
            cdp.frames.clear()
            metrics_before = _performance_metrics(cdp)
            cdp.call("Page.startScreencast", {"format": "png", "everyNthFrame": 1})
            cdp.capture_frames = True
            recording_start_epoch = float(cdp.evaluate("window.__nfRapidDiagnostic.reset()"))
            if screen_recorder is not None:
                screen_recorder.start()
            time.sleep(0.12)
            rapid_wall_start = time.monotonic()

            nav_items = cdp.evaluate(_dom_probe_expression())["navItems"]
            nav_centers = {
                _normalize_path(item["route"]): (
                    item["rect"]["x"] + item["rect"]["width"] / 2,
                    item["rect"]["y"] + item["rect"]["height"] / 2,
                )
                for item in nav_items
            }
            cadence: list[dict[str, Any]] = []
            next_deadline = time.perf_counter()
            for index in range(args.clicks):
                route_name = NAV_ROUTES[index % len(NAV_ROUTES)][0]
                route = f"/{route_name}"
                x, y = nav_centers[route]
                before = time.perf_counter()
                request_click(x, y)
                after = time.perf_counter()
                cadence.append(
                    {
                        "index": index,
                        "route": route,
                        "dispatchEpoch": time.time() * 1000,
                        "dispatchMs": round((after - before) * 1000, 3),
                    }
                )
                next_deadline += args.interval_ms / 1000
                remaining = next_deadline - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)

            current_page_last_probe = None
            if args.verify_current_page_last:
                # Let the main stress sequence settle, then reproduce the subtle
                # last-intent edge case: an older push is followed immediately
                # by a click on the page that is still current. The second click
                # must win even though it would normally look like a no-op.
                time.sleep(0.6)
                current_route = _normalize_path(cdp.evaluate("location.pathname"))
                target_route = next(
                    f"/{route_name}"
                    for route_name, _title in NAV_ROUTES
                    if f"/{route_name}" != current_route
                )
                transition_count_before = int(
                    cdp.evaluate("Number(window.__novelforgeDiagnostics?.routeTransitionsStarted || 0)")
                )
                transition_observation = None
                for probe_index, route in enumerate((target_route, current_route), start=args.clicks):
                    x, y = nav_centers[route]
                    before = time.perf_counter()
                    request_click(x, y)
                    after = time.perf_counter()
                    cadence.append(
                        {
                            "index": probe_index,
                            "route": route,
                            "dispatchEpoch": time.time() * 1000,
                            "dispatchMs": round((after - before) * 1000, 3),
                            "currentPageLastProbe": True,
                        }
                    )
                    if route == target_route:
                        # Wait just until router.push has actually started while
                        # location still identifies the old/current page. This
                        # distinguishes the hard in-flight case from two clicks
                        # merely coalescing inside one animation frame.
                        observe_deadline = time.perf_counter() + 0.3
                        while time.perf_counter() < observe_deadline:
                            transition_observation = cdp.evaluate(
                                "({path: location.pathname, started: Number("
                                "window.__novelforgeDiagnostics?.routeTransitionsStarted || 0)})"
                            )
                            observed_path = _normalize_path(transition_observation["path"])
                            if (
                                int(transition_observation["started"]) > transition_count_before
                                or observed_path != current_route
                            ):
                                break
                            time.sleep(0.001)
                current_page_last_probe = {
                    "startPath": current_route,
                    "olderDestination": target_route,
                    "expectedFinalPath": current_route,
                    "transitionObservation": transition_observation,
                    "transitionStartObservedWhileCurrent": bool(
                        transition_observation
                        and int(transition_observation["started"]) > transition_count_before
                        and _normalize_path(transition_observation["path"]) == current_route
                    ),
                }

            click_end_epoch = time.time() * 1000
            time.sleep(args.settle_ms / 1000)
            recording_stop_epoch = float(cdp.evaluate("window.__nfRapidDiagnostic.stop()"))
            if screen_recorder is not None:
                screen_recorder.stop()
            metrics_after = _performance_metrics(cdp)
            cdp.capture_frames = False
            cdp.call("Page.stopScreencast")
            time.sleep(0.15)
            network_events = cdp.drain_events()
            diagnostic = cdp.evaluate("JSON.parse(JSON.stringify(window.__nfRapidDiagnostic))")
            raw_frames = [
                item
                for item in cdp.frames
                if float(item.get("metadata", {}).get("timestamp", 0)) * 1000 >= recording_start_epoch - 100
            ]

            report["recording"] = {
                "startEpoch": recording_start_epoch,
                "clickEndEpoch": click_end_epoch,
                "stopEpoch": recording_stop_epoch,
                "wallMs": round((time.monotonic() - rapid_wall_start) * 1000, 2),
                "cadence": cadence,
                "cadenceDispatchMedianMs": round(statistics.median(item["dispatchMs"] for item in cadence), 3),
                "cadenceDispatchMaxMs": round(max(item["dispatchMs"] for item in cadence), 3),
            }
            report["diagnostic"] = diagnostic
            report["rapid"] = _summarize_intent_latency(diagnostic)
            report["mainThread"] = _metric_delta(metrics_before, metrics_after)
            report["network"] = _parse_network(network_events)
            dom_classification = _classify_dom_samples(diagnostic.get("samples", []), reference_dom)
            report["dom"] = {key: value for key, value in dom_classification.items() if key != "classified"}
            visual_classification = _classify_frames(
                raw_frames,
                output_dir,
                reference_dom,
                geometry,
                visual_templates,
                dom_classification,
                diagnostic.get("clicks", []),
            )
            report["visual"] = visual_classification
            if screen_recorder is not None and screen_visual_templates is not None:
                report["screenCapture"] = {
                    "intervalMs": args.screen_capture_interval_ms,
                    "frameCount": len(screen_recorder.frames),
                    "sourceBounds": next(
                        (item.get("sourceBounds") for item in screen_recorder.frames if item.get("sourceBounds")),
                        None,
                    ),
                    "errors": [item["captureError"] for item in screen_recorder.frames if item.get("captureError")],
                }
                report["screenVisual"] = _classify_frames(
                    [
                        item
                        for item in screen_recorder.frames
                        if float(item.get("receivedEpoch", 0)) >= recording_start_epoch - 100
                    ],
                    output_dir,
                    reference_dom,
                    geometry,
                    screen_visual_templates,
                    dom_classification,
                    diagnostic.get("clicks", []),
                    frames_directory="screen-frames",
                    frame_prefix="screen",
                )
                report["screenIntentResponse"] = _summarize_each_intent(
                    diagnostic,
                    dom_classification,
                    report["screenVisual"],
                    recording_stop_epoch,
                )
            report["intentResponse"] = _summarize_each_intent(
                diagnostic,
                dom_classification,
                visual_classification,
                recording_stop_epoch,
            )
            report["finalProbe"] = cdp.evaluate(_dom_probe_expression())
            if current_page_last_probe is not None:
                current_page_last_probe["actualFinalPath"] = _normalize_path(report["finalProbe"]["path"])
                probe_clicks = [
                    item for item in diagnostic.get("clicks", [])
                    if item.get("phase") == "click"
                ][-2:]
                current_page_last_probe["clicks"] = probe_clicks
                current_page_last_probe["lastClickWasCurrentPage"] = bool(
                    probe_clicks
                    and _normalize_path(probe_clicks[-1].get("pathAtInput", ""))
                    == current_page_last_probe["expectedFinalPath"]
                    and _normalize_path(probe_clicks[-1].get("desired", ""))
                    == current_page_last_probe["expectedFinalPath"]
                )
                current_page_last_probe["passed"] = (
                    current_page_last_probe["actualFinalPath"]
                    == current_page_last_probe["expectedFinalPath"]
                )
                report["currentPageLastIntentProbe"] = current_page_last_probe
            report["shellDiagnostics"] = cdp.evaluate(
                "window.__novelforgeDiagnostics ? JSON.parse(JSON.stringify(window.__novelforgeDiagnostics)) : null"
            )
        except Exception as error:  # noqa: BLE001 - preserve partial evidence
            import traceback

            report["error"] = repr(error)
            report["traceback"] = traceback.format_exc()
        finally:
            if cdp is not None:
                cdp.close()
            state["report"] = report
            state["done"] = True

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()

    quit_timer = QTimer()
    quit_timer.setInterval(100)

    def maybe_quit() -> None:
        if state["done"]:
            application.quit()

    quit_timer.timeout.connect(maybe_quit)
    quit_timer.start()
    hard_stop = QTimer()
    hard_stop.setSingleShot(True)
    hard_stop.timeout.connect(application.quit)
    hard_stop.start(240_000)
    application.exec()
    driver.join(timeout=10)
    server.stop()

    report = state.get("report") or {"error": "diagnostic driver did not finish"}
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_summary(output_dir, report)
    print((output_dir / "SUMMARY.md").read_text(encoding="utf-8"))
    if report.get("error"):
        print(report.get("traceback", ""))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
