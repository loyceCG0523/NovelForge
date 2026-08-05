import unittest
from threading import Event
from unittest.mock import patch

import httpx

from app.services.llm_client import (
    EmptyStreamResponseError,
    FixedTemperatureRequiredError,
    InvalidJSONResponseError,
    JSONContentStreamExtractor,
    LLMClient,
    LLMConfig,
    LLMRequestCancelledError,
    ReasoningBudgetExhaustedError,
    build_context_token_budget,
    compress_chat_context,
    estimate_chat_input_tokens,
)


class JSONContentStreamExtractorTests(unittest.TestCase):
    def test_extracts_fragmented_content_and_decodes_escapes(self) -> None:
        extractor = JSONContentStreamExtractor()
        chunks = [
            '{"title":"章名","summary":"摘要","con',
            'tent":"第一段。\\n\\n第',
            '二段含有\\"对白\\"。","chapter_progress":{}}',
        ]

        result = "".join(extractor.feed(chunk) for chunk in chunks)

        self.assertEqual(result, '第一段。\n\n第二段含有"对白"。')

    def test_ignores_content_after_closing_quote(self) -> None:
        extractor = JSONContentStreamExtractor()

        result = extractor.feed('{"content":"正文","chapter_progress":{"actual_summary":"不应输出"}}')

        self.assertEqual(result, "正文")


class FixedTemperatureCompatibilityTests(unittest.TestCase):
    def test_json_mode_adds_lowercase_compatibility_instruction(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://example.test/v1",
                        api_key="secret",
                        model="strict-json-model",
                    )
                )
                self.messages = []

            def _chat_once(self, **kwargs) -> str:
                self.messages = kwargs["payload"]["messages"]
                return '{"ok":true}'

        client = RecordingClient()
        _, parsed = client.complete_json(
            [{"role": "system", "content": "只输出大写 JSON。"}]
        )

        self.assertTrue(parsed["ok"])
        self.assertTrue(
            any("json" in message["content"] for message in client.messages)
        )

    def test_client_retries_once_with_temperature_one(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://example.test/v1",
                        api_key="secret",
                        model="fixed-temperature-model",
                        temperature=0.1,
                    )
                )
                self.temperatures = []

            def _chat_once(self, **kwargs) -> str:
                self.temperatures.append(kwargs["payload"]["temperature"])
                if len(self.temperatures) == 1:
                    raise FixedTemperatureRequiredError()
                return '{"ok":true}'

        client = RecordingClient()
        _, parsed = client.complete_json([{"role": "user", "content": "test"}])

        self.assertEqual(client.temperatures, [0.1, 1])
        self.assertTrue(parsed["ok"])

    def test_temperature_is_omitted_when_config_value_is_none(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://api.deepseek.com",
                        api_key="secret",
                        model="deepseek-v4-pro",
                        temperature=None,
                    )
                )
                self.payload = {}

            def _chat_once(self, **kwargs) -> str:
                self.payload = kwargs["payload"]
                return '{"ok":true}'

        client = RecordingClient()
        _, parsed = client.complete_json([{"role": "user", "content": "test json"}])

        self.assertNotIn("temperature", client.payload)
        self.assertTrue(parsed["ok"])


class DeepSeekV4FlashCompatibilityTests(unittest.TestCase):
    def test_chat_payload_enables_low_reasoning_without_temperature(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://api.deepseek.com",
                        api_key="secret",
                        model="deepseek-v4-flash",
                        temperature=None,
                        reasoning_effort="low",
                    )
                )
                self.payload = {}

            def _chat_once(self, **kwargs) -> str:
                self.payload = kwargs["payload"]
                return '{"ok":true}'

        client = RecordingClient()
        client.complete_json([{"role": "user", "content": "test json"}])

        self.assertEqual(client.payload["thinking"], {"type": "enabled"})
        self.assertEqual(client.payload["reasoning_effort"], "low")
        self.assertNotIn("temperature", client.payload)

    def test_native_web_search_uses_responses_api_and_normalizes_sources(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://api.deepseek.com",
                api_key="secret",
                model="deepseek-v4-flash",
                reasoning_effort="low",
            )
        )
        response = {
            "id": "resp-test",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"sources":[{"title":"上海裁员补偿流程",'
                                '"url":"https://example.com/layoff",'
                                '"snippet":"上海裁员补偿流程与标准",'
                                '"published_at":"2026-01-01","score":0.95}]}'
                            ),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.com/layoff",
                                    "title": "上海裁员补偿流程",
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        with patch.object(client, "_post_with_retries", return_value=response) as request:
            sources = client.search_web("上海裁员补偿流程", max_results=3)

        self.assertEqual(request.call_args.kwargs["url"], "https://api.deepseek.com/responses")
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["tools"], [{"type": "web_search"}])
        self.assertEqual(payload["tool_choice"], {"type": "web_search"})
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertEqual(payload["text"], {"format": {"type": "json_object"}})
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(sources[0]["domain"], "example.com")
        self.assertTrue(sources[0]["payload"]["citation_verified"])


class RequestDeadlineTests(unittest.TestCase):
    def test_default_request_deadline_is_one_minute(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="test-model",
            )
        )

        with patch("app.services.llm_client.time.monotonic", side_effect=[100.0, 160.01]):
            deadline = client._new_request_deadline()
            with self.assertRaisesRegex(RuntimeError, "连续等待有效内容超过 60 秒"):
                client._remaining_request_seconds(deadline)

    def test_structured_json_retry_gets_a_new_first_content_window(self) -> None:
        class DeadlineRecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://example.test/v1",
                        api_key="secret",
                        model="test-model",
                    )
                )
                self.deadlines = []

            def _chat(self, *args, **kwargs) -> str:
                self.deadlines.append(kwargs["request_deadline"])
                if len(self.deadlines) == 1:
                    return "not json"
                return '{"ok":true}'

        client = DeadlineRecordingClient()
        with patch("app.services.llm_client.time.monotonic", side_effect=[100.0, 110.0]):
            _, parsed = client.complete_json([{"role": "user", "content": "return json"}])

        self.assertTrue(parsed["ok"])
        self.assertEqual(len(client.deadlines), 2)
        self.assertEqual(client.deadlines, [160.0, 170.0])


class ContextWindowBudgetTests(unittest.TestCase):
    def test_request_omits_output_limit_parameter(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://example.test/v1",
                        api_key="secret",
                        model="context-model",
                        context_window_tokens=4096,
                    )
                )
                self.payload = {}

            def _chat_once(self, **kwargs) -> str:
                self.payload = kwargs["payload"]
                return '{"ok":true}'

        messages = [{"role": "user", "content": "test json"}]
        client = RecordingClient()
        client.complete_json(messages)

        self.assertNotIn("max_tokens", client.payload)

    def test_overlong_multi_turn_context_is_compressed_locally(self) -> None:
        config = LLMConfig(
            base_url="https://example.test/v1",
            api_key="secret",
            model="context-model",
            context_window_tokens=4096,
        )
        messages = [
            {"role": "system", "content": "必须遵守系统规则。"},
            {"role": "user", "content": "较早问题" * 1000},
            {"role": "assistant", "content": "较早回答" * 1000},
            {"role": "assistant", "content": "最近回答"},
            {"role": "user", "content": "当前请求必须保留"},
        ]

        compressed, telemetry = compress_chat_context(config, messages)

        budget = build_context_token_budget(config, compressed)
        self.assertTrue(telemetry["applied"])
        self.assertLess(len(compressed), len(messages))
        self.assertLessEqual(
            estimate_chat_input_tokens(compressed),
            budget["usable_input_tokens"],
        )
        self.assertEqual(compressed[-1]["content"], "当前请求必须保留")

    def test_single_overlong_prompt_uses_local_fallback_compression(self) -> None:
        config = LLMConfig(
            base_url="https://example.test/v1",
            api_key="secret",
            model="context-model",
            context_window_tokens=4096,
        )
        messages = [{"role": "user", "content": "开头" + "长" * 3000 + "结尾"}]

        compressed, telemetry = compress_chat_context(config, messages)

        self.assertTrue(telemetry["applied"])
        self.assertIn("开头", compressed[0]["content"])
        self.assertIn("结尾", compressed[0]["content"])
        self.assertLessEqual(
            estimate_chat_input_tokens(compressed),
            build_context_token_budget(config, compressed)["usable_input_tokens"],
        )


class PlainTextCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="plain-text-model",
            )
        )

    def test_plain_text_request_does_not_send_json_response_format(self) -> None:
        response = {"choices": [{"message": {"content": "OK"}}]}
        with patch.object(
            self.client,
            "_post_with_retries",
            return_value=response,
        ) as request:
            content = self.client.complete_text(
                [{"role": "user", "content": "回复 OK"}]
            )

        self.assertEqual(content, "OK")
        self.assertNotIn("response_format", request.call_args.kwargs["payload"])
        self.assertFalse(
            any(
                "Return only a valid json object." in message["content"]
                for message in request.call_args.kwargs["payload"]["messages"]
            )
        )

    def test_reasoning_only_response_still_proves_connection_is_valid(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "The endpoint returned a valid reasoning response.",
                    }
                }
            ]
        }
        with patch.object(
            self.client,
            "_post_with_retries",
            return_value=response,
        ):
            content = self.client.complete_text(
                [{"role": "user", "content": "回复 OK"}]
            )

        self.assertIn("valid reasoning response", content)


class GenericJSONStreamingTests(unittest.TestCase):
    def test_malformed_json_is_repaired_without_an_extra_model_call(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://example.test/v1",
                        api_key="secret",
                        model="json-model",
                    )
                )
                self.calls = 0

            def _chat(self, *_args, **_kwargs) -> str:
                self.calls += 1
                return "```json\n{'ok': true,}\n```"

        client = RecordingClient()
        _, parsed = client.complete_json([{"role": "user", "content": "test"}])

        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(client.calls, 1)

    def test_invalid_json_retries_once_with_stronger_contract(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://example.test/v1",
                        api_key="secret",
                        model="json-model",
                    )
                )
                self.messages = []
                self.outputs = iter(["not json at all", '{"ok":true}'])

            def _chat(self, messages, **_kwargs) -> str:
                self.messages.append(messages)
                return next(self.outputs)

        client = RecordingClient()
        _, parsed = client.complete_json([{"role": "user", "content": "test"}])

        self.assertEqual(parsed, {"ok": True})
        self.assertEqual(len(client.messages), 2)
        self.assertIn(
            "The previous response did not satisfy JSON_OUTPUT_CONTRACT",
            client.messages[1][0]["content"],
        )
        self.assertTrue(
            client.last_request_telemetry["structured_output_retry"]["succeeded"]
        )

    def test_required_fields_are_validated_before_accepting_json(self) -> None:
        class RecordingClient(LLMClient):
            def __init__(self) -> None:
                super().__init__(
                    LLMConfig(
                        base_url="https://example.test/v1",
                        api_key="secret",
                        model="json-model",
                    )
                )
                self.outputs = iter(
                    [
                        '{"title":"第一章","content":""}',
                        (
                            '{"title":"第一章","summary":"摘要","content":"正文",'
                            '"chapter_progress":{}}'
                        ),
                    ]
                )

            def _chat(self, *_args, **_kwargs) -> str:
                return next(self.outputs)

        client = RecordingClient()
        _, parsed = client.complete_json(
            [{"role": "user", "content": "test"}],
            required_keys=("title", "summary", "content", "chapter_progress"),
            required_non_empty_keys=("content",),
        )

        self.assertEqual(parsed["content"], "正文")

    def test_two_invalid_json_responses_raise_readable_error(self) -> None:
        class InvalidClient(LLMClient):
            def _chat(self, *_args, **_kwargs) -> str:
                return "not json"

        client = InvalidClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="json-model",
            )
        )

        with self.assertRaisesRegex(InvalidJSONResponseError, "连续两次"):
            client.complete_json([{"role": "user", "content": "test"}])

    def test_non_stream_reasoning_budget_exhaustion_has_precise_error(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="reasoning-model",
                max_retries=0,
            )
        )
        response = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "",
                        "reasoning_content": "thinking until the token limit",
                    },
                }
            ]
        }
        with patch.object(client, "_post_with_retries", return_value=response):
            with self.assertRaisesRegex(
                ReasoningBudgetExhaustedError,
                "耗尽输出 Token",
            ):
                client.complete_json(
                    [{"role": "user", "content": "test"}],
                    stream=False,
                )

        self.assertEqual(client.last_request_telemetry["finish_reasons"], ["length"])

    def test_json_requests_stream_by_default(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
            )
        )
        with patch.object(
            client,
            "_post_stream_with_retries",
            return_value='{"ok":true}',
        ) as stream_request:
            _, parsed = client.complete_json(
                [{"role": "user", "content": "test"}],
            )

        self.assertEqual(parsed, {"ok": True})
        stream_request.assert_called_once()

    def test_patch_json_can_stream_without_chapter_content_callback(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
            )
        )
        with patch.object(
            client,
            "_post_stream_with_retries",
            return_value='{"patches":[]}',
        ) as stream_request:
            _, parsed = client.complete_json(
                [{"role": "user", "content": "test"}],
                stream=True,
            )

        self.assertEqual(parsed, {"patches": []})
        stream_request.assert_called_once()

    def test_empty_stream_falls_back_to_non_stream_with_response_format(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
            )
        )

        def empty_stream(**_kwargs):
            client.last_request_telemetry = {
                "mode": "stream",
                "status": "empty",
                "reasoning_chars": 20,
            }
            raise EmptyStreamResponseError("reasoning only")

        response = {"choices": [{"message": {"content": '{"patches":[]}'}}]}
        with patch.object(
            client,
            "_post_stream_with_retries",
            side_effect=empty_stream,
        ), patch.object(
            client,
            "_post_with_retries",
            return_value=response,
        ) as fallback_request:
            _, parsed = client.complete_json(
                [{"role": "user", "content": "test"}],
                stream=True,
            )

        self.assertEqual(parsed, {"patches": []})
        self.assertEqual(
            fallback_request.call_args.kwargs["payload"]["response_format"],
            {"type": "json_object"},
        )
        self.assertNotIn("stream", fallback_request.call_args.kwargs["payload"])
        self.assertEqual(client.last_request_telemetry["mode"], "non_stream_fallback")
        self.assertEqual(client.last_request_telemetry["status"], "completed")

    def test_reasoning_only_fallback_is_not_used_as_final_json(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
            )
        )
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": '{"patches":[{"unsafe":"thought"}]}',
                    }
                }
            ]
        }
        with patch.object(
            client,
            "_post_stream_with_retries",
            side_effect=EmptyStreamResponseError("reasoning only"),
        ), patch.object(
            client,
            "_post_with_retries",
            return_value=response,
        ):
            with self.assertRaisesRegex(InvalidJSONResponseError, "连续两次"):
                client.complete_json(
                    [{"role": "user", "content": "test"}],
                    stream=True,
                )

        self.assertTrue(client.last_request_telemetry["reasoning_only"])

    def test_reasoning_budget_exhaustion_skips_non_stream_fallback(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
            )
        )
        with patch.object(
            client,
            "_post_stream_with_retries",
            side_effect=ReasoningBudgetExhaustedError("budget exhausted"),
        ), patch.object(client, "_post_with_retries") as fallback_request:
            with self.assertRaises(ReasoningBudgetExhaustedError):
                client.complete_json(
                    [{"role": "user", "content": "test"}],
                    stream=True,
                )

        fallback_request.assert_not_called()


class StreamProtocolCompatibilityTests(unittest.TestCase):
    class _Response:
        status_code = 200

        def __init__(self, lines):
            self.lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_lines(self):
            return iter(self.lines)

    class _Client:
        def __init__(self, responses, *_args, **_kwargs):
            self.responses = responses

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return StreamProtocolCompatibilityTests._Response(self.responses.pop(0))

    def test_reasoning_only_stream_is_retried_before_success(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=1,
            )
        )
        responses = [
            [
                'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
                "data: [DONE]",
            ],
            [
                'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}',
                "data: [DONE]",
            ],
        ]
        resets = []
        activities = []

        def client_factory(*args, **kwargs):
            return self._Client(responses, *args, **kwargs)

        with patch("app.services.llm_client.httpx.Client", side_effect=client_factory), patch(
            "app.services.llm_client.time.sleep"
        ):
            content = client._post_stream_with_retries(
                url="https://example.test/v1/chat/completions",
                headers={},
                payload={},
                on_content_delta=None,
                on_stream_reset=lambda: resets.append(True),
                on_raw_delta=None,
                on_activity=activities.append,
            )

        self.assertEqual(content, '{"ok":true}')
        self.assertEqual(len(resets), 2)
        self.assertEqual(client.last_request_telemetry["attempt"], 2)
        self.assertEqual(client.last_request_telemetry["attempts"][0]["status"], "empty")
        self.assertGreater(
            client.last_request_telemetry["attempts"][0]["reasoning_chars"],
            0,
        )
        self.assertTrue(any(item["kind"] == "reasoning" for item in activities))
        self.assertTrue(any(item.get("reasoning_delta") == "thinking" for item in activities))
        self.assertTrue(any(item["kind"] == "content" for item in activities))

    def test_whitespace_only_stream_is_treated_as_empty_and_retried(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=1,
            )
        )
        responses = [
            [
                'data: {"choices":[{"delta":{"reasoning_content":"thinking",'
                '"content":"   "},"finish_reason":"length"}]}',
                "data: [DONE]",
            ],
            [
                'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"}}]}',
                "data: [DONE]",
            ],
        ]

        def client_factory(*args, **kwargs):
            return self._Client(responses, *args, **kwargs)

        with patch("app.services.llm_client.httpx.Client", side_effect=client_factory), patch(
            "app.services.llm_client.time.sleep"
        ):
            content = client._post_stream_with_retries(
                url="https://example.test/v1/chat/completions",
                headers={},
                payload={},
                on_content_delta=None,
                on_stream_reset=None,
                on_raw_delta=None,
            )

        self.assertEqual(content, '{"ok":true}')
        first_attempt = client.last_request_telemetry["attempts"][0]
        self.assertTrue(first_attempt["whitespace_only"])
        self.assertEqual(first_attempt["received_content_chars"], 3)

    def test_message_content_inside_sse_event_is_accepted(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
            )
        )
        responses = [
            [
                "event: message",
                "id: response-1",
                'data: {"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}',
                "data: [DONE]",
            ]
        ]

        def client_factory(*args, **kwargs):
            return self._Client(responses, *args, **kwargs)

        with patch("app.services.llm_client.httpx.Client", side_effect=client_factory):
            content = client._post_stream_with_retries(
                url="https://example.test/v1/chat/completions",
                headers={},
                payload={},
                on_content_delta=None,
                on_stream_reset=None,
                on_raw_delta=None,
            )

        self.assertEqual(content, '{"ok":true}')

    def test_blank_heartbeats_do_not_refresh_first_content_deadline(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
                timeout_seconds=60,
            )
        )
        responses = [["", ": keepalive"]]
        activities = []

        def client_factory(*args, **kwargs):
            return self._Client(responses, *args, **kwargs)

        with patch("app.services.llm_client.httpx.Client", side_effect=client_factory), patch(
            "app.services.llm_client.time.monotonic",
            side_effect=[0.0, 0.0, 30.0, 60.01],
        ):
            with self.assertRaisesRegex(RuntimeError, "未返回首个有效思考或正文"):
                client._post_stream_with_retries(
                    url="https://example.test/v1/chat/completions",
                    headers={},
                    payload={},
                    on_content_delta=None,
                    on_stream_reset=None,
                    on_raw_delta=None,
                    on_activity=activities.append,
                    request_deadline=60.0,
                )

        self.assertEqual(activities, [])

    def test_stream_can_continue_past_one_minute_while_content_keeps_arriving(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
                timeout_seconds=60,
            )
        )
        responses = [[
            'data: {"choices":[{"delta":{"content":"A"}}]}',
            'data: {"choices":[{"delta":{"content":"B"}}]}',
            'data: {"choices":[{"delta":{"content":"C"}}]}',
            "data: [DONE]",
        ]]

        def client_factory(*args, **kwargs):
            return self._Client(responses, *args, **kwargs)

        with patch("app.services.llm_client.httpx.Client", side_effect=client_factory), patch(
            "app.services.llm_client.time.monotonic",
            side_effect=[0.0, 0.0, 10.0, 50.0, 90.0, 110.0],
        ):
            content = client._post_stream_with_retries(
                url="https://example.test/v1/chat/completions",
                headers={},
                payload={},
                on_content_delta=None,
                on_stream_reset=None,
                on_raw_delta=None,
                request_deadline=60.0,
            )

        self.assertEqual(content, "ABC")

    def test_stream_times_out_after_sixty_seconds_without_new_effective_content(self) -> None:
        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
                timeout_seconds=60,
            )
        )
        responses = [[
            'data: {"choices":[{"delta":{"content":"A"}}]}',
            "",
            ": keepalive",
        ]]

        def client_factory(*args, **kwargs):
            return self._Client(responses, *args, **kwargs)

        with patch("app.services.llm_client.httpx.Client", side_effect=client_factory), patch(
            "app.services.llm_client.time.monotonic",
            side_effect=[0.0, 0.0, 10.0, 50.0, 70.01],
        ):
            with self.assertRaisesRegex(RuntimeError, "连续 60 秒未返回新的有效思考或正文"):
                client._post_stream_with_retries(
                    url="https://example.test/v1/chat/completions",
                    headers={},
                    payload={},
                    on_content_delta=None,
                    on_stream_reset=None,
                    on_raw_delta=None,
                    request_deadline=60.0,
                )

    def test_pause_interrupts_a_blocked_stream_before_read_timeout(self) -> None:
        calls = 0

        def cancel_check() -> bool:
            nonlocal calls
            calls += 1
            return calls >= 2

        class BlockingResponse:
            status_code = 200

            def __init__(self, client):
                self.client = client

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_lines(self):
                self.client.closed.wait(3)
                raise httpx.ReadError("closed by cancellation monitor")

        class BlockingClient:
            def __init__(self, *_args, **_kwargs):
                self.closed = Event()

            @property
            def is_closed(self):
                return self.closed.is_set()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()
                return False

            def close(self):
                self.closed.set()

            def stream(self, *_args, **_kwargs):
                return BlockingResponse(self)

        client = LLMClient(
            LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="stream-model",
                max_retries=0,
                timeout_seconds=30,
                cancel_check=cancel_check,
            )
        )

        with patch("app.services.llm_client.httpx.Client", BlockingClient):
            with self.assertRaises(LLMRequestCancelledError):
                client._post_stream_with_retries(
                    url="https://example.test/v1/chat/completions",
                    headers={},
                    payload={},
                    on_content_delta=None,
                    on_stream_reset=None,
                    on_raw_delta=None,
                )

        self.assertEqual(client.last_request_telemetry["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
