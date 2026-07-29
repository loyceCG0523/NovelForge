import unittest
from uuid import uuid4

from app.models.chapter import Chapter
from app.services.chapter_fact_delta import (
    FACT_DELTA_SOURCE,
    build_chapter_fact_delta,
)
from app.services.memory_extractor import AUTO_MEMORY_SOURCES


class ChapterFactDeltaTests(unittest.TestCase):
    def test_fact_delta_uses_generation_progress_without_another_llm_call(self) -> None:
        chapter = Chapter(
            id=uuid4(),
            novel_id=uuid4(),
            chapter_index=7,
            title="转折点",
            summary="旧摘要",
            content="章节正文",
        )

        delta = build_chapter_fact_delta(
            chapter,
            {
                "actual_summary": "主角拿到了仓库钥匙。",
                "completed_beats": ["拿到钥匙"],
                "unresolved_beats": ["钥匙来自谁"],
                "actual_ending_state": {"location": "仓库门口"},
            },
        )

        self.assertEqual(delta["payload"]["source"], FACT_DELTA_SOURCE)
        self.assertTrue(delta["payload"]["lightweight"])
        self.assertEqual(delta["payload"]["summary"], "主角拿到了仓库钥匙。")
        self.assertEqual(delta["payload"]["unresolved_beats"], ["钥匙来自谁"])

    def test_full_memory_merge_replaces_lightweight_delta(self) -> None:
        self.assertIn(FACT_DELTA_SOURCE, AUTO_MEMORY_SOURCES)


if __name__ == "__main__":
    unittest.main()
