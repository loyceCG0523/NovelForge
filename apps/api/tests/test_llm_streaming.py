import unittest
from unittest.mock import patch

from app.services.llm_client import (
    EmptyStreamResponseError,
    FixedTemperatureRequiredError,
    JSONContentStreamExtractor,
    LLMClient,
    LLMConfig,
    ReasoningBudgetExhaustedError,
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

    def test_empty_stream_falls_back_to_non_stream_without_response_format(self) -> None:
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
        self.assertNotIn("response_format", fallback_request.call_args.kwargs["payload"])
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
            with self.assertRaisesRegex(EmptyStreamResponseError, "reasoning_content"):
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
        self.assertTrue(any(item["kind"] == "content" for item in activities))

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


if __name__ == "__main__":
    unittest.main()
