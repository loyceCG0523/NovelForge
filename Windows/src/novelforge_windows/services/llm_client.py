"""Direct OpenAI-compatible model client; no NovelForge server is involved."""

from __future__ import annotations

from typing import Any

import httpx

from novelforge_windows.domain.models import ModelSettings


class ModelConnectionError(RuntimeError):
    pass


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("模型 Base URL 不能为空。")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


class OpenAICompatibleClient:
    def complete(
        self,
        settings: ModelSettings,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
    ) -> str:
        if not settings.model.strip():
            raise ValueError("请先在设置中填写模型名称。")
        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        payload: dict[str, Any] = {
            "model": settings.model.strip(),
            "messages": messages,
            "temperature": settings.temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        try:
            with httpx.Client(timeout=httpx.Timeout(180, connect=20)) as client:
                response = client.post(
                    chat_completions_url(settings.base_url),
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500].strip()
            raise ModelConnectionError(
                f"模型接口返回 {exc.response.status_code}：{detail or '无错误详情'}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelConnectionError(f"无法连接模型接口：{exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelConnectionError("模型响应缺少 choices[0].message.content。") from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelConnectionError("模型返回了空内容。")
        return content.strip()

    def test(self, settings: ModelSettings) -> str:
        return self.complete(
            settings,
            [
                {"role": "system", "content": "你是连接测试助手。"},
                {"role": "user", "content": "只回复：连接成功"},
            ],
            max_tokens=16,
        )

