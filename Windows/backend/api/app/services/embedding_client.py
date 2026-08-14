"""阿里云百炼 qwen3.7-text-embedding 原生 HTTP 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.desktop_secrets import reveal_preference_secret

from app.core.config import settings


DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding"
EMBEDDING_SERVICE_PATH = (
    "/services/embeddings/text-embedding/text-embedding"
)


@dataclass(slots=True)
class EmbeddingConfig:
    endpoint: str
    api_key: str
    model: str
    dimensions: int
    timeout_seconds: float = 135.0


def normalize_embedding_endpoint(value: str) -> str:
    """兼容百炼原生 /api/v1 与 OpenAI-compatible /compatible-mode/v1。"""
    endpoint = str(value or "").strip().rstrip("/")
    if endpoint.endswith("/api/v1"):
        endpoint += EMBEDDING_SERVICE_PATH
    elif endpoint.endswith("/compatible-mode/v1"):
        endpoint += "/embeddings"
    return endpoint


def is_openai_compatible_embedding_endpoint(endpoint: str) -> bool:
    return "/compatible-mode/" in endpoint and endpoint.rstrip("/").endswith(
        "/embeddings"
    )


def _validate_embedding_values(endpoint: str, model: str, api_key: str) -> None:
    if not endpoint:
        raise ValueError("请填写 Embedding API Endpoint")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Embedding Endpoint 必须是有效的 HTTP 或 HTTPS 地址")
    if not model:
        raise ValueError("请填写 Embedding 模型名称")
    if not api_key:
        raise ValueError("请填写 Embedding API Key，或先保存已有密钥")


def build_embedding_config(preferences: dict | None) -> EmbeddingConfig | None:
    """从当前用户偏好读取向量模型；关闭或配置不完整时不发起调用。"""
    embedding = (preferences or {}).get("embedding") or {}
    if not embedding.get("enabled"):
        return None
    endpoint = normalize_embedding_endpoint(str(embedding.get("endpoint") or ""))
    api_key = reveal_preference_secret(embedding.get("api_key") or "")
    model = str(embedding.get("model") or DEFAULT_EMBEDDING_MODEL).strip()
    if not endpoint or not api_key or not model:
        return None
    return EmbeddingConfig(
        endpoint=endpoint,
        api_key=api_key,
        model=model,
        dimensions=max(1, int(settings.embedding_dimensions)),
    )


def build_test_embedding_config(
    preferences: dict | None,
    *,
    endpoint: str = "",
    model: str = "",
    api_key: str = "",
) -> EmbeddingConfig:
    """组合一次性连接测试配置，空密钥安全复用已保存值。"""
    saved = (preferences or {}).get("embedding") or {}
    resolved_endpoint = normalize_embedding_endpoint(
        str(endpoint or saved.get("endpoint") or "")
    )
    resolved_model = str(
        model or saved.get("model") or DEFAULT_EMBEDDING_MODEL
    ).strip()
    resolved_api_key = str(api_key or reveal_preference_secret(saved.get("api_key") or "")).strip()
    _validate_embedding_values(
        resolved_endpoint,
        resolved_model,
        resolved_api_key,
    )
    return EmbeddingConfig(
        endpoint=resolved_endpoint,
        api_key=resolved_api_key,
        model=resolved_model,
        dimensions=max(1, int(settings.embedding_dimensions)),
        timeout_seconds=30.0,
    )


class EmbeddingClient:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config

    def embed(
        self,
        texts: list[str],
        *,
        text_type: str = "document",
        instruct: str = "",
    ) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > 20:
            raise ValueError("qwen3.7-text-embedding 单次最多输入 20 条文本")
        if text_type not in {"document", "query"}:
            raise ValueError("text_type 只能是 document 或 query")

        compatible_mode = is_openai_compatible_embedding_endpoint(
            self.config.endpoint
        )
        if compatible_mode:
            payload: dict[str, Any] = {
                "model": self.config.model,
                "input": texts,
                "dimensions": self.config.dimensions,
                "encoding_format": "float",
            }
        else:
            payload = {
                "model": self.config.model,
                "input": {"texts": texts},
                "parameters": {
                    "dimension": self.config.dimensions,
                    "output_type": "dense",
                    "text_type": text_type,
                },
            }
            if instruct.strip():
                payload["parameters"]["instruct"] = instruct.strip()
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        response = httpx.post(
            self.config.endpoint,
            headers=headers,
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        if compatible_mode:
            error = body.get("error") or {}
            if error:
                raise RuntimeError(
                    f"Embedding 调用失败：{error.get('code') or 'unknown_error'} "
                    f"{error.get('message') or ''}".strip()
                )
            data = body.get("data") or []
            ordered = sorted(data, key=lambda item: int(item.get("index") or 0))
        else:
            if body.get("code"):
                raise RuntimeError(
                    f"DashScope Embedding 调用失败：{body.get('code')} "
                    f"{body.get('message') or ''}".strip()
                )
            data = (body.get("output") or {}).get("embeddings") or []
            ordered = sorted(
                data,
                key=lambda item: int(item.get("text_index") or 0),
            )
        vectors = [list(item.get("embedding") or []) for item in ordered]
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding 服务返回数量与输入不一致")
        for vector in vectors:
            if len(vector) != self.config.dimensions:
                raise RuntimeError(
                    f"Embedding 维度不一致：期望 {self.config.dimensions}，实际 {len(vector)}"
                )
        return vectors
