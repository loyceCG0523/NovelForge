from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineScript
from PySide6.QtWidgets import QApplication

from novelforge_windows.config import resolve_app_paths
from novelforge_windows.ui.web_window import (
    create_desktop_profile,
    create_runtime_hint_script,
    frontend_build_id,
    frontend_cache_namespace,
)


def _write_frontend_build(root, build_id: str) -> None:
    manifest = root / "_next" / "static" / build_id / "_buildManifest.js"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("self.__BUILD_MANIFEST={};", encoding="utf-8")
    (root / "index.html").write_text(
        f'<script src="/_next/static/{build_id}/_buildManifest.js"></script>',
        encoding="utf-8",
    )


def test_frontend_cache_namespace_is_stable_and_build_specific(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_frontend_build(first, "build-A_123")
    _write_frontend_build(second, "build-B_456")

    assert frontend_build_id(first) == "build-A_123"
    assert frontend_cache_namespace(first) == frontend_cache_namespace(first)
    assert frontend_cache_namespace(first) == "next-build-A_123"
    assert frontend_cache_namespace(first) != frontend_cache_namespace(second)


def test_frontend_build_id_uses_index_when_stale_manifest_remains(tmp_path):
    _write_frontend_build(tmp_path, "current-build")
    stale = tmp_path / "_next" / "static" / "stale-build" / "_buildManifest.js"
    stale.parent.mkdir(parents=True)
    stale.write_text("self.__BUILD_MANIFEST={};", encoding="utf-8")

    assert frontend_build_id(tmp_path) == "current-build"


def test_frontend_cache_namespace_has_stable_version_fallback(tmp_path):
    assert frontend_build_id(tmp_path).startswith("app-")
    assert frontend_cache_namespace(tmp_path) == frontend_cache_namespace(tmp_path)


def test_runtime_hint_is_injected_into_main_document_before_react(monkeypatch):
    monkeypatch.delenv("NOVELFORGE_EFFECTIVE_RENDERING", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)

    script = create_runtime_hint_script()

    assert script.name() == "NovelForgeRuntimeHint"
    assert script.injectionPoint() == QWebEngineScript.InjectionPoint.DocumentCreation
    assert script.worldId() == QWebEngineScript.ScriptWorldId.MainWorld
    assert not script.runsOnSubFrames()
    assert 'rendering: "gpu"' in script.sourceCode()
    assert 'Object.defineProperty(window, "__NOVELFORGE_RUNTIME__"' in script.sourceCode()
    assert "Object.freeze" in script.sourceCode()
    assert "configurable: false" in script.sourceCode()
    assert "writable: false" in script.sourceCode()


def test_runtime_hint_reports_software_rendering(monkeypatch):
    monkeypatch.setenv("NOVELFORGE_EFFECTIVE_RENDERING", "software")

    script = create_runtime_hint_script()

    assert 'rendering: "software"' in script.sourceCode()


def test_desktop_profile_is_named_and_uses_the_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVELFORGE_WINDOWS_DATA_DIR", str(tmp_path))
    application = QApplication.instance() or QApplication([])
    paths = resolve_app_paths()
    frontend_dir = tmp_path / "frontend"
    _write_frontend_build(frontend_dir, "profile-build")

    profile = create_desktop_profile(
        paths,
        storage_name=f"NovelForgeTest-{tmp_path.name}",
        frontend_dir=frontend_dir,
    )
    try:
        assert not profile.isOffTheRecord()
        assert profile.httpCacheType() == QWebEngineProfile.HttpCacheType.DiskHttpCache
        assert profile.persistentStoragePath() == str(tmp_path / "web-profile")
        assert profile.cachePath() == str(tmp_path / "web-cache" / "next-profile-build")
    finally:
        profile.deleteLater()
        application.processEvents()
