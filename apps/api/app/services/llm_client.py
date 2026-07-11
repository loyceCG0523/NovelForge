"""OpenAI-compatible LLM 调用封装。"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from json_repair import repair_json


DEFAULT_CHAPTER_TEMPERATURE = 0.7
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
RESPONSE_FORMAT_UNSUPPORTED_MARKERS = (
    "does not support response_format",
    "response_format is not supported",
    "unsupported parameter",
    "invalid parameter",
)


@dataclass
class LLMConfig:
    """单次模型调用需要的配置。"""

    base_url: str
    api_key: str
    model: str
    temperature: float = DEFAULT_CHAPTER_TEMPERATURE
    timeout_seconds: float = 90.0
    max_retries: int = 2


class LLMClient:
    """调用 OpenAI-compatible chat completions 接口。"""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def generate_chapter(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """调用模型并解析章节 JSON 输出。"""
        content, parsed = self.complete_json(messages)
        return {
            "title": str(parsed.get("title") or "未命名章节"),
            "summary": str(parsed.get("summary") or ""),
            "content": str(parsed.get("content") or content),
            "raw_response": content,
        }

    def complete_json(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        """调用模型并解析通用 JSON 输出，供章节生成、记忆抽取等任务复用。"""
        content = self._chat(messages)
        return content, parse_json_response(content)

    def _chat(self, messages: list[dict[str, str]]) -> str:
        """执行 chat completions 请求。"""
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            data = self._post_with_retries(
                url=url,
                headers=headers,
                payload={**payload, "response_format": {"type": "json_object"}},
            )
        except UnsupportedResponseFormatError:
            # DeepSeek 等 OpenAI-compatible 接口不一定支持 response_format。
            # 降级后仍由 parse_json_response + json_repair 兜底解析 JSON。
            data = self._post_with_retries(url=url, headers=headers, payload=payload)
        return data["choices"][0]["message"]["content"]

    def _post_with_retries(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict:
        """带重试的 HTTP 请求，处理临时 SSL/代理/网关抖动。"""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                with httpx.Client(timeout=self.config.timeout_seconds) as client:
                    response = client.post(url, headers=headers, json=payload)
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.config.max_retries:
                        time.sleep(1.2 * (attempt + 1))
                        continue
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if _is_unsupported_response_format_error(exc):
                    raise UnsupportedResponseFormatError(
                        f"LLM API 不支持 response_format 参数：HTTP {exc.response.status_code}"
                    ) from exc
                if exc.response.status_code not in RETRYABLE_STATUS_CODES or attempt >= self.config.max_retries:
                    raise RuntimeError(
                        f"LLM API 返回错误：HTTP {exc.response.status_code}，{exc.response.text[:500]}"
                    ) from exc
                time.sleep(1.2 * (attempt + 1))
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise RuntimeError(
                        "LLM API 网络连接失败：HTTPS 连接被提前断开或超时。"
                        "请检查代理/VPN、base_url 是否正确，或稍后重试。"
                    ) from exc
                time.sleep(1.2 * (attempt + 1))

        raise RuntimeError(f"LLM API 请求失败：{last_error}") from last_error


class UnsupportedResponseFormatError(RuntimeError):
    """供应商兼容接口不支持 response_format 时触发，用于自动降级重试。"""


def _is_unsupported_response_format_error(exc: httpx.HTTPStatusError) -> bool:
    """判断 400 错误是否由 response_format 兼容性导致。"""
    if exc.response.status_code != 400:
        return False
    text = exc.response.text.lower()
    return "response_format" in text and any(marker in text for marker in RESPONSE_FORMAT_UNSUPPORTED_MARKERS)


def parse_json_response(content: str) -> dict[str, Any]:
    """解析模型返回的 JSON；如果模型包了代码块，则尽量提取内部 JSON。"""
    candidate = _extract_json_candidate(content)
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        repaired = repair_json(candidate)
        return json.loads(repaired, strict=False)


def _extract_json_candidate(content: str) -> str:
    """从模型回复中提取最可能的 JSON 对象。"""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.S | re.I)
    if fenced:
        return fenced.group(1).strip()

    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return content[start : end + 1]
    return content.strip()


def parse_chapter_response(content: str) -> dict[str, Any]:
    """兼容章节生成调用的 JSON 解析函数。"""
    return parse_json_response(content)


def build_llm_config(preferences: dict) -> LLMConfig | None:
    """从用户偏好中提取 LLM 配置；没有 API Key 时返回 None。"""
    llm = (preferences or {}).get("llm") or {}
    api_key = str(llm.get("api_key") or "").strip()
    if not api_key:
        return None

    base_url = str(llm.get("base_url") or "https://api.openai.com/v1").strip()
    model = str(llm.get("model") or "gpt-4.1-mini").strip()

    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=DEFAULT_CHAPTER_TEMPERATURE,
    )
