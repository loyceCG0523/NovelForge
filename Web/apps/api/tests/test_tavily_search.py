import unittest
from unittest.mock import patch

from app.services.tavily_search import (
    TavilyConfig,
    query_overlap_score,
    search_tavily,
)


class _FakeResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, response_data, captured, **_kwargs):
        self.response_data = response_data
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, *, headers, json):
        self.captured.update({"url": url, "headers": headers, "payload": json})
        return _FakeResponse(self.response_data)


class TavilySearchAccuracyTests(unittest.TestCase):
    def test_query_overlap_prefers_sources_covering_specific_facts(self) -> None:
        query = "上海互联网公司裁员经济补偿流程"

        relevant = query_overlap_score(
            query,
            "上海互联网公司裁员补偿流程",
            "介绍劳动合同解除、书面通知和经济补偿。",
        )
        irrelevant = query_overlap_score(
            query,
            "上海周末旅游攻略",
            "介绍餐厅、公园和城市夜景。",
        )

        self.assertGreater(relevant, irrelevant)
        self.assertGreaterEqual(relevant, 0.5)

    def test_advanced_recent_search_uses_precision_parameters_and_local_rerank(self) -> None:
        captured = {}
        diagnostics = {}
        response_data = {
            "request_id": "request-1",
            "results": [
                {
                    "title": "热门手机推荐",
                    "url": "https://noise.example/phones",
                    "content": "讨论手机参数和购买价格，与查询主题无关。",
                    "score": 0.99,
                },
                {
                    "title": "本月上海裁员经济补偿办理规则",
                    "url": "https://good.example/labor?utm_source=test",
                    "content": "整理上海互联网公司裁员经济补偿流程与办理要求。",
                    "score": 0.82,
                    "published_date": "2026-07-20",
                },
                {
                    "title": "重复链接",
                    "url": "https://good.example/labor?from=duplicate",
                    "content": "裁员经济补偿流程重复结果。",
                    "score": 0.95,
                },
            ],
        }

        def client_factory(**kwargs):
            return _FakeClient(response_data, captured, **kwargs)

        with patch("app.services.tavily_search.httpx.Client", side_effect=client_factory):
            results = search_tavily(
                TavilyConfig(api_key="secret"),
                "2026年上海互联网公司裁员经济补偿办理流程",
                max_results=3,
                search_depth="advanced",
                start_date="2026-06-22",
                time_range="month",
                country="china",
                min_score=0.55,
                min_query_overlap=0.08,
                allowed_languages={"zh-Hans"},
                required_any_terms=("裁员", "经济补偿"),
                include_domains=("good.example",),
                diagnostics=diagnostics,
            )

        payload = captured["payload"]
        self.assertEqual(payload["search_depth"], "advanced")
        self.assertEqual(payload["chunks_per_source"], 2)
        self.assertEqual(payload["time_range"], "month")
        self.assertEqual(payload["country"], "china")
        self.assertEqual(payload["max_results"], 6)
        self.assertEqual(payload["include_domains"], ["good.example"])
        self.assertNotIn("start_date", payload)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["domain"], "good.example")
        self.assertEqual(results[0]["payload"]["tavily_request_id"], "request-1")
        self.assertGreater(results[0]["payload"]["query_overlap_score"], 0)
        self.assertEqual(diagnostics["raw_result_count"], 3)
        self.assertEqual(diagnostics["returned_count"], 1)
        self.assertEqual(diagnostics["source_policy_rejected_count"], 1)

    def test_high_provider_score_cannot_bypass_query_relevance(self) -> None:
        captured = {}
        response_data = {
            "results": [
                {
                    "title": "高分但无关的旅游文章",
                    "url": "https://example.com/travel",
                    "content": "介绍海边、酒店、美食和交通。",
                    "score": 0.99,
                }
            ]
        }

        def client_factory(**kwargs):
            return _FakeClient(response_data, captured, **kwargs)

        with patch("app.services.tavily_search.httpx.Client", side_effect=client_factory):
            results = search_tavily(
                TavilyConfig(api_key="secret"),
                "上海互联网公司裁员经济补偿流程",
                max_results=3,
            )

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
