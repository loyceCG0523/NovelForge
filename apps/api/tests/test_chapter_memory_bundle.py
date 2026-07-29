"""章节记忆与时间线合并抽取回归测试。"""

import unittest
from unittest.mock import MagicMock, patch

from app.models.chapter import Chapter
from app.models.novel import Novel
from app.services.chapter_memory_bundle import (
    build_chapter_memory_bundle_prompt,
    extract_chapter_memory_bundle,
)
from app.services.llm_client import LLMConfig


class ChapterMemoryBundleTests(unittest.TestCase):
    def build_records(self) -> tuple[Novel, Chapter]:
        novel = Novel(title="测试作品", genre="都市", brief={"story_era": "2025年"})
        chapter = Chapter(
            chapter_index=2,
            title="雨夜",
            summary="两人在雨夜重逢。",
            content="晚上八点，林予安在地铁站遇见沈栀夏。",
        )
        return novel, chapter

    def test_prompt_requests_memory_and_timeline_from_one_chapter_payload(self) -> None:
        novel, chapter = self.build_records()
        messages = build_chapter_memory_bundle_prompt(novel, chapter, [])
        prompt = messages[1]["content"]

        self.assertIn('"memories"', prompt)
        self.assertIn('"timeline_entries"', prompt)
        self.assertEqual(prompt.count(chapter.content), 1)

    @patch("app.services.chapter_memory_bundle.get_timeline_context", return_value=[])
    @patch("app.services.chapter_memory_bundle.LLMClient.complete_json")
    def test_one_model_call_produces_both_record_types(
        self,
        complete_json,
        _timeline_context,
    ) -> None:
        novel, chapter = self.build_records()
        complete_json.return_value = (
            "{}",
            {
                "memories": [
                    {
                        "memory_type": "location",
                        "entity_name": "地铁站",
                        "payload": {"summary": "两人在这里重逢"},
                    }
                ],
                "timeline_entries": [
                    {
                        "story_day": 2,
                        "start_time": "晚上八点",
                        "time_expression": "晚上八点",
                        "location": "地铁站",
                        "summary": "林予安遇见沈栀夏",
                        "participants": ["林予安", "沈栀夏"],
                        "certainty": "confirmed",
                    }
                ],
            },
        )

        memories, timeline = extract_chapter_memory_bundle(
            db=MagicMock(),
            novel=novel,
            chapter=chapter,
            llm_config=LLMConfig(
                base_url="https://example.test/v1",
                api_key="secret",
                model="test-model",
            ),
        )

        complete_json.assert_called_once()
        self.assertEqual(len(memories), 1)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(memories[0]["entity_name"], "地铁站")
        self.assertEqual(timeline[0]["location"], "地铁站")


if __name__ == "__main__":
    unittest.main()
