"""OpenAI-compatible LLM 调用封装。"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from json_repair import repair_json

from app.services.paragraph_formatter import format_chapter_paragraphs


DEFAULT_CHAPTER_TEMPERATURE = 0.7
DEFAULT_REVIEW_TEMPERATURE = 0.1
TIMEOUT_MULTIPLIER = 1.5
DEFAULT_LLM_TIMEOUT_SECONDS = 90.0 * TIMEOUT_MULTIPLIER
DEFAULT_LLM_TOTAL_TIMEOUT_SECONDS = 600.0 * TIMEOUT_MULTIPLIER
DEEP_THINKING_ACTIVITY_TIMEOUT_SECONDS = 300.0
DEEP_THINKING_TOTAL_TIMEOUT_SECONDS = 2700.0
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
RESPONSE_FORMAT_UNSUPPORTED_MARKERS = (
    "does not support response_format",
    "response_format is not supported",
    "unsupported parameter",
    "invalid parameter",
)
MAX_TOKENS_UNSUPPORTED_MARKERS = (
    "does not support max_tokens",
    "max_tokens is not supported",
    "unsupported parameter",
    "invalid parameter",
)
FIXED_TEMPERATURE_MARKERS = (
    "only 1 is allowed",
    "must be 1",
    "temperature must equal 1",
)


@dataclass
class LLMConfig:
    """单次模型调用需要的配置。"""

    base_url: str
    api_key: str
    model: str
    temperature: float | None = DEFAULT_CHAPTER_TEMPERATURE
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    total_timeout_seconds: float | None = DEFAULT_LLM_TOTAL_TIMEOUT_SECONDS
    max_retries: int = 1


class LLMClient:
    """调用 OpenAI-compatible chat completions 接口。"""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        # 只记录协议级摘要，不保存提示词或模型正文，便于定位兼容接口问题。
        self.last_request_telemetry: dict[str, Any] = {}

    def generate_chapter(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_stream_reset: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """调用模型并解析章节 JSON 输出。"""
        content, parsed = self.complete_json(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            on_content_delta=on_content_delta,
            on_stream_reset=on_stream_reset,
        )
        normalized_content = format_chapter_paragraphs(
            str(parsed.get("content") or content)
        )
        chapter_progress = parsed.get("chapter_progress") if isinstance(parsed.get("chapter_progress"), dict) else {}
        return {
            "title": str(parsed.get("title") or "未命名章节"),
            "summary": str(parsed.get("summary") or ""),
            "content": normalized_content,
            "chapter_progress": chapter_progress,
            "raw_response": content,
        }

    def complete_json(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_stream_reset: Callable[[], None] | None = None,
        stream: bool = True,
        on_raw_delta: Callable[[str], None] | None = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """流式接收并解析通用 JSON；显式 stream=False 仅供特殊兼容场景使用。"""
        content = self._chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            on_content_delta=on_content_delta,
            on_stream_reset=on_stream_reset,
            stream=stream,
            on_raw_delta=on_raw_delta,
            on_activity=on_activity,
        )
        return content, parse_json_response(content)

    def complete_text(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """执行一次普通文本补全，供连接测试等无需 JSON 解析的轻量场景使用。"""
        return self._chat(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=False,
        )

    def _chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_stream_reset: Callable[[], None] | None = None,
        stream: bool = False,
        on_raw_delta: Callable[[str], None] | None = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
        json_mode: bool = True,
    ) -> str:
        """执行 chat completions 请求。"""
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": messages,
        }
        resolved_temperature = self.config.temperature if temperature is None else temperature
        if resolved_temperature is not None:
            payload["temperature"] = resolved_temperature
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            return self._chat_once(
                url=url,
                headers=headers,
                payload=payload,
                on_content_delta=on_content_delta,
                on_stream_reset=on_stream_reset,
                stream=stream,
                on_raw_delta=on_raw_delta,
                on_activity=on_activity,
                json_mode=json_mode,
            )
        except FixedTemperatureRequiredError:
            # 部分 OpenAI-compatible 模型只接受 temperature=1。
            # 这是能力协商，不消耗普通网络重试次数。
            return self._chat_once(
                url=url,
                headers=headers,
                payload={**payload, "temperature": 1},
                on_content_delta=on_content_delta,
                on_stream_reset=on_stream_reset,
                stream=stream,
                on_raw_delta=on_raw_delta,
                on_activity=on_activity,
                json_mode=json_mode,
            )

    def _chat_once(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        on_content_delta: Callable[[str], None] | None,
        on_stream_reset: Callable[[], None] | None,
        stream: bool,
        on_raw_delta: Callable[[str], None] | None,
        on_activity: Callable[[dict[str, Any]], None] | None,
        json_mode: bool,
    ) -> str:
        """使用一组已确定的兼容参数执行请求。"""
        if stream or on_content_delta is not None:
            return self._chat_streaming(
                url=url,
                headers=headers,
                payload=payload,
                on_content_delta=on_content_delta,
                on_stream_reset=on_stream_reset,
                on_raw_delta=on_raw_delta,
                on_activity=on_activity,
                json_mode=json_mode,
            )
        if not json_mode:
            try:
                data = self._post_with_retries(
                    url=url,
                    headers=headers,
                    payload=payload,
                )
            except UnsupportedMaxTokensError:
                payload.pop("max_tokens", None)
                data = self._post_with_retries(
                    url=url,
                    headers=headers,
                    payload=payload,
                )
            return _extract_text_response(data, allow_reasoning=True)
        try:
            data = self._post_with_retries(
                url=url,
                headers=headers,
                payload={**payload, "response_format": {"type": "json_object"}},
            )
        except UnsupportedResponseFormatError:
            try:
                data = self._post_with_retries(url=url, headers=headers, payload=payload)
            except UnsupportedMaxTokensError:
                payload.pop("max_tokens", None)
                data = self._post_with_retries(url=url, headers=headers, payload=payload)
        except UnsupportedMaxTokensError:
            payload.pop("max_tokens", None)
            try:
                data = self._post_with_retries(
                    url=url,
                    headers=headers,
                    payload={**payload, "response_format": {"type": "json_object"}},
                )
            except UnsupportedResponseFormatError:
                data = self._post_with_retries(url=url, headers=headers, payload=payload)
        content = _extract_text_response(data)
        if content:
            return content
        reasoning = _extract_text_response(data, allow_reasoning=True)
        finish_reasons = _extract_finish_reasons(data)
        self.last_request_telemetry = {
            "mode": "non_stream",
            "status": "empty",
            "output_chars": 0,
            "reasoning_only": bool(reasoning),
            "finish_reasons": finish_reasons,
        }
        if reasoning and "length" in finish_reasons:
            raise ReasoningBudgetExhaustedError(
                "模型思考过程耗尽输出 Token，未生成最终 content。"
            )
        raise EmptyStreamResponseError(
            "LLM API 非流式响应仅包含 reasoning_content，未返回最终 content。"
            if reasoning
            else "LLM API 非流式响应未返回最终 content。"
        )

    def _chat_streaming(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        on_content_delta: Callable[[str], None] | None,
        on_stream_reset: Callable[[], None] | None,
        on_raw_delta: Callable[[str], None] | None,
        on_activity: Callable[[dict[str, Any]], None] | None,
        json_mode: bool,
    ) -> str:
        """流式接收完整 JSON，同时只把 content 字符串增量交给前端预览。"""
        variants = (
            [
                {**payload, "response_format": {"type": "json_object"}},
                dict(payload),
            ]
            if json_mode
            else [dict(payload)]
        )
        last_error: Exception | None = None
        for variant_index, variant in enumerate(variants):
            try:
                return self._post_stream_with_retries(
                    url=url,
                    headers=headers,
                    payload=variant,
                    on_content_delta=on_content_delta,
                    on_stream_reset=on_stream_reset,
                    on_raw_delta=on_raw_delta,
                    on_activity=on_activity,
                )
            except EmptyStreamResponseError as exc:
                if isinstance(exc, ReasoningBudgetExhaustedError):
                    raise
                # 部分 OpenAI-compatible 网关会以 200 结束 SSE，却没有输出
                # delta.content。流式重试耗尽后，去掉 response_format 做一次
                # 普通请求，避免一次网关抖动直接中断整条生成链路。
                last_error = exc
                fallback_payload = dict(payload)
                try:
                    data = self._post_with_retries(
                        url=url,
                        headers=headers,
                        payload=fallback_payload,
                    )
                except UnsupportedMaxTokensError:
                    fallback_payload.pop("max_tokens", None)
                    data = self._post_with_retries(
                        url=url,
                        headers=headers,
                        payload=fallback_payload,
                    )
                content = _extract_text_response(data)
                stream_telemetry = dict(self.last_request_telemetry)
                if not content:
                    reasoning = _extract_text_response(data, allow_reasoning=True)
                    reason = (
                        "上游非流式降级响应仅包含 reasoning_content，未返回最终 content。"
                        if reasoning
                        else "上游非流式降级响应未返回最终 content。"
                    )
                    self.last_request_telemetry = {
                        "mode": "non_stream_fallback",
                        "status": "empty",
                        "fallback_reason": str(exc),
                        "reasoning_only": bool(reasoning),
                        "stream": stream_telemetry,
                    }
                    raise EmptyStreamResponseError(reason) from exc
                self.last_request_telemetry = {
                    "mode": "non_stream_fallback",
                    "status": "completed",
                    "fallback_reason": str(exc),
                    "output_chars": len(content),
                    "stream": stream_telemetry,
                }
                if on_raw_delta is not None:
                    on_raw_delta(content)
                if on_content_delta is not None:
                    preview = JSONContentStreamExtractor().feed(content)
                    if preview:
                        on_content_delta(preview)
                return content
            except UnsupportedResponseFormatError as exc:
                last_error = exc
                if variant_index == 0:
                    continue
                raise
            except UnsupportedMaxTokensError as exc:
                last_error = exc
                if "max_tokens" in payload:
                    payload = {key: value for key, value in payload.items() if key != "max_tokens"}
                    variants = [
                        {**payload, "response_format": {"type": "json_object"}},
                        dict(payload),
                    ]
                    return self._chat_streaming(
                        url=url,
                        headers=headers,
                        payload=payload,
                        on_content_delta=on_content_delta,
                        on_stream_reset=on_stream_reset,
                        on_raw_delta=on_raw_delta,
                        on_activity=on_activity,
                        json_mode=json_mode,
                    )
                raise
        raise RuntimeError(f"LLM 流式请求失败：{last_error}") from last_error

    def _post_stream_with_retries(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        on_content_delta: Callable[[str], None] | None,
        on_stream_reset: Callable[[], None] | None,
        on_raw_delta: Callable[[str], None] | None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        last_error: Exception | None = None
        attempt_summaries: list[dict[str, Any]] = []
        for attempt in range(self.config.max_retries + 1):
            if on_stream_reset is not None:
                on_stream_reset()
            extractor = JSONContentStreamExtractor()
            raw_parts: list[str] = []
            plain_lines: list[str] = []
            reasoning_chars = 0
            output_chars = 0
            event_count = 0
            finish_reasons: list[str] = []
            attempt_started_at = time.monotonic()
            first_activity_at: float | None = None
            last_activity_at: float | None = None
            try:
                stream_payload = {**payload, "stream": True}
                activity_timeout = max(1.0, float(self.config.timeout_seconds))
                timeout = httpx.Timeout(
                    connect=min(activity_timeout, 60.0),
                    read=activity_timeout,
                    write=activity_timeout,
                    pool=min(activity_timeout, 60.0),
                )
                with httpx.Client(timeout=timeout) as client:
                    with client.stream("POST", url, headers=headers, json=stream_payload) as response:
                        if response.status_code >= 400:
                            response.read()
                            response.raise_for_status()
                        for line in response.iter_lines():
                            activity_at = time.monotonic()
                            if first_activity_at is None:
                                first_activity_at = activity_at
                            last_activity_at = activity_at
                            if (
                                self.config.total_timeout_seconds
                                and time.monotonic() - attempt_started_at
                                > self.config.total_timeout_seconds
                            ):
                                raise httpx.TimeoutException("LLM stream exceeded total timeout")
                            if not line:
                                continue
                            if not line.startswith("data:"):
                                # 合法 SSE 还可能包含 event/id/retry/注释行，它们
                                # 不是模型正文；仅保留可能属于普通 JSON 响应的行。
                                if not line.startswith(("event:", "id:", "retry:", ":")):
                                    plain_lines.append(line)
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            chunk = json.loads(data)
                            event_count += 1
                            choice = (chunk.get("choices") or [{}])[0]
                            finish_reason = choice.get("finish_reason")
                            if finish_reason:
                                finish_reasons.append(str(finish_reason))
                            delta_payload = choice.get("delta") or {}
                            message_payload = choice.get("message") or {}
                            reasoning = (
                                delta_payload.get("reasoning_content")
                                or delta_payload.get("reasoning")
                                or message_payload.get("reasoning_content")
                                or message_payload.get("reasoning")
                                or ""
                            )
                            if isinstance(reasoning, str):
                                reasoning_chars += len(reasoning)
                            # 标准 SSE 使用 delta；少量兼容网关会在 data 事件里
                            # 直接返回 message，二者都只提取最终 content。
                            delta = _extract_content_value(delta_payload.get("content"))
                            if not delta:
                                delta = _extract_content_value(
                                    message_payload.get("content")
                                )
                            if not delta:
                                delta = _extract_content_value(choice.get("text"))
                            if not delta:
                                if on_activity is not None:
                                    try:
                                        on_activity(
                                            {
                                                "kind": "reasoning" if reasoning else "heartbeat",
                                                "attempt": attempt + 1,
                                                "event_count": event_count,
                                                "reasoning_chars": reasoning_chars,
                                                "output_chars": output_chars,
                                                "elapsed_seconds": round(
                                                    activity_at - attempt_started_at,
                                                    3,
                                                ),
                                            }
                                        )
                                    except Exception:
                                        # 监控回调不能反向中断模型流。
                                        pass
                                continue
                            raw_parts.append(delta)
                            output_chars += len(delta)
                            if on_activity is not None:
                                try:
                                    on_activity(
                                        {
                                            "kind": "content",
                                            "attempt": attempt + 1,
                                            "event_count": event_count,
                                            "reasoning_chars": reasoning_chars,
                                            "output_chars": output_chars,
                                            "elapsed_seconds": round(
                                                activity_at - attempt_started_at,
                                                3,
                                            ),
                                        }
                                    )
                                except Exception:
                                    pass
                            if on_raw_delta is not None:
                                on_raw_delta(delta)
                            content_delta = extractor.feed(delta)
                            if content_delta and on_content_delta is not None:
                                on_content_delta(content_delta)
                if raw_parts:
                    attempt_summary = {
                        "mode": "stream",
                        "status": "completed",
                        "attempt": attempt + 1,
                        "event_count": event_count,
                        "output_chars": sum(len(part) for part in raw_parts),
                        "reasoning_chars": reasoning_chars,
                        "first_activity_seconds": (
                            round(first_activity_at - attempt_started_at, 3)
                            if first_activity_at is not None
                            else None
                        ),
                        "last_activity_seconds": (
                            round(last_activity_at - attempt_started_at, 3)
                            if last_activity_at is not None
                            else None
                        ),
                        "activity_timeout_seconds": activity_timeout,
                        "finish_reasons": finish_reasons,
                    }
                    attempt_summaries.append(attempt_summary)
                    self.last_request_telemetry = {
                        **attempt_summary,
                        "attempts": attempt_summaries,
                    }
                    return "".join(raw_parts)
                if plain_lines:
                    data = json.loads("\n".join(plain_lines))
                    content = _extract_text_response(data)
                    if not content:
                        reasoning = _extract_text_response(data, allow_reasoning=True)
                        plain_finish_reasons = _extract_finish_reasons(data)
                        attempt_summary = {
                            "mode": "stream_plain_json",
                            "status": "empty",
                            "attempt": attempt + 1,
                            "event_count": event_count,
                            "output_chars": 0,
                            "reasoning_chars": len(reasoning),
                            "finish_reasons": plain_finish_reasons,
                        }
                        attempt_summaries.append(attempt_summary)
                        self.last_request_telemetry = {
                            **attempt_summary,
                            "attempts": attempt_summaries,
                        }
                        if reasoning and "length" in plain_finish_reasons:
                            raise ReasoningBudgetExhaustedError(
                                "模型思考过程耗尽输出 Token，未生成最终 content。"
                            )
                        raise EmptyStreamResponseError(
                            "LLM API 流式响应仅包含 reasoning_content，未返回最终 content。"
                            if reasoning
                            else "LLM API 流式响应未返回最终 content。"
                        )
                    attempt_summary = {
                        "mode": "stream_plain_json",
                        "status": "completed",
                        "attempt": attempt + 1,
                        "event_count": event_count,
                        "output_chars": len(content),
                        "reasoning_chars": reasoning_chars,
                        "finish_reasons": finish_reasons,
                    }
                    attempt_summaries.append(attempt_summary)
                    self.last_request_telemetry = {
                        **attempt_summary,
                        "attempts": attempt_summaries,
                    }
                    if on_raw_delta is not None:
                        on_raw_delta(content)
                    preview = extractor.feed(content)
                    if preview and on_content_delta is not None:
                        on_content_delta(preview)
                    return content
                attempt_summary = {
                    "mode": "stream",
                    "status": "empty",
                    "attempt": attempt + 1,
                    "event_count": event_count,
                    "output_chars": 0,
                    "reasoning_chars": reasoning_chars,
                    "first_activity_seconds": (
                        round(first_activity_at - attempt_started_at, 3)
                        if first_activity_at is not None
                        else None
                    ),
                    "last_activity_seconds": (
                        round(last_activity_at - attempt_started_at, 3)
                        if last_activity_at is not None
                        else None
                    ),
                    "activity_timeout_seconds": activity_timeout,
                    "finish_reasons": finish_reasons,
                }
                attempt_summaries.append(attempt_summary)
                self.last_request_telemetry = {
                    **attempt_summary,
                    "attempts": attempt_summaries,
                }
                if reasoning_chars and "length" in finish_reasons:
                    raise ReasoningBudgetExhaustedError(
                        "模型思考过程耗尽输出 Token，未生成最终 content。"
                    )
                raise EmptyStreamResponseError(
                    "LLM API 流式响应仅包含 reasoning_content，未返回最终 content。"
                    if reasoning_chars
                    else "LLM API 流式响应为空，未返回最终 content。"
                )
            except EmptyStreamResponseError as exc:
                last_error = exc
                if isinstance(exc, ReasoningBudgetExhaustedError):
                    raise
                if attempt >= self.config.max_retries:
                    raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if _is_unsupported_response_format_error(exc):
                    raise UnsupportedResponseFormatError("LLM API 不支持 response_format 参数") from exc
                if _is_unsupported_max_tokens_error(exc):
                    raise UnsupportedMaxTokensError("LLM API 不支持 max_tokens 参数") from exc
                if _is_fixed_temperature_error(exc):
                    raise FixedTemperatureRequiredError("LLM API 仅支持 temperature=1") from exc
                if exc.response.status_code not in RETRYABLE_STATUS_CODES or attempt >= self.config.max_retries:
                    raise RuntimeError(f"LLM API 返回错误：HTTP {exc.response.status_code}：{exc.response.text[:500]}") from exc
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise RuntimeError(
                        f"LLM API 流式连接失败或超时（{type(exc).__name__}）。"
                    ) from exc
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise RuntimeError("LLM API 流式响应格式无效。") from exc
            time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"LLM API 流式请求失败：{last_error}") from last_error

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
                if _is_unsupported_max_tokens_error(exc):
                    raise UnsupportedMaxTokensError(
                        f"LLM API 不支持 max_tokens 参数：HTTP {exc.response.status_code}"
                    ) from exc
                if _is_fixed_temperature_error(exc):
                    raise FixedTemperatureRequiredError(
                        f"LLM API 仅支持 temperature=1：HTTP {exc.response.status_code}"
                    ) from exc
                if exc.response.status_code not in RETRYABLE_STATUS_CODES or attempt >= self.config.max_retries:
                    raise RuntimeError(
                        f"LLM API 返回错误：HTTP {exc.response.status_code}：{exc.response.text[:500]}"
                    ) from exc
                time.sleep(1.2 * (attempt + 1))
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise RuntimeError(
                        f"LLM API 网络连接失败（{type(exc).__name__}）："
                        "HTTPS 连接被提前断开或超时。"
                        "请检查代理/VPN、base_url 是否正确，或稍后重试。"
                    ) from exc
                time.sleep(1.2 * (attempt + 1))

        raise RuntimeError(f"LLM API 请求失败：{last_error}") from last_error


class UnsupportedResponseFormatError(RuntimeError):
    """供应商兼容接口不支持 response_format 时触发，用于自动降级重试。"""


class UnsupportedMaxTokensError(RuntimeError):
    """供应商兼容接口不支持 max_tokens 时触发，用于自动降级重试。"""


class FixedTemperatureRequiredError(RuntimeError):
    """供应商兼容模型只允许 temperature=1 时触发，用于自动兼容重试。"""


class EmptyStreamResponseError(RuntimeError):
    """HTTP/SSE 正常结束，但供应商没有返回可用的最终 content。"""


class ReasoningBudgetExhaustedError(EmptyStreamResponseError):
    """输出额度全部被 reasoning_content 消耗，未生成最终 content。"""


def _extract_finish_reasons(data: dict[str, Any]) -> list[str]:
    """提取普通响应中的结束原因，供空 content 的精确诊断使用。"""
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list):
        return []
    return [
        str(choice.get("finish_reason"))
        for choice in choices
        if isinstance(choice, dict) and choice.get("finish_reason")
    ]


def _extract_content_value(value: Any) -> str:
    """兼容字符串及 OpenAI 内容分片数组，只提取最终文本。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict)
            and item.get("type") in {None, "text", "output_text"}
        )
    return ""


def _extract_text_response(data: dict[str, Any], *, allow_reasoning: bool = False) -> str:
    """提取普通文本响应；连接测试可接受推理模型返回的 reasoning_content。"""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM API 响应格式无效，未找到 choices.message") from exc

    content = _extract_content_value(message.get("content"))
    if content.strip():
        return content
    if allow_reasoning:
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
    return ""


class JSONContentStreamExtractor:
    """从逐块到达的 JSON 文本中解码顶层 content 字符串。"""

    def __init__(self) -> None:
        self.prefix = ""
        self.started = False
        self.ended = False
        self.escaped = False
        self.unicode_digits: str | None = None

    def feed(self, chunk: str) -> str:
        if self.ended or not chunk:
            return ""
        data = chunk
        if not self.started:
            self.prefix += chunk
            match = re.search(r'"content"\s*:\s*"', self.prefix)
            if match is None:
                return ""
            data = self.prefix[match.end():]
            self.prefix = ""
            self.started = True

        output: list[str] = []
        for char in data:
            if self.unicode_digits is not None:
                self.unicode_digits += char
                if len(self.unicode_digits) == 4:
                    try:
                        output.append(chr(int(self.unicode_digits, 16)))
                    except ValueError:
                        output.append("\\u" + self.unicode_digits)
                    self.unicode_digits = None
                    self.escaped = False
                continue
            if self.escaped:
                if char == "u":
                    self.unicode_digits = ""
                    continue
                output.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(char, char))
                self.escaped = False
                continue
            if char == "\\":
                self.escaped = True
                continue
            if char == '"':
                self.ended = True
                break
            output.append(char)
        return "".join(output)


def _is_unsupported_response_format_error(exc: httpx.HTTPStatusError) -> bool:
    """判断 400 错误是否由 response_format 兼容性导致。"""
    if exc.response.status_code != 400:
        return False
    text = exc.response.text.lower()
    return "response_format" in text and any(marker in text for marker in RESPONSE_FORMAT_UNSUPPORTED_MARKERS)


def _is_unsupported_max_tokens_error(exc: httpx.HTTPStatusError) -> bool:
    """判断 400 错误是否由 max_tokens 兼容性导致。"""
    if exc.response.status_code != 400:
        return False
    text = exc.response.text.lower()
    return "max_tokens" in text and any(marker in text for marker in MAX_TOKENS_UNSUPPORTED_MARKERS)


def _is_fixed_temperature_error(exc: httpx.HTTPStatusError) -> bool:
    """判断 400 错误是否要求固定使用 temperature=1。"""
    if exc.response.status_code != 400:
        return False
    text = exc.response.text.lower()
    return "temperature" in text and any(marker in text for marker in FIXED_TEMPERATURE_MARKERS)

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
def _model_temperature(base_url: str, model: str, default: float) -> float | None:
    """对默认思考模式模型省略无效的 temperature 参数。"""
    hostname = (urlparse(base_url).hostname or "").lower()
    if hostname == "api.deepseek.com" and model.strip().lower() == "deepseek-v4-pro":
        return None
    return default


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
        temperature=_model_temperature(base_url, model, DEFAULT_CHAPTER_TEMPERATURE),
    )


def build_review_llm_config(preferences: dict) -> LLMConfig | None:
    """读取事件规划/审校模型配置；未单独配置时兼容复用正文模型。"""
    preferences = preferences or {}
    review_llm = preferences.get("review_llm") or {}
    if not review_llm.get("enabled"):
        writer_config = build_llm_config(preferences)
        if writer_config is None:
            return None
        return LLMConfig(
            base_url=writer_config.base_url,
            api_key=writer_config.api_key,
            model=writer_config.model,
            temperature=_model_temperature(
                writer_config.base_url,
                writer_config.model,
                DEFAULT_REVIEW_TEMPERATURE,
            ),
            timeout_seconds=writer_config.timeout_seconds,
            total_timeout_seconds=writer_config.total_timeout_seconds,
            max_retries=writer_config.max_retries,
        )

    api_key = str(review_llm.get("api_key") or "").strip()
    if not api_key:
        return None

    base_url = str(review_llm.get("base_url") or "https://api.openai.com/v1").strip()
    model = str(review_llm.get("model") or "gpt-4.1-mini").strip()
    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=_model_temperature(base_url, model, DEFAULT_REVIEW_TEMPERATURE),
        timeout_seconds=DEEP_THINKING_ACTIVITY_TIMEOUT_SECONDS,
        total_timeout_seconds=DEEP_THINKING_TOTAL_TIMEOUT_SECONDS,
    )


def build_test_llm_config(
    preferences: dict,
    *,
    target: str,
    base_url: str = "",
    model: str = "",
    api_key: str = "",
) -> LLMConfig:
    """组合一次性连接测试配置；表单空密钥安全复用服务器已保存密钥。"""
    preferences = preferences or {}
    if target == "writer":
        saved = preferences.get("llm") or {}
        default_temperature = DEFAULT_CHAPTER_TEMPERATURE
    elif target == "reviewer":
        saved = preferences.get("review_llm") or {}
        default_temperature = DEFAULT_REVIEW_TEMPERATURE
    else:
        raise ValueError("不支持的模型测试目标")

    resolved_base_url = str(base_url or saved.get("base_url") or "").strip()
    resolved_model = str(model or saved.get("model") or "").strip()
    resolved_api_key = str(api_key or saved.get("api_key") or "").strip()
    if not resolved_base_url:
        raise ValueError("请填写 Base URL")
    parsed_url = urlparse(resolved_base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("Base URL 必须是有效的 HTTP 或 HTTPS 地址")
    if not resolved_model:
        raise ValueError("请填写模型名称")
    if not resolved_api_key:
        raise ValueError("请填写 API Key，或先保存已有密钥")

    return LLMConfig(
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        model=resolved_model,
        temperature=_model_temperature(
            resolved_base_url,
            resolved_model,
            default_temperature,
        ),
        timeout_seconds=30.0,
        total_timeout_seconds=37.5,
        max_retries=0,
    )
