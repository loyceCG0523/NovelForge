import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.api.research import search_research_sources
from app.schemas.research import ResearchSearchRequest
from app.services.llm_client import LLMConfig


class ResearchSearchProviderTests(unittest.TestCase):
    def test_manual_search_prefers_official_deepseek_web_search(self) -> None:
        novel = SimpleNamespace(id=uuid4())
        user = SimpleNamespace(preferences={})
        db = MagicMock()
        db.refresh.side_effect = lambda source: setattr(source, "id", uuid4())
        llm_config = LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="secret",
            model="deepseek-v4-flash",
            reasoning_effort="low",
        )
        result = {
            "title": "合租冲突写法",
            "url": "https://example.com/story",
            "domain": "example.com",
            "snippet": "通过目标冲突与误解升级场景。",
            "score": 0.9,
            "published_at": "2026-01-01",
            "payload": {"native_tool": "web_search"},
        }

        with patch(
            "app.api.research.build_llm_config",
            return_value=llm_config,
        ), patch(
            "app.api.research.LLMClient.search_web",
            return_value=[result],
        ) as native_search, patch(
            "app.api.research.search_tavily",
        ) as tavily_search:
            sources = search_research_sources(
                ResearchSearchRequest(query="合租冲突写法"),
                novel=novel,
                current_user=user,
                db=db,
            )

        native_search.assert_called_once_with("合租冲突写法", max_results=5)
        tavily_search.assert_not_called()
        self.assertEqual(sources[0].provider, "deepseek_web_search")
        self.assertEqual(
            sources[0].payload["search_provider"],
            "deepseek_web_search",
        )


if __name__ == "__main__":
    unittest.main()
