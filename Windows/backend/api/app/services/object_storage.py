"""Local filesystem object storage for the standalone Windows edition."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path


def _storage_root() -> Path:
    configured = os.getenv("NOVELFORGE_OBJECTS_DIR", "").strip()
    if not configured:
        raise RuntimeError("NOVELFORGE_OBJECTS_DIR 未配置。")
    root = Path(configured).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _object_path(object_name: str) -> Path:
    normalized = str(object_name or "").replace("\\", "/").lstrip("/")
    if not normalized:
        raise ValueError("对象名称不能为空。")
    root = _storage_root()
    target = (root / normalized).resolve()
    if target != root and root not in target.parents:
        raise ValueError("对象路径越出了本地数据目录。")
    return target


def ensure_bucket() -> None:
    _storage_root()


def sanitize_filename(filename: str) -> str:
    name = (filename or "sample.txt").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name)
    return name[:180] or "sample.txt"


def upload_fileobj(object_name: str, file_obj, length: int, content_type: str = "text/plain") -> None:
    del content_type
    target = _object_path(object_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    remaining = max(0, int(length))
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("wb") as output:
        while remaining:
            chunk = file_obj.read(min(64 * 1024, remaining))
            if not chunk:
                break
            output.write(chunk)
            remaining -= len(chunk)
    temporary.replace(target)


def upload_text(object_name: str, content: str) -> None:
    target = _object_path(object_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)


def read_text_object(object_name: str, max_bytes: int = 2 * 1024 * 1024) -> str:
    target = _object_path(object_name)
    if not target.is_file():
        return ""
    data = target.read_bytes()
    if len(data) > max_bytes:
        raise ValueError("文本分段超过允许的读取大小")
    return data.decode("utf-8", errors="ignore")


def remove_object(object_name: str) -> None:
    if not object_name:
        return
    target = _object_path(object_name)
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return


def iter_text_object_chunks(object_name: str, chunk_chars: int = 8000) -> Iterator[str]:
    target = _object_path(object_name)
    buffer = ""
    with target.open("r", encoding="utf-8", errors="ignore") as source:
        while True:
            data = source.read(64 * 1024)
            if not data:
                break
            buffer += data
            while len(buffer) >= chunk_chars:
                cut = _find_chunk_boundary(buffer, chunk_chars)
                chunk = buffer[:cut].strip()
                buffer = buffer[cut:]
                if chunk:
                    yield chunk
    tail = buffer.strip()
    if tail:
        yield tail


def _find_chunk_boundary(buffer: str, chunk_chars: int) -> int:
    window = buffer[:chunk_chars]
    candidates = [window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"), window.rfind("！"), window.rfind("？")]
    boundary = max(candidates)
    return boundary + 1 if boundary >= int(chunk_chars * 0.55) else chunk_chars
