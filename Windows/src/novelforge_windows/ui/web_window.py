"""Desktop window hosting the Web edition UI in Qt WebEngine."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QWidget

from novelforge_windows import __version__
from novelforge_windows.config import AppPaths
from novelforge_windows.rendering import request_safe_rendering_once, software_rendering_active
from novelforge_windows.resources import resource_path


_PAGE_BACKGROUND = QColor("#f5f7f8")
_MAX_RENDERER_RELOADS = 1
_PRESENTATION_REFRESH_INTERVAL_MS = 16
_PRESENTATION_REFRESH_FRAMES = 4
# Input-driven bursts cover longer activity tails than URL commits: the sidebar
# collapse transition runs 180 ms, so 12 frames (~190 ms) still heal the final
# presented frame after the animation ends.
_PRESENTATION_INPUT_REFRESH_FRAMES = 12
# Idle safety net: if a torn or empty texture ever sticks without any further
# input (async DOM updates, polling), one repaint per heartbeat re-presents the
# latest completed frame. Confirmed on the reference hybrid-GPU machine, where
# the visible window once stayed black after a sidebar toggle while Chromium
# had long rendered the page; a single update() restored it.
_PRESENTATION_HEARTBEAT_MS = 500
_PRESENTATION_HEARTBEAT_MS_SOFTWARE = 1000
# Events that predict fresh Chromium frames. Pushed repaints deliberately do
# NOT include Paint/UpdateRequest: our own update() calls produce those, which
# would keep the refresh timer alive forever.
_PRESENTATION_ACTIVITY_EVENTS = frozenset(
    {
        QEvent.Type.Wheel,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.KeyPress,
        QEvent.Type.KeyRelease,
        QEvent.Type.TouchBegin,
        QEvent.Type.TouchUpdate,
        QEvent.Type.Resize,
    }
)
_BUILD_MANIFEST_NAME = "_buildManifest.js"
_SAFE_CACHE_NAMESPACE = re.compile(r"[^A-Za-z0-9._-]+")
_RUNTIME_HINT_SCRIPT_NAME = "NovelForgeRuntimeHint"


class _PopupPage(QWebEnginePage):
    def acceptNavigationRequest(self, url: QUrl, navigation_type, is_main_frame: bool) -> bool:
        if is_main_frame:
            QDesktopServices.openUrl(url)
            self.deleteLater()
            return False
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)


class _DesktopPage(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, local_port: int, parent=None) -> None:
        super().__init__(profile, parent)
        self.local_port = local_port

    def acceptNavigationRequest(self, url: QUrl, navigation_type, is_main_frame: bool) -> bool:
        if (
            is_main_frame
            and url.scheme() in {"http", "https"}
            and not (url.host() in {"127.0.0.1", "localhost"} and url.port() == self.local_port)
        ):
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)

    def createWindow(self, _window_type):
        return _PopupPage(self.profile(), self)


def create_desktop_profile(
    paths: AppPaths,
    parent=None,
    *,
    storage_name: str = "NovelForge",
    frontend_dir: str | Path | None = None,
) -> QWebEngineProfile:
    """Create the named disk profile used by the desktop application.

    Cookies and web storage intentionally keep one stable profile path. HTTP
    cache entries are isolated by the bundled Next build id so an upgrade can
    never combine an old HTML/RSC response with the new JavaScript chunks.
    """
    profile_dir = paths.data_dir / "web-profile"
    cache_dir = paths.data_dir / "web-cache" / frontend_cache_namespace(frontend_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile(storage_name, parent)
    profile.setPersistentStoragePath(str(profile_dir))
    profile.setCachePath(str(cache_dir))
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
    profile.setDownloadPath(str(paths.exports_dir))
    return profile


def frontend_build_id(frontend_dir: str | Path | None = None) -> str:
    """Return the build id embedded by Next's static export.

    A static export places ``_buildManifest.js`` below a build-id directory.
    The index reference disambiguates the unlikely case where a development
    output directory contains more than one build. Packaged outputs normally
    contain exactly one candidate.
    """
    root = (
        Path(frontend_dir).expanduser().resolve()
        if frontend_dir is not None
        else resource_path("frontend/out").resolve()
    )
    static_dir = root / "_next" / "static"
    candidates = {
        manifest.parent.name: manifest
        for manifest in static_dir.glob(f"*/{_BUILD_MANIFEST_NAME}")
        if manifest.is_file() and manifest.parent.parent == static_dir
    }
    if candidates:
        try:
            index_html = (root / "index.html").read_text(encoding="utf-8")
        except OSError:
            index_html = ""
        for build_id in candidates:
            manifest_reference = f"/_next/static/{build_id}/{_BUILD_MANIFEST_NAME}"
            if manifest_reference in index_html:
                return build_id
        if len(candidates) == 1:
            return next(iter(candidates))
        # A normal Next export has one manifest. Prefer the most recently
        # written one if a manually copied development output left stale files.
        return max(
            candidates.items(),
            key=lambda item: (item[1].stat().st_mtime_ns, item[0]),
        )[0]
    return f"app-{__version__}"


def frontend_cache_namespace(frontend_dir: str | Path | None = None) -> str:
    """Return a filesystem-safe, deterministic HTTP-cache namespace."""
    build_id = frontend_build_id(frontend_dir)
    safe_build_id = _SAFE_CACHE_NAMESPACE.sub("_", build_id).strip("._-")
    if not safe_build_id:
        safe_build_id = f"app-{__version__}"
    return f"next-{safe_build_id}"


def create_runtime_hint_script() -> QWebEngineScript:
    """Expose immutable desktop rendering metadata before application scripts."""
    rendering = "software" if software_rendering_active() else "gpu"
    script = QWebEngineScript()
    script.setName(_RUNTIME_HINT_SCRIPT_NAME)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(False)
    script.setSourceCode(
        "(() => {\n"
        f'  const runtimeHint = Object.freeze({{ rendering: "{rendering}" }});\n'
        '  Object.defineProperty(window, "__NOVELFORGE_RUNTIME__", {\n'
        "    value: runtimeHint, enumerable: true, configurable: false, writable: false\n"
        "  });\n"
        "})();"
    )
    return script


class WebMainWindow(QMainWindow):
    def __init__(self, url: str, paths: AppPaths, local_port: int) -> None:
        super().__init__()
        self.paths = paths
        self.setWindowTitle("NovelForge")
        self.setMinimumSize(1080, 700)
        # Never open larger than the usable screen: an overflowing window sits
        # partially off-screen, which also stresses the presentation path.
        available = (
            QApplication.primaryScreen().availableGeometry()
            if QApplication.primaryScreen() is not None
            else None
        )
        self.resize(
            min(1440, available.width()) if available is not None else 1440,
            min(900, available.height()) if available is not None else 900,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._closing = False
        self._renderer_reload_count = 0
        self._presentation_refreshes_remaining = 0

        # Create the view first so its page is torn down before the disk profile.
        self.web_view = QWebEngineView(self)
        self.profile = create_desktop_profile(paths, self)
        self.profile.downloadRequested.connect(self._handle_download)
        page = _DesktopPage(self.profile, local_port, self.web_view)
        page.setBackgroundColor(_PAGE_BACKGROUND)
        page.scripts().insert(create_runtime_hint_script())
        page.renderProcessTerminated.connect(self._handle_renderer_termination)
        self.web_view.setPage(page)
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, True)
        self.setCentralWidget(self.web_view)
        # App Router navigation commits inside the existing Chromium document.
        # On Windows, the imported WebEngine texture can reach Qt before the
        # QWidget backing store schedules a full presentation, especially with
        # hybrid/virtual display adapters.  Ask the real view to repaint for a
        # handful of vsync periods after each history URL change so the latest
        # texture is presented instead of leaving stale dirty rectangles.
        self._presentation_refresh_timer = QTimer(self)
        self._presentation_refresh_timer.setInterval(_PRESENTATION_REFRESH_INTERVAL_MS)
        self._presentation_refresh_timer.timeout.connect(self._refresh_web_presentation)
        self.web_view.urlChanged.connect(self._schedule_web_presentation_refresh)
        # The 0.2.5 refresh only ran after URL commits. Real-world tearing also
        # happens after scrolling, sidebar animation and resizes, where no URL
        # change fires: on the hybrid-GPU reference machine the window once
        # settled on a fully black texture while Chromium had long finished
        # rendering, and one explicit update() restored it. Arm the same
        # refresh after input activity inside the view, and keep a slow
        # heartbeat so a torn frame self-heals even when nothing else happens.
        QApplication.instance().installEventFilter(self)
        self._presentation_heartbeat_timer = QTimer(self)
        self._presentation_heartbeat_timer.setInterval(
            _PRESENTATION_HEARTBEAT_MS_SOFTWARE
            if software_rendering_active()
            else _PRESENTATION_HEARTBEAT_MS
        )
        self._presentation_heartbeat_timer.timeout.connect(self._heartbeat_web_presentation)
        self._presentation_heartbeat_timer.start()
        self.web_view.load(QUrl(url))

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        if self._closing:
            return super().eventFilter(watched, event)
        event_type = event.type()
        if event_type in _PRESENTATION_ACTIVITY_EVENTS:
            if watched is self.web_view or (
                isinstance(watched, QWidget) and self.web_view.isAncestorOf(watched)
            ):
                # Software rasterization presents far slower than the timer
                # interval; stacking pushed repaints there only adds CPU load.
                if not software_rendering_active():
                    self._arm_presentation_refresh(_PRESENTATION_INPUT_REFRESH_FRAMES)
        elif watched is self and event_type in (
            QEvent.Type.WindowActivate,
            QEvent.Type.Show,
            QEvent.Type.Resize,
        ):
            if not software_rendering_active():
                self._arm_presentation_refresh(_PRESENTATION_INPUT_REFRESH_FRAMES)
        return super().eventFilter(watched, event)

    def _arm_presentation_refresh(self, frames: int) -> None:
        self._presentation_refreshes_remaining = max(self._presentation_refreshes_remaining, frames)
        self.web_view.update()
        if not self._presentation_refresh_timer.isActive():
            self._presentation_refresh_timer.start()

    def _heartbeat_web_presentation(self) -> None:
        if self._closing or self.isMinimized() or not self.isVisible():
            return
        self.web_view.update()

    def _schedule_web_presentation_refresh(self, _url: QUrl) -> None:
        if self._closing:
            return
        self._arm_presentation_refresh(_PRESENTATION_REFRESH_FRAMES)

    def _refresh_web_presentation(self) -> None:
        if self._closing or self._presentation_refreshes_remaining <= 0:
            self._presentation_refresh_timer.stop()
            return
        self._presentation_refreshes_remaining -= 1
        self.web_view.update()
        if self._presentation_refreshes_remaining <= 0:
            self._presentation_refresh_timer.stop()

    def _handle_renderer_termination(self, status, exit_code: int) -> None:
        if self._closing:
            return

        recoverable_statuses = {
            QWebEnginePage.RenderProcessTerminationStatus.AbnormalTerminationStatus,
            QWebEnginePage.RenderProcessTerminationStatus.CrashedTerminationStatus,
        }
        if status in recoverable_statuses and not software_rendering_active():
            request_safe_rendering_once(
                self.paths.data_dir,
                reason=getattr(status, "name", str(status)),
                exit_code=exit_code,
            )

        if self._renderer_reload_count >= _MAX_RENDERER_RELOADS:
            return
        self._renderer_reload_count += 1
        QTimer.singleShot(250, self._reload_after_renderer_termination)

    def _reload_after_renderer_termination(self) -> None:
        if not self._closing:
            self.web_view.page().reload()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._presentation_refresh_timer.stop()
        self._presentation_heartbeat_timer.stop()
        super().closeEvent(event)

    def _handle_download(self, request: QWebEngineDownloadRequest) -> None:
        suggested = request.suggestedFileName() or "novelforge-export.txt"
        initial_path = self.paths.exports_dir / suggested
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存 NovelForge 导出文件",
            str(initial_path),
            "所有文件 (*.*)",
        )
        if not selected_path:
            request.cancel()
            return
        target = Path(selected_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        request.setDownloadDirectory(str(target.parent))
        request.setDownloadFileName(target.name)
        request.accept()
