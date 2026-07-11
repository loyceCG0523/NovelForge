"""对象存储封装。

当前本地使用 MinIO，生产环境可以替换为任意 S3 兼容服务。业务层只关心 object key，
不直接依赖 MinIO 客户端细节。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import urlparse

from minio import Minio

from app.core.config import settings


def _build_client() -> Minio:
    """根据 .env 中的 S3/MinIO 配置创建客户端。"""
    parsed = urlparse(settings.s3_endpoint)
    endpoint = parsed.netloc or parsed.path
    secure = parsed.scheme == "https"
    return Minio(
        endpoint,
        access_key=settings.s3_access_key_id,
        secret_key=settings.s3_secret_access_key,
        secure=secure,
    )


def ensure_bucket() -> None:
    """确保默认 bucket 已存在。"""
    client = _build_client()
    if not client.bucket_exists(settings.s3_bucket):
        client.make_bucket(settings.s3_bucket)


def sanitize_filename(filename: str) -> str:
    """生成适合放进 object key 的文件名片段。"""
    name = (filename or "sample.txt").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name)
    return name[:180] or "sample.txt"


def upload_fileobj(object_name: str, file_obj, length: int, content_type: str = "text/plain") -> None:
    """上传文件对象到 MinIO。"""
    ensure_bucket()
    client = _build_client()
    client.put_object(
        settings.s3_bucket,
        object_name,
        file_obj,
        length=length,
        content_type=content_type or "application/octet-stream",
    )


def remove_object(object_name: str) -> None:
    """从 MinIO 删除对象；对象不存在时不影响业务删除流程。"""
    if not object_name:
        return
    ensure_bucket()
    client = _build_client()
    try:
        client.remove_object(settings.s3_bucket, object_name)
    except Exception:
        # 删除数据库记录时不应因对象存储临时不可用而阻断用户操作。
        return


def iter_text_object_chunks(object_name: str, chunk_chars: int = 8000) -> Iterator[str]:
    """流式读取文本对象，并按字符数切成分析块。

    这里使用 UTF-8 宽松解码，避免用户上传的文本里混入少量异常字节导致整份样本失败。
    """
    client = _build_client()
    response = client.get_object(settings.s3_bucket, object_name)
    buffer = ""
    try:
        for data in response.stream(64 * 1024):
            buffer += data.decode("utf-8", errors="ignore")
            while len(buffer) >= chunk_chars:
                cut = _find_chunk_boundary(buffer, chunk_chars)
                chunk = buffer[:cut].strip()
                buffer = buffer[cut:]
                if chunk:
                    yield chunk
        tail = buffer.strip()
        if tail:
            yield tail
    finally:
        response.close()
        response.release_conn()


def _find_chunk_boundary(buffer: str, chunk_chars: int) -> int:
    """优先在段落、句号或章节标题附近切块，减少把句子从中间切开。"""
    window = buffer[:chunk_chars]
    candidates = [
        window.rfind("\n\n"),
        window.rfind("\n"),
        window.rfind("。"),
        window.rfind("！"),
        window.rfind("？"),
    ]
    boundary = max(candidates)
    if boundary >= int(chunk_chars * 0.55):
        return boundary + 1
    return chunk_chars
