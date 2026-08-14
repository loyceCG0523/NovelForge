"""Application paths and local runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "NovelForge"
DATA_DIR_ENV = "NOVELFORGE_WINDOWS_DATA_DIR"


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    database_path: Path
    legacy_database_path: Path
    objects_dir: Path
    samples_dir: Path
    exports_dir: Path


def resolve_app_paths(*, create: bool = True) -> AppPaths:
    """Resolve per-user paths, with an override for development and tests."""
    override = os.getenv(DATA_DIR_ENV, "").strip()
    if override:
        data_dir = Path(override).expanduser().resolve()
    else:
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA 不可用，无法确定本地数据目录。")
        data_dir = (Path(local_app_data) / APP_NAME).resolve()

    paths = AppPaths(
        data_dir=data_dir,
        database_path=data_dir / "novelforge-local.db",
        legacy_database_path=data_dir / "novelforge.db",
        objects_dir=data_dir / "objects",
        samples_dir=data_dir / "samples",
        exports_dir=data_dir / "exports",
    )
    if create:
        for directory in (
            paths.data_dir,
            paths.objects_dir,
            paths.samples_dir,
            paths.exports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
    return paths
