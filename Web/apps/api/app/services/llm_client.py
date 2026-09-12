"""OpenAI-compatible LLM 调用封装。"""

import json
import re
import time
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from json_repair import repair_json

from app.services.paragraph_formatter import format_chapter_paragraphs


DEFAULT_CHAPTER_TEMPERATURE = 0.7
DEFAULT_REVIEW_TEMPERATURE = 0.1
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
DEFAULT_LLM_TOTAL_TIMEOUT_SECONDS = None
DEEP_THINKING_ACTIVITY_TIMEOUT_SECONDS = 60.0
DEEP_THINKING_TOTAL_TIMEOUT_SECONDS = None
DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS = 128_000
MIN_MODEL_CONTEXT_WINDOW_TOKENS = 4_096
CONTEXT_SAFETY_RESERVE_TOKENS = 1_024
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
JSON_MODE_CONTRACT_MARKER = "JSON_OUTPUT_CONTRACT"
JSON_MODE_COMPATIBILITY_INSTRUCTION = (
    "JSON_OUTPUT_CONTRACT: Return exactly one valid json object matching the requested "
    "fields and the format example already provided in the request. Start with { and end "
    "with }. Do not return Markdown, commentary, or blank/whitespace-only content."
)
JSON_RETRY_INSTRUCTION = (
    "The previous response did not satisfy JSON_OUTPUT_CONTRACT. Generate the answer again "
    "from the original request. Return one complete valid json object only; preserve every "
    "required field and do not output Markdown or any text outside the object."
)
RESPONSE_FORMAT_UNSUPPORTED_MARKERS = (
    "does not support response_format",
    "response_format is not supported",
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
    context_window_tokens: int = DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS
    cancel_check: Callable[[], bool] | None = None
    reasoning_effort: str | None = None


class _CancellationMonitor:
    """后台轮询暂停标记，并关闭阻塞中的 HTTP 连接。"""

    def __init__(self, callback: Callable[[], bool] | None, client: httpx.Client) -> None:
        self.callback = callback
        self.client = client
        self.cancelled = Event()

    def start(self) -> None:
        if self.callback is None:
            return
        Thread(target=self._run, name="llm-cancel-monitor", daemon=True).start()

    def _run(self) -> None:
        while not self.cancelled.wait(0.35):
            if self.client.is_closed:
                return
            try:
                should_cancel = bool(self.callback and self.callback())
            except Exception:
                should_cancel = False
            if should_cancel:
                self.cancelled.set()
                self.client.close()
                return

    def raise_if_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise LLMRequestCancelledError("模型调用已按用户暂停请求中止。")


def is_official_deepseek_v4_flash(base_url: str, model: str) -> bool:
    """仅匹配 DeepSeek 官方根地址与 deepseek-v4-flash。"""
    parsed = urlparse(str(base_url or "").strip())
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.deepseek.com"
        and (parsed.port in {None, 443})
        and parsed.path.rstrip("/") == ""
        and not parsed.query
        and not parsed.fragment
        and str(model or "").strip().lower() == "deepseek-v4-flash"
    )


def is_official_deepseek_model(base_url: str, model: str) -> bool:
    """匹配 DeepSeek 官方根地址与任意 deepseek 系列模型。

    原生 web_search 等能力只要求官方端点 + DeepSeek 系列模型，
    不限定具体型号；型号相关的参数特调仍用 is_official_deepseek_v4_flash。
    """
    parsed = urlparse(str(base_url or "").strip())
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.deepseek.com"
        and (parsed.port in {None, 443})
        and parsed.path.rstrip("/") == ""
        and not parsed.query
        and not parsed.fragment
        and str(model or "").strip().lower().startswith("deepseek")
    )


def normalize_context_window_tokens(value: Any) -> int:
    """读取模型上下文长度；旧配置按 128K 兼容。"""
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS
    return max(MIN_MODEL_CONTEXT_WINDOW_TOKENS, resolved)


def estimate_chat_input_tokens(messages: list[dict[str, str]]) -> int:
    """保守估算消息 Token；按 UTF-8 字节计数，避免自定义模型低估输入。"""
    estimated = 16
    for message in messages:
        estimated += 16
        estimated += len(str(message.get("role") or "").encode("utf-8"))
        estimated += len(str(message.get("content") or "").encode("utf-8"))
    return max(1, estimated)


def build_context_token_budget(
    config: LLMConfig,
    messages: list[dict[str, str]],
) -> dict[str, int]:
    """只计算系统侧输入上下文占用，不把结果映射为模型输出参数。"""
    context_window_tokens = normalize_context_window_tokens(config.context_window_tokens)
    estimated_input_tokens = estimate_chat_input_tokens(messages)
    usable_input_tokens = max(
        1,
        context_window_tokens - CONTEXT_SAFETY_RESERVE_TOKENS,
    )
    return {
        "context_window_tokens": context_window_tokens,
        "estimated_input_tokens": estimated_input_tokens,
        "context_safety_reserve_tokens": CONTEXT_SAFETY_RESERVE_TOKENS,
        "usable_input_tokens": usable_input_tokens,
        "remaining_context_tokens": max(0, usable_input_tokens - estimated_input_tokens),
        "context_overflow_tokens": max(0, estimated_input_tokens - usable_input_tokens),
    }


CONTEXT_COMPRESSION_MARKER = "\n\n…[上下文已压缩]…\n\n"
OLDER_CONTEXT_HEADER = "以下是较早对话的压缩记录：\n"


def _truncate_utf8_text(value: str, max_bytes: int) -> str:
    """按 UTF-8 字节预算保留文本首尾，供超长上下文的最终兜底。"""
    raw = str(value or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(value or "")
    if max_bytes <= 0:
        return ""
    marker = CONTEXT_COMPRESSION_MARKER.encode("utf-8")
    if max_bytes <= len(marker):
        return raw[:max_bytes].decode("utf-8", errors="ignore")
    content_budget = max_bytes - len(marker)
    head_bytes = max(1, int(content_budget * 0.7))
    tail_bytes = max(0, content_budget - head_bytes)
    head = raw[:head_bytes].decode("utf-8", errors="ignore")
    tail = raw[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return f"{head}{CONTEXT_COMPRESSION_MARKER}{tail}"


def compress_chat_context(
    config: LLMConfig,
    messages: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """在本地压缩超长消息，保留系统指令和最近两轮对话的优先级。"""
    prepared = [
        {
            **message,
            "role": str(message.get("role") or "user"),
            "content": str(message.get("content") or ""),
        }
        for message in messages
    ]
    before = build_context_token_budget(config, prepared)
    target_tokens = before["usable_input_tokens"]
    if before["estimated_input_tokens"] <= target_tokens:
        return prepared, {
            "applied": False,
            "messages_before": len(prepared),
            "messages_after": len(prepared),
            "estimated_input_tokens_before": before["estimated_input_tokens"],
            "estimated_input_tokens_after": before["estimated_input_tokens"],
        }

    non_system_indexes = [
        index
        for index, message in enumerate(prepared)
        if message["role"] != "system"
    ]
    recent_indexes = set(non_system_indexes[-2:])
    older_indexes = [
        index
        for index in non_system_indexes
        if index not in recent_indexes
    ]

    # 多轮对话先把较早轮次合并成一条短记录，再视需要压缩单条长消息。
    if older_indexes:
        older_parts = []
        for index in older_indexes:
            message = prepared[index]
            role_label = "助手" if message["role"] == "assistant" else "用户"
            older_parts.append(f"{role_label}：{message['content']}")
        summary = OLDER_CONTEXT_HEADER + "\n".join(older_parts)
        summary_budget = max(256, min(8_192, target_tokens // 5))
        summary_message = {
            "role": "system",
            "content": _truncate_utf8_text(summary, summary_budget),
        }
        first_older = older_indexes[0]
        prepared = [
            message
            for index, message in enumerate(prepared)
            if index not in set(older_indexes)
        ]
        insert_at = min(first_older, len(prepared))
        prepared.insert(insert_at, summary_message)

    def current_estimate() -> int:
        return estimate_chat_input_tokens(prepared)

    # 精确削减超出的部分。优先压缩历史记录和较早消息，最后才动最近请求。
    for minimum_keep in (256, 0):
        while current_estimate() > target_tokens:
            overflow = current_estimate() - target_tokens
            candidates = sorted(
                range(len(prepared)),
                key=lambda index: (
                    0 if prepared[index]["content"].startswith(OLDER_CONTEXT_HEADER) else 1,
                    2 if prepared[index]["role"] == "system" else 1,
                    index,
                ),
            )
            changed = False
            for index in candidates:
                content = prepared[index]["content"]
                content_bytes = len(content.encode("utf-8"))
                if content_bytes <= minimum_keep:
                    continue
                next_budget = max(minimum_keep, content_bytes - overflow - 32)
                truncated = _truncate_utf8_text(content, next_budget)
                if truncated != content:
                    prepared[index]["content"] = truncated
                    changed = True
                    break
            if not changed:
                break

    after = build_context_token_budget(config, prepared)
    if after["estimated_input_tokens"] > target_tokens:
        raise ContextWindowExceededError(
            "上下文已尝试压缩，但消息协议开销仍超过模型支持的最大上下文长度。"
        )
    return prepared, {
        "applied": True,
        "messages_before": len(messages),
        "messages_after": len(prepared),
        "estimated_input_tokens_before": before["estimated_input_tokens"],
        "estimated_input_tokens_after": after["estimated_input_tokens"],
        "removed_estimated_tokens": (
            before["estimated_input_tokens"] - after["estimated_input_tokens"]
        ),
    }


def ensure_json_mode_instruction(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """为 JSON Mode 强制补充小写 json、边界和禁止空白输出约束。"""
    prepared = [dict(message) for message in messages]
    if any(
        JSON_MODE_CONTRACT_MARKER in str(message.get("content") or "")
        for message in prepared
    ):
        return prepared
    for message in prepared:
        if message.get("role") != "system":
            continue
        content = str(message.get("content") or "").rstrip()
        message["content"] = (
            f"{content}\n{JSON_MODE_COMPATIBILITY_INSTRUCTION}"
            if content
            else JSON_MODE_COMPATIBILITY_INSTRUCTION
        )
        return prepared
    return [
        {"role": "system", "content": JSON_MODE_COMPATIBILITY_INSTRUCTION},
        *prepared,
    ]


def strengthen_json_retry_instruction(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """结构化输出失败后强化原请求，不把错误响应重新塞回上下文。"""
    prepared = ensure_json_mode_instruction(messages)
    for message in prepared:
        if message.get("role") != "system":
            continue
        content = str(message.get("content") or "").rstrip()
        message["content"] = f"{content}\n{JSON_RETRY_INSTRUCTION}"
        return prepared
    return [
        {
            "role": "system",
            "content": f"{JSON_MODE_COMPATIBILITY_INSTRUCTION}\n{JSON_RETRY_INSTRUCTION}",
        },
        *prepared,
    ]


class LLMClient:
    """调用 OpenAI-compatible chat completions 接口。"""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        # 只记录协议级摘要，不保存提示词或模型正文，便于定位兼容接口问题。
        self.last_request_telemetry: dict[str, Any] = {}

    def _raise_if_cancelled(self) -> None:
        """在请求边界响应上层暂停信号。"""
        callback = self.config.cancel_check
        if callback is None:
            return
        try:
            cancelled = bool(callback())
        except Exception:
            cancelled = False
        if cancelled:
            raise LLMRequestCancelledError("模型调用已按用户暂停请求中止。")

    def _new_request_deadline(self) -> float | None:
        """创建首个有效内容截止时间；流开始后改由静默窗口接管。"""
        timeout_seconds = (
            self.config.total_timeout_seconds
            if self.config.total_timeout_seconds is not None
            else self.config.timeout_seconds
        )
        return time.monotonic() + max(1.0, float(timeout_seconds))

    def _remaining_request_seconds(self, deadline: float | None) -> float:
        """返回当前网络步骤可用时间；截止后立即停止，不再开启下一次重试。"""
        if deadline is None:
            return max(1.0, float(self.config.timeout_seconds))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timeout_seconds = (
                self.config.total_timeout_seconds
                if self.config.total_timeout_seconds is not None
                else self.config.timeout_seconds
            )
            raise RuntimeError(
                f"LLM API 连续等待有效内容超过 {int(timeout_seconds)} 秒，已停止本次请求。"
            )
        return max(0.05, min(float(self.config.timeout_seconds), remaining))

    @staticmethod
    def _sleep_before_retry(delay: float, deadline: float | None) -> None:
        """重试退避也计入整次请求总时限。"""
        if deadline is not None:
            delay = min(max(0.0, delay), max(0.0, deadline - time.monotonic()))
        if delay:
            time.sleep(delay)

    def generate_chapter(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_stream_reset: Callable[[], None] | None = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """调用模型并解析章节 JSON 输出。"""
        content, parsed = self.complete_json(
            messages,
            temperature=temperature,
            on_content_delta=on_content_delta,
            on_stream_reset=on_stream_reset,
            on_activity=on_activity,
            required_keys=("title", "summary", "content", "chapter_progress"),
            required_non_empty_keys=("content",),
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
        temperature: float | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_stream_reset: Callable[[], None] | None = None,
        stream: bool = True,
        on_raw_delta: Callable[[str], None] | None = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
        required_keys: tuple[str, ...] = (),
        required_non_empty_keys: tuple[str, ...] = (),
    ) -> tuple[str, dict[str, Any]]:
        """接收并校验 JSON；空白、畸形或缺字段时强化契约后完整重试一次。"""
        request_messages = messages
        first_failure = ""
        first_telemetry: dict[str, Any] = {}
        for structured_attempt in range(2):
            # JSON 修复重试是一次新的上游请求，重新获得首个有效内容等待窗口。
            request_deadline = self._new_request_deadline()
            try:
                content = self._chat(
                    request_messages,
                    temperature=temperature,
                    on_content_delta=on_content_delta,
                    on_stream_reset=on_stream_reset,
                    stream=stream,
                    on_raw_delta=on_raw_delta,
                    on_activity=on_activity,
                    request_deadline=request_deadline,
                )
                parsed = parse_json_response(content)
                validate_json_response_contract(
                    parsed,
                    required_keys=required_keys,
                    required_non_empty_keys=required_non_empty_keys,
                )
                if structured_attempt:
                    self.last_request_telemetry = {
                        **self.last_request_telemetry,
                        "structured_output_retry": {
                            "attempted": True,
                            "succeeded": True,
                            "first_failure": first_failure,
                            "first_attempt": first_telemetry,
                        },
                    }
                return content, parsed
            except LLMRequestCancelledError:
                raise
            except ReasoningBudgetExhaustedError:
                raise
            except (EmptyStreamResponseError, InvalidJSONResponseError) as exc:
                if structured_attempt:
                    self.last_request_telemetry = {
                        **self.last_request_telemetry,
                        "structured_output_retry": {
                            "attempted": True,
                            "succeeded": False,
                            "first_failure": first_failure,
                            "final_failure": str(exc),
                            "first_attempt": first_telemetry,
                        },
                    }
                    raise InvalidJSONResponseError(
                        "模型连续两次未返回符合系统要求的有效 JSON，已停止本步骤；"
                        "可以安全重试，不会写入不完整结果。"
                    ) from exc
                first_failure = str(exc)
                first_telemetry = dict(self.last_request_telemetry)
                request_messages = strengthen_json_retry_instruction(messages)
        raise InvalidJSONResponseError("模型未返回符合系统要求的有效 JSON。")

    def complete_text(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
    ) -> str:
        """执行一次普通文本补全，供连接测试等无需 JSON 解析的轻量场景使用。"""
        return self._chat(
            messages,
            temperature=temperature,
            json_mode=False,
            request_deadline=self._new_request_deadline(),
        )

    def search_web(self, query: str, *, max_results: int = 3) -> list[dict[str, Any]]:
        """使用 DeepSeek Responses API 的服务端 web_search 工具。"""
        if not is_official_deepseek_model(self.config.base_url, self.config.model):
            raise ValueError("当前模型配置不支持 DeepSeek 原生 web_search")
        cleaned_query = str(query or "").strip()
        if not cleaned_query:
            return []
        limit = max(1, min(int(max_results), 8))
        payload = {
            "model": self.config.model,
            "instructions": (
                "你是小说创作资料检索助手。必须先使用 web_search，只返回真实检索到的网页。"
                "查询若涉及情节写法，snippet 概括可迁移的冲突、反转、场景和悬念方法；若涉及搞笑话术，"
                "snippet 概括真实口语的语气、节奏、接话和回应结构，不大段摘抄原文。"
                "最终仅输出 json 对象，不要输出解释。每条来源包含 title、url、snippet、"
                "published_at、score；不得编造网址或来源内容。"
            ),
            "input": (
                f"检索：{cleaned_query}\n"
                f"最多返回 {limit} 条高相关来源。格式："
                '{"sources":[{"title":"","url":"https://...","snippet":"",'
                '"published_at":"YYYY-MM-DD或空字符串","score":0.0}]}'
            ),
            "tools": [{"type": "web_search"}],
            "tool_choice": {"type": "web_search"},
            "reasoning": {"effort": self.config.reasoning_effort or "low"},
            "text": {"format": {"type": "json_object"}},
        }
        response = self._post_with_retries(
            url=self.config.base_url.rstrip("/") + "/responses",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            request_deadline=self._new_request_deadline(),
        )
        output_text = _extract_responses_output_text(response)
        citations = _extract_responses_url_citations(response)
        return _normalize_web_search_sources(
            output_text,
            citations=citations,
            response_id=str(response.get("id") or ""),
            max_results=limit,
        )

    def _chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_stream_reset: Callable[[], None] | None = None,
        stream: bool = False,
        on_raw_delta: Callable[[str], None] | None = None,
        on_activity: Callable[[dict[str, Any]], None] | None = None,
        json_mode: bool = True,
        request_deadline: float | None = None,
    ) -> str:
        """执行 chat completions 请求。"""
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        request_messages = (
            ensure_json_mode_instruction(messages)
            if json_mode
            else messages
        )
        prepared_messages, compression = compress_chat_context(
            self.config,
            request_messages,
        )
        payload = {
            "model": self.config.model,
            "messages": prepared_messages,
        }
        if self.config.reasoning_effort:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.config.reasoning_effort
        context_budget = build_context_token_budget(self.config, prepared_messages)
        self.last_request_telemetry = {
            "context_budget": context_budget,
            "context_compression": compression,
        }
        resolved_temperature = self.config.temperature if temperature is None else temperature
        if resolved_temperature is not None:
            payload["temperature"] = resolved_temperature
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
                request_deadline=request_deadline,
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
                request_deadline=request_deadline,
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
        request_deadline: float | None,
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
                request_deadline=request_deadline,
            )
        if not json_mode:
            data = self._post_with_retries(
                url=url,
                headers=headers,
                payload=payload,
                request_deadline=request_deadline,
            )
            return _extract_text_response(data, allow_reasoning=True)
        try:
            data = self._post_with_retries(
                url=url,
                headers=headers,
                payload={**payload, "response_format": {"type": "json_object"}},
                request_deadline=request_deadline,
            )
        except UnsupportedResponseFormatError:
            data = self._post_with_retries(
                url=url,
                headers=headers,
                payload=payload,
                request_deadline=request_deadline,
            )
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
        request_deadline: float | None,
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
                    request_deadline=request_deadline,
                )
            except EmptyStreamResponseError as exc:
                if isinstance(exc, ReasoningBudgetExhaustedError):
                    raise
                # 部分 OpenAI-compatible 网关会以 200 结束 SSE，却没有输出
                # delta.content。流式重试耗尽后保留当前已协商成功的 JSON Mode
                # 参数做一次普通请求，避免降级请求重新产生非 JSON 输出。
                last_error = exc
                fallback_payload = dict(variant)
                fallback_payload.pop("stream", None)
                data = self._post_with_retries(
                    url=url,
                    headers=headers,
                    payload=fallback_payload,
                    # 上游已经结束了流式响应；兼容降级是一次新请求，
                    # 因此重新计算“首个有效内容”等待窗口。
                    request_deadline=self._new_request_deadline(),
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
        request_deadline: float | None = None,
    ) -> str:
        last_error: Exception | None = None
        attempt_summaries: list[dict[str, Any]] = []
        received_effective_content_ever = False
        for attempt in range(self.config.max_retries + 1):
            self._raise_if_cancelled()
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
            first_content_deadline = (
                request_deadline
                if request_deadline is not None and not received_effective_content_ever
                else attempt_started_at + max(1.0, float(self.config.timeout_seconds))
            )
            first_activity_at: float | None = None
            last_activity_at: float | None = None
            cancel_monitor: _CancellationMonitor | None = None
            try:
                stream_payload = {**payload, "stream": True}
                activity_timeout = max(1.0, float(self.config.timeout_seconds))
                initial_wait_timeout = (
                    activity_timeout
                    if received_effective_content_ever
                    else self._remaining_request_seconds(request_deadline)
                )
                timeout = httpx.Timeout(
                    connect=min(initial_wait_timeout, 60.0),
                    read=initial_wait_timeout,
                    write=initial_wait_timeout,
                    pool=min(initial_wait_timeout, 60.0),
                )
                with httpx.Client(timeout=timeout) as client:
                    cancel_monitor = _CancellationMonitor(self.config.cancel_check, client)
                    cancel_monitor.start()
                    with client.stream("POST", url, headers=headers, json=stream_payload) as response:
                        if response.status_code >= 400:
                            response.read()
                            response.raise_for_status()
                        for line in response.iter_lines():
                            cancel_monitor.raise_if_cancelled()
                            activity_at = time.monotonic()
                            if first_activity_at is None:
                                if activity_at >= first_content_deadline:
                                    raise LLMInactivityTimeoutError(
                                        f"LLM API {int(activity_timeout)} 秒内未返回首个有效思考或正文。"
                                    )
                            elif (
                                last_activity_at is not None
                                and activity_at - last_activity_at >= activity_timeout
                            ):
                                raise LLMInactivityTimeoutError(
                                    f"LLM API 连续 {int(activity_timeout)} 秒未返回新的有效思考或正文。"
                                )
                            if not line:
                                continue
                            if not line.startswith("data:"):
                                # 合法 SSE 还可能包含 event/id/retry/注释行，它们
                                # 不是模型正文；仅保留可能属于普通 JSON 响应的行。
                                if not line.startswith(("event:", "id:", "retry:", ":")):
                                    plain_lines.append(line)
                                    if line.strip():
                                        if first_activity_at is None:
                                            first_activity_at = activity_at
                                        last_activity_at = activity_at
                                        received_effective_content_ever = True
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
                                if isinstance(reasoning, str) and reasoning.strip():
                                    if first_activity_at is None:
                                        first_activity_at = activity_at
                                    last_activity_at = activity_at
                                    received_effective_content_ever = True
                                if on_activity is not None and isinstance(reasoning, str) and reasoning.strip():
                                    try:
                                        on_activity(
                                            {
                                                "kind": "reasoning",
                                                "attempt": attempt + 1,
                                                "event_count": event_count,
                                                "reasoning_delta": reasoning if isinstance(reasoning, str) else "",
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
                            has_effective_content = bool(delta.strip())
                            has_effective_reasoning = bool(
                                isinstance(reasoning, str) and reasoning.strip()
                            )
                            if has_effective_content or has_effective_reasoning:
                                if first_activity_at is None:
                                    first_activity_at = activity_at
                                last_activity_at = activity_at
                                received_effective_content_ever = True
                            if on_activity is not None and (
                                has_effective_content or has_effective_reasoning
                            ):
                                try:
                                    on_activity(
                                        {
                                            "kind": "content",
                                            "attempt": attempt + 1,
                                            "event_count": event_count,
                                            "reasoning_delta": reasoning if isinstance(reasoning, str) else "",
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
                    cancel_monitor.raise_if_cancelled()
                combined_content = "".join(raw_parts)
                whitespace_only_chars = (
                    len(combined_content)
                    if combined_content and not combined_content.strip()
                    else 0
                )
                if combined_content.strip():
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
                    return combined_content
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
                    "received_content_chars": whitespace_only_chars,
                    "whitespace_only": bool(whitespace_only_chars),
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
                if not whitespace_only_chars and reasoning_chars and "length" in finish_reasons:
                    raise ReasoningBudgetExhaustedError(
                        "模型思考过程耗尽输出 Token，未生成最终 content。"
                    )
                raise EmptyStreamResponseError(
                    "LLM API 流式响应只有空白字符，未返回有效 JSON content。"
                    if whitespace_only_chars
                    else (
                        "LLM API 流式响应仅包含 reasoning_content，未返回最终 content。"
                        if reasoning_chars
                        else "LLM API 流式响应为空，未返回最终 content。"
                    )
                )
            except LLMRequestCancelledError:
                self.last_request_telemetry = {
                    "mode": "stream",
                    "status": "cancelled",
                    "attempt": attempt + 1,
                    "event_count": event_count,
                    "output_chars": output_chars,
                    "reasoning_chars": reasoning_chars,
                }
                raise
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
                if _is_fixed_temperature_error(exc):
                    raise FixedTemperatureRequiredError("LLM API 仅支持 temperature=1") from exc
                if exc.response.status_code not in RETRYABLE_STATUS_CODES or attempt >= self.config.max_retries:
                    raise RuntimeError(f"LLM API 返回错误：HTTP {exc.response.status_code}：{exc.response.text[:500]}") from exc
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                if cancel_monitor is not None and cancel_monitor.cancelled.is_set():
                    self.last_request_telemetry = {
                        "mode": "stream",
                        "status": "cancelled",
                        "attempt": attempt + 1,
                        "event_count": event_count,
                        "output_chars": output_chars,
                        "reasoning_chars": reasoning_chars,
                    }
                    cancel_monitor.raise_if_cancelled()
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise RuntimeError(
                        f"LLM API 流式连接失败或超时（{type(exc).__name__}）。"
                    ) from exc
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise RuntimeError("LLM API 流式响应格式无效。") from exc
            self._sleep_before_retry(
                1.2 * (attempt + 1),
                None if received_effective_content_ever else request_deadline,
            )
        raise RuntimeError(f"LLM API 流式请求失败：{last_error}") from last_error

    def _post_with_retries(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        request_deadline: float | None = None,
    ) -> dict:
        """带重试的 HTTP 请求，处理临时 SSL/代理/网关抖动。"""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._raise_if_cancelled()
            try:
                request_timeout = self._remaining_request_seconds(request_deadline)
                with httpx.Client(timeout=request_timeout) as client:
                    response = client.post(url, headers=headers, json=payload)
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.config.max_retries:
                        self._sleep_before_retry(1.2 * (attempt + 1), request_deadline)
                        continue
                    response.raise_for_status()
                    self._raise_if_cancelled()
                    try:
                        return response.json()
                    except json.JSONDecodeError as exc:
                        last_error = exc
                        if attempt >= self.config.max_retries:
                            raise RuntimeError(
                                "LLM API 返回 HTTP 200，但响应体不是有效 JSON。"
                            ) from exc
                        self._sleep_before_retry(1.2 * (attempt + 1), request_deadline)
                        continue
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if _is_unsupported_response_format_error(exc):
                    raise UnsupportedResponseFormatError(
                        f"LLM API 不支持 response_format 参数：HTTP {exc.response.status_code}"
                    ) from exc
                if _is_fixed_temperature_error(exc):
                    raise FixedTemperatureRequiredError(
                        f"LLM API 仅支持 temperature=1：HTTP {exc.response.status_code}"
                    ) from exc
                if exc.response.status_code not in RETRYABLE_STATUS_CODES or attempt >= self.config.max_retries:
                    raise RuntimeError(
                        f"LLM API 返回错误：HTTP {exc.response.status_code}：{exc.response.text[:500]}"
                    ) from exc
                self._sleep_before_retry(1.2 * (attempt + 1), request_deadline)
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    raise RuntimeError(
                        f"LLM API 网络连接失败（{type(exc).__name__}）："
                        "HTTPS 连接被提前断开或超时。"
                        "请检查代理/VPN、base_url 是否正确，或稍后重试。"
                    ) from exc
                self._sleep_before_retry(1.2 * (attempt + 1), request_deadline)

        raise RuntimeError(f"LLM API 请求失败：{last_error}") from last_error


class UnsupportedResponseFormatError(RuntimeError):
    """供应商兼容接口不支持 response_format 时触发，用于自动降级重试。"""


class LLMRequestCancelledError(RuntimeError):
    """用户暂停任务后，用于立即终止尚未完成的模型请求。"""


class ContextWindowExceededError(RuntimeError):
    """输入和安全预留已超过模型配置的最大上下文窗口。"""


class FixedTemperatureRequiredError(RuntimeError):
    """供应商兼容模型只允许 temperature=1 时触发，用于自动兼容重试。"""


class EmptyStreamResponseError(RuntimeError):
    """HTTP/SSE 正常结束，但供应商没有返回可用的最终 content。"""


class InvalidJSONResponseError(RuntimeError):
    """模型正文为空、JSON 损坏，或缺少当前步骤要求的结构字段。"""


class ReasoningBudgetExhaustedError(EmptyStreamResponseError):
    """输出额度全部被 reasoning_content 消耗，未生成最终 content。"""


class LLMInactivityTimeoutError(RuntimeError):
    """首个有效内容或流式增量超过静默窗口仍未到达。"""


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


def _extract_responses_output_text(data: dict[str, Any]) -> str:
    """兼容 Responses API 顶层 output_text 与 message 内容块。"""
    direct = data.get("output_text") if isinstance(data, dict) else None
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text_value = content.get("text") or content.get("output_text")
            if isinstance(text_value, str):
                parts.append(text_value)
    return "".join(parts).strip()


def _extract_responses_url_citations(data: dict[str, Any]) -> list[dict[str, str]]:
    """递归提取 Responses API 输出中的 URL 引用标注。"""
    citations: list[dict[str, str]] = []
    seen: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("type") == "url_citation":
            url = str(value.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                citations.append(
                    {
                        "url": url,
                        "title": str(value.get("title") or "").strip(),
                    }
                )
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                visit(nested)

    visit(data.get("output") or [])
    return citations


def _normalize_web_search_sources(
    output_text: str,
    *,
    citations: list[dict[str, str]],
    response_id: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """把 DeepSeek web_search 最终 JSON 与引用标注整理成研究来源。"""
    try:
        parsed = parse_json_response(output_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = {}
    raw_sources = parsed.get("sources") if isinstance(parsed, dict) else []
    if not isinstance(raw_sources, list):
        raw_sources = []

    citation_by_url = {
        str(item.get("url") or "").strip(): item
        for item in citations
        if str(item.get("url") or "").strip()
    }
    candidates = list(raw_sources)
    if citations and candidates:
        candidates = [
            item
            for item in candidates
            if isinstance(item, dict)
            and str(item.get("url") or "").strip() in citation_by_url
        ]
    if not candidates:
        candidates = [
            {
                "title": item.get("title") or item.get("url"),
                "url": item.get("url"),
                "snippet": item.get("title") or "DeepSeek web_search 检索来源",
                "published_at": "",
                "score": 0.8,
            }
            for item in citations
        ]

    normalized: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            continue
        if url in seen_urls:
            continue
        citation = citation_by_url.get(url) or {}
        title = str(item.get("title") or citation.get("title") or parsed_url.netloc).strip()[:500]
        snippet = str(item.get("snippet") or title).strip()[:1800]
        try:
            score = float(item.get("score") or 0.8)
        except (TypeError, ValueError):
            score = 0.8
        seen_urls.add(url)
        normalized.append(
            {
                "title": title,
                "url": url,
                "domain": (parsed_url.hostname or "").lower(),
                "snippet": snippet,
                "score": max(0.0, min(score, 1.0)),
                "published_at": str(item.get("published_at") or "").strip()[:80],
                "payload": {
                    "deepseek_response_id": response_id,
                    "native_tool": "web_search",
                    "citation_verified": bool(citation),
                },
            }
        )
        if len(normalized) >= max_results:
            break
    return normalized


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
    prompt_word_rejected = (
        "must contain the word 'json'" in text
        and "json_object" in text
    )
    return prompt_word_rejected or (
        "response_format" in text
        and any(marker in text for marker in RESPONSE_FORMAT_UNSUPPORTED_MARKERS)
    )


def _is_fixed_temperature_error(exc: httpx.HTTPStatusError) -> bool:
    """判断 400 错误是否要求固定使用 temperature=1。"""
    if exc.response.status_code != 400:
        return False
    text = exc.response.text.lower()
    return "temperature" in text and any(marker in text for marker in FIXED_TEMPERATURE_MARKERS)

def parse_json_response(content: str) -> dict[str, Any]:
    """提取、轻量修复并校验模型返回的 JSON 对象。"""
    cleaned = str(content or "").lstrip("\ufeff\u200b\u200c\u200d\u2060").strip()
    if not cleaned:
        raise InvalidJSONResponseError("模型返回了空白内容，无法解析为 JSON。")
    candidate = _extract_json_candidate(cleaned)
    try:
        parsed = json.loads(candidate, strict=False)
    except json.JSONDecodeError as first_error:
        try:
            repaired = repair_json(candidate)
            parsed = (
                repaired
                if isinstance(repaired, (dict, list))
                else json.loads(str(repaired or ""), strict=False)
            )
        except (json.JSONDecodeError, TypeError, ValueError) as repair_error:
            raise InvalidJSONResponseError(
                "模型返回内容不是有效 JSON，自动修复也未成功。"
            ) from first_error
    if not isinstance(parsed, dict):
        raise InvalidJSONResponseError("模型返回的 JSON 顶层必须是对象。")
    return parsed


def validate_json_response_contract(
    parsed: dict[str, Any],
    *,
    required_keys: tuple[str, ...] = (),
    required_non_empty_keys: tuple[str, ...] = (),
) -> None:
    """校验当前业务步骤要求的字段，避免把半截 JSON 当成成功结果写入。"""
    missing = [key for key in required_keys if key not in parsed]
    empty = [
        key
        for key in required_non_empty_keys
        if key not in parsed
        or parsed.get(key) is None
        or (isinstance(parsed.get(key), (str, list, dict)) and not parsed.get(key))
    ]
    if not missing and not empty:
        return
    details: list[str] = []
    if missing:
        details.append("缺少字段：" + "、".join(missing))
    if empty:
        details.append("字段为空：" + "、".join(empty))
    raise InvalidJSONResponseError("模型 JSON 不符合当前步骤约定（" + "；".join(details) + "）。")


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
    if is_official_deepseek_v4_flash(base_url, model) or (
        hostname == "api.deepseek.com"
        and model.strip().lower() == "deepseek-v4-pro"
    ):
        return None
    return default


def _model_reasoning_effort(base_url: str, model: str) -> str | None:
    """DeepSeek 官方 v4-flash 默认使用低思考强度。"""
    return "low" if is_official_deepseek_v4_flash(base_url, model) else None


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
        reasoning_effort=_model_reasoning_effort(base_url, model),
        context_window_tokens=normalize_context_window_tokens(
            llm.get("context_window_tokens")
        ),
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
            context_window_tokens=writer_config.context_window_tokens,
            reasoning_effort=writer_config.reasoning_effort,
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
        reasoning_effort=_model_reasoning_effort(base_url, model),
        timeout_seconds=DEEP_THINKING_ACTIVITY_TIMEOUT_SECONDS,
        total_timeout_seconds=DEEP_THINKING_TOTAL_TIMEOUT_SECONDS,
        context_window_tokens=normalize_context_window_tokens(
            review_llm.get("context_window_tokens")
        ),
    )


def build_test_llm_config(
    preferences: dict,
    *,
    target: str,
    base_url: str = "",
    model: str = "",
    api_key: str = "",
    context_window_tokens: int | None = None,
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
        reasoning_effort=_model_reasoning_effort(
            resolved_base_url,
            resolved_model,
        ),
        timeout_seconds=30.0,
        total_timeout_seconds=37.5,
        max_retries=0,
        context_window_tokens=normalize_context_window_tokens(
            context_window_tokens
            if context_window_tokens is not None
            else saved.get("context_window_tokens")
        ),
    )
