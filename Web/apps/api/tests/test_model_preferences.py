"""正文模型、审校模型与 Tavily 偏好配置回归测试。"""

import unittest

from app.api.users import merge_preferences
from app.schemas.user import mask_api_key, sanitize_preferences
from app.services.llm_client import (
    DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS,
    DEFAULT_REVIEW_TEMPERATURE,
    LLMConfig,
    build_context_token_budget,
    build_llm_config,
    build_review_llm_config,
    build_test_llm_config,
    is_official_deepseek_v4_flash,
)
from app.services.embedding_client import (
    build_embedding_config,
    build_test_embedding_config,
    normalize_embedding_endpoint,
)
from app.services.tavily_search import build_tavily_config


class ModelPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preferences = {
            "llm": {
                "base_url": "https://writer.example/v1",
                "api_key": "writer-secret",
                "model": "writer-model",
            }
        }

    def test_review_model_reuses_writer_api_by_default(self) -> None:
        writer = build_llm_config(self.preferences)
        reviewer = build_review_llm_config(self.preferences)

        self.assertIsNotNone(writer)
        self.assertIsNotNone(reviewer)
        self.assertEqual(reviewer.base_url, writer.base_url)
        self.assertEqual(reviewer.api_key, writer.api_key)
        self.assertEqual(reviewer.model, writer.model)
        self.assertEqual(reviewer.temperature, DEFAULT_REVIEW_TEMPERATURE)
        self.assertEqual(writer.timeout_seconds, 60.0)
        self.assertIsNone(writer.total_timeout_seconds)
        self.assertEqual(reviewer.timeout_seconds, 60.0)
        self.assertIsNone(reviewer.total_timeout_seconds)
        self.assertEqual(writer.context_window_tokens, DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS)
        self.assertEqual(reviewer.context_window_tokens, DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS)

    def test_writer_and_reviewer_keep_independent_context_windows(self) -> None:
        preferences = {
            "llm": {
                **self.preferences["llm"],
                "context_window_tokens": 131072,
            },
            "review_llm": {
                "enabled": True,
                "base_url": "https://reviewer.example/v1",
                "api_key": "reviewer-secret",
                "model": "reviewer-model",
                "context_window_tokens": 200000,
            },
        }

        self.assertEqual(build_llm_config(preferences).context_window_tokens, 131072)
        self.assertEqual(build_review_llm_config(preferences).context_window_tokens, 200000)

    def test_context_budget_reports_input_overflow_without_creating_output_limit(self) -> None:
        config = LLMConfig(
            base_url="https://example.invalid",
            api_key="test",
            model="test",
            context_window_tokens=4096,
        )
        budget = build_context_token_budget(
            config,
            [{"role": "user", "content": "short prompt"}],
        )

        self.assertLessEqual(
            budget["estimated_input_tokens"]
            + budget["remaining_context_tokens"]
            + budget["context_safety_reserve_tokens"],
            budget["context_window_tokens"],
        )

        overflow = build_context_token_budget(
            config,
            [{"role": "user", "content": "长" * 2000}],
        )
        self.assertGreater(overflow["context_overflow_tokens"], 0)
        self.assertEqual(overflow["remaining_context_tokens"], 0)

    def test_independent_review_model_uses_its_own_api(self) -> None:
        preferences = {
            **self.preferences,
            "review_llm": {
                "enabled": True,
                "base_url": "https://reviewer.example/v1",
                "api_key": "reviewer-secret",
                "model": "reviewer-model",
            },
        }

        reviewer = build_review_llm_config(preferences)

        self.assertIsNotNone(reviewer)
        self.assertEqual(reviewer.base_url, "https://reviewer.example/v1")
        self.assertEqual(reviewer.api_key, "reviewer-secret")
        self.assertEqual(reviewer.model, "reviewer-model")
        self.assertEqual(reviewer.timeout_seconds, 60.0)
        self.assertIsNone(reviewer.total_timeout_seconds)

    def test_official_deepseek_v4_pro_omits_ignored_temperature(self) -> None:
        preferences = {
            **self.preferences,
            "review_llm": {
                "enabled": True,
                "base_url": "https://api.deepseek.com",
                "api_key": "reviewer-secret",
                "model": "deepseek-v4-pro",
            },
        }

        reviewer = build_review_llm_config(preferences)

        self.assertIsNotNone(reviewer)
        self.assertIsNone(reviewer.temperature)

    def test_official_deepseek_v4_flash_uses_low_reasoning(self) -> None:
        preferences = {
            "llm": {
                "base_url": "https://api.deepseek.com/",
                "api_key": "deepseek-secret",
                "model": "deepseek-v4-flash",
            }
        }

        writer = build_llm_config(preferences)
        reviewer = build_review_llm_config(preferences)

        self.assertIsNotNone(writer)
        self.assertIsNotNone(reviewer)
        self.assertIsNone(writer.temperature)
        self.assertEqual(writer.reasoning_effort, "low")
        self.assertEqual(reviewer.reasoning_effort, "low")

    def test_deepseek_flash_adapter_only_matches_official_root_url(self) -> None:
        self.assertTrue(
            is_official_deepseek_v4_flash(
                "https://api.deepseek.com",
                "deepseek-v4-flash",
            )
        )
        self.assertFalse(
            is_official_deepseek_v4_flash(
                "https://api.deepseek.com/v1",
                "deepseek-v4-flash",
            )
        )
        self.assertFalse(
            is_official_deepseek_v4_flash(
                "https://api.deepseek.com",
                "deepseek-v4-pro",
            )
        )

    def test_enabled_review_model_requires_its_own_key(self) -> None:
        preferences = {**self.preferences, "review_llm": {"enabled": True}}

        self.assertIsNone(build_review_llm_config(preferences))

    def test_review_secret_is_preserved_and_sanitized(self) -> None:
        current = {
            **self.preferences,
            "review_llm": {"enabled": True, "api_key": "reviewer-secret", "model": "old-reviewer"},
        }
        merged = merge_preferences(
            current,
            {"review_llm": {"enabled": False, "api_key": "", "model": "new-reviewer"}},
        )
        safe = sanitize_preferences(merged)

        self.assertEqual(merged["review_llm"]["api_key"], "reviewer-secret")
        self.assertNotIn("api_key", safe["review_llm"])
        self.assertTrue(safe["review_llm"]["api_key_configured"])
        self.assertEqual(safe["review_llm"]["api_key_masked"], "review••••••••cret")
        self.assertFalse(safe["review_llm"]["enabled"])

    def test_api_key_mask_keeps_only_short_prefix_and_suffix(self) -> None:
        self.assertEqual(mask_api_key("sk-example-secret-1234"), "sk-exa••••••••1234")
        self.assertEqual(mask_api_key("12345678"), "12••••78")
        self.assertEqual(mask_api_key("tiny"), "••••")

    def test_writer_connection_test_reuses_saved_secret(self) -> None:
        config = build_test_llm_config(
            self.preferences,
            target="writer",
            base_url="https://new-writer.example/v1",
            model="new-writer-model",
        )

        self.assertEqual(config.base_url, "https://new-writer.example/v1")
        self.assertEqual(config.model, "new-writer-model")
        self.assertEqual(config.api_key, "writer-secret")
        self.assertEqual(config.timeout_seconds, 30.0)
        self.assertEqual(config.total_timeout_seconds, 37.5)
        self.assertEqual(config.max_retries, 0)
        self.assertEqual(config.context_window_tokens, DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS)

    def test_reviewer_connection_test_does_not_fall_back_to_writer_secret(self) -> None:
        with self.assertRaisesRegex(ValueError, "API Key"):
            build_test_llm_config(
                self.preferences,
                target="reviewer",
                base_url="https://reviewer.example/v1",
                model="reviewer-model",
            )

    def test_connection_test_rejects_invalid_base_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP"):
            build_test_llm_config(
                self.preferences,
                target="writer",
                base_url="writer.example/v1",
                model="writer-model",
            )


class TavilyPreferenceTests(unittest.TestCase):
    def test_legacy_key_remains_enabled_without_explicit_switch(self) -> None:
        config = build_tavily_config({"web_search": {"tavily_api_key": "legacy-secret"}})

        self.assertIsNotNone(config)
        self.assertEqual(config.api_key, "legacy-secret")

    def test_explicit_disabled_switch_blocks_search_with_saved_key(self) -> None:
        config = build_tavily_config(
            {"web_search": {"enabled": False, "tavily_api_key": "saved-secret"}}
        )

        self.assertIsNone(config)

    def test_enabled_switch_uses_saved_key(self) -> None:
        config = build_tavily_config(
            {"web_search": {"enabled": True, "tavily_api_key": "saved-secret"}}
        )

        self.assertIsNotNone(config)
        self.assertEqual(config.api_key, "saved-secret")


class EmbeddingPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preferences = {
            "embedding": {
                "enabled": True,
                "endpoint": "https://workspace.example/api/v1",
                "api_key": "embedding-secret",
                "model": "qwen3.7-text-embedding",
            }
        }

    def test_disabled_switch_blocks_embedding_calls(self) -> None:
        preferences = {
            "embedding": {
                **self.preferences["embedding"],
                "enabled": False,
            }
        }

        self.assertIsNone(build_embedding_config(preferences))

    def test_enabled_config_normalizes_workspace_api_v1(self) -> None:
        config = build_embedding_config(self.preferences)

        self.assertIsNotNone(config)
        self.assertEqual(
            config.endpoint,
            "https://workspace.example/api/v1/services/embeddings/"
            "text-embedding/text-embedding",
        )
        self.assertEqual(config.api_key, "embedding-secret")
        self.assertEqual(config.model, "qwen3.7-text-embedding")

    def test_embedding_secret_is_preserved_and_sanitized(self) -> None:
        merged = merge_preferences(
            self.preferences,
            {
                "embedding": {
                    "enabled": False,
                    "api_key": "",
                    "model": "new-embedding-model",
                }
            },
        )
        safe = sanitize_preferences(merged)

        self.assertEqual(merged["embedding"]["api_key"], "embedding-secret")
        self.assertNotIn("api_key", safe["embedding"])
        self.assertTrue(safe["embedding"]["api_key_configured"])
        self.assertEqual(
            safe["embedding"]["api_key_masked"],
            "embedd••••••••cret",
        )
        self.assertFalse(safe["embedding"]["enabled"])

    def test_connection_test_reuses_saved_secret(self) -> None:
        config = build_test_embedding_config(
            self.preferences,
            endpoint="https://new-workspace.example/api/v1",
            model="qwen3.7-text-embedding",
        )

        self.assertEqual(config.api_key, "embedding-secret")
        self.assertEqual(config.timeout_seconds, 30.0)

    def test_endpoint_rejects_non_http_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTP"):
            build_test_embedding_config(
                {},
                endpoint="workspace.example/api/v1",
                model="qwen3.7-text-embedding",
                api_key="secret",
            )

    def test_full_service_endpoint_is_not_duplicated(self) -> None:
        endpoint = (
            "https://workspace.example/api/v1/services/embeddings/"
            "text-embedding/text-embedding"
        )
        self.assertEqual(normalize_embedding_endpoint(endpoint), endpoint)

    def test_compatible_base_url_appends_embeddings_path(self) -> None:
        self.assertEqual(
            normalize_embedding_endpoint(
                "https://workspace.example/compatible-mode/v1"
            ),
            "https://workspace.example/compatible-mode/v1/embeddings",
        )


if __name__ == "__main__":
    unittest.main()
