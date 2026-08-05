import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.event_research import (
    _build_research_plan_prompt,
    collect_event_research,
    plan_event_research_queries,
)
from app.services.llm_client import LLMConfig


class EventResearchProviderTests(unittest.TestCase):
    def build_novel(self):
        return SimpleNamespace(
            id=uuid4(),
            title="测试小说",
            genre="喜剧",
            premise="失业青年误住进陌生女孩家",
            brief={},
        )

    def test_plot_craft_and_comedy_language_queries_cannot_be_skipped(self) -> None:
        novel = self.build_novel()
        event_plan = {
            "event_title": "雨夜误住",
            "core_conflict": "男主无处可去，女主不愿让陌生人留宿",
            "chapter_plans": [{"core_event": "双方谈判一晚试住条件"}],
        }
        llm_config = LLMConfig(
            base_url="https://reviewer.example/v1",
            api_key="secret",
            model="reviewer-model",
        )

        with patch(
            "app.services.event_research.LLMClient.complete_json",
            return_value=('{}', {"needs_research": False, "queries": []}),
        ):
            queries = plan_event_research_queries(novel, event_plan, llm_config)

        self.assertEqual(len(queries), 2)
        self.assertTrue(any("小说情节怎么写" in query for query in queries))
        self.assertTrue(any("搞笑对话" in query and "真实口语" in query for query in queries))
        self.assertTrue(all("雨夜误住" in query for query in queries))

    def test_required_creative_queries_survive_without_planning_model(self) -> None:
        queries = plan_event_research_queries(
            self.build_novel(),
            {"event_title": "合租第一天", "core_conflict": "生活习惯冲突"},
            None,
        )

        self.assertEqual(len(queries), 2)

    def test_planner_only_decides_optional_factual_query(self) -> None:
        messages = _build_research_plan_prompt(
            self.build_novel(),
            {"event_title": "雨夜误住", "core_conflict": "临时留宿"},
        )
        prompt = "\n".join(message["content"] for message in messages)

        self.assertIn("系统会固定检索两项", prompt)
        self.assertIn("最多给出 1 条", prompt)
        self.assertIn("搞笑口语", prompt)

    def test_official_deepseek_flash_uses_native_search_instead_of_tavily(self) -> None:
        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        db.refresh.side_effect = lambda source: setattr(source, "id", uuid4())
        novel = self.build_novel()
        owner = SimpleNamespace(
            preferences={
                "web_search": {
                    "enabled": False,
                    "tavily_api_key": "configured-but-unused",
                }
            }
        )
        planning_config = LLMConfig(
            base_url="https://reviewer.example/v1",
            api_key="reviewer-secret",
            model="reviewer-model",
        )
        writer_config = LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="deepseek-secret",
            model="deepseek-v4-flash",
            reasoning_effort="low",
        )
        native_result = {
            "title": "上海裁员补偿流程",
            "url": "https://example.com/layoff",
            "domain": "example.com",
            "snippet": "上海裁员补偿流程、条件与计算标准",
            "score": 0.95,
            "published_at": "2026-01-01",
            "payload": {"native_tool": "web_search"},
        }

        with patch(
            "app.services.event_research.plan_event_research_queries",
            return_value=["上海裁员补偿流程"],
        ), patch(
            "app.services.event_research.LLMClient.search_web",
            return_value=[native_result],
        ) as native_search, patch(
            "app.services.event_research.search_tavily"
        ) as tavily_search:
            result = collect_event_research(
                db,
                novel,
                owner,
                {},
                planning_config,
                search_llm_config=writer_config,
            )

        native_search.assert_called_once_with("上海裁员补偿流程", max_results=3)
        tavily_search.assert_not_called()
        self.assertEqual(result["provider"], "deepseek_web_search")
        saved_sources = db.add_all.call_args.args[0]
        self.assertEqual(saved_sources[0].provider, "deepseek_web_search")
        self.assertEqual(
            saved_sources[0].payload["search_provider"],
            "deepseek_web_search",
        )


if __name__ == "__main__":
    unittest.main()
