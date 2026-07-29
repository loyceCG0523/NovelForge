import unittest
from unittest.mock import Mock, patch

from app.services.embedding_client import EmbeddingClient, EmbeddingConfig


class EmbeddingClientTests(unittest.TestCase):
    def setUp(self):
        self.config = EmbeddingConfig(
            endpoint=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/"
                "services/embeddings/text-embedding/text-embedding"
            ),
            api_key="test-key",
            model="qwen3.7-text-embedding",
            dimensions=3,
        )

    @patch("app.services.embedding_client.httpx.post")
    def test_uses_dashscope_native_document_protocol(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "output": {
                "embeddings": [
                    {"text_index": 1, "embedding": [4.0, 5.0, 6.0]},
                    {"text_index": 0, "embedding": [1.0, 2.0, 3.0]},
                ]
            }
        }
        post.return_value = response

        vectors = EmbeddingClient(self.config).embed(
            ["第一段", "第二段"],
            text_type="document",
        )

        self.assertEqual(vectors, [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen3.7-text-embedding")
        self.assertEqual(payload["input"], {"texts": ["第一段", "第二段"]})
        self.assertEqual(
            payload["parameters"],
            {"dimension": 3, "output_type": "dense", "text_type": "document"},
        )

    @patch("app.services.embedding_client.httpx.post")
    def test_query_instruct_is_sent(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "output": {"embeddings": [{"text_index": 0, "embedding": [1.0, 2.0, 3.0]}]}
        }
        post.return_value = response

        EmbeddingClient(self.config).embed(
            ["雨夜追逐的紧张场景"],
            text_type="query",
            instruct="Retrieve fiction passages.",
        )

        parameters = post.call_args.kwargs["json"]["parameters"]
        self.assertEqual(parameters["text_type"], "query")
        self.assertEqual(parameters["instruct"], "Retrieve fiction passages.")

    def test_rejects_more_than_twenty_rows(self):
        with self.assertRaisesRegex(ValueError, "20"):
            EmbeddingClient(self.config).embed(["片段"] * 21)

    @patch("app.services.embedding_client.httpx.post")
    def test_openai_compatible_base_uses_embeddings_protocol(self, post):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [
                {"index": 0, "embedding": [1.0, 2.0, 3.0]},
            ]
        }
        post.return_value = response
        config = EmbeddingConfig(
            endpoint=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1/embeddings"
            ),
            api_key="test-key",
            model="qwen3.7-text-embedding",
            dimensions=3,
        )

        vectors = EmbeddingClient(config).embed(
            ["雨夜"],
            text_type="query",
            instruct="This is only available in native mode.",
        )

        self.assertEqual(vectors, [[1.0, 2.0, 3.0]])
        self.assertEqual(
            post.call_args.args[0],
            config.endpoint,
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "model": "qwen3.7-text-embedding",
                "input": ["雨夜"],
                "dimensions": 3,
                "encoding_format": "float",
            },
        )


if __name__ == "__main__":
    unittest.main()
