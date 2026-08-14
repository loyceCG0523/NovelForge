"""作品导出文件名回归测试。"""

import unittest
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.novels import (
    _build_export_metadata,
    _build_markdown_export,
    _build_txt_export,
    _chapter_display_title,
    _safe_export_filename,
)
from app.main import app
from app.models.chapter import Chapter


class NovelExportFilenameTests(unittest.TestCase):
    def test_markdown_filename_contains_date_hour_and_minute(self) -> None:
        filename = _safe_export_filename(
            "测试小说",
            "md",
            exported_at=datetime(2026, 7, 18, 19, 45),
        )

        self.assertEqual(filename, "测试小说_20260718_1945.md")

    def test_txt_filename_keeps_timestamp_after_sanitized_title(self) -> None:
        filename = _safe_export_filename(
            '测试:小说/第一部',
            "txt",
            exported_at=datetime(2026, 7, 18, 9, 5),
        )

        self.assertEqual(filename, "测试_小说_第一部_20260718_0905.txt")

    def test_browser_can_read_content_disposition_across_origins(self) -> None:
        response = TestClient(app).get(
            "/",
            headers={"Origin": "http://127.0.0.1:3000"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "content-disposition",
            response.headers.get("access-control-expose-headers", "").lower(),
        )


class ChapterDisplayTitleTests(unittest.TestCase):
    def test_compact_chapter_prefix_is_not_repeated(self) -> None:
        chapter = Chapter(chapter_index=1, title="第1章：被裁当天，雨一直下")

        self.assertEqual(
            _chapter_display_title(chapter),
            "第 1 章 被裁当天，雨一直下",
        )

    def test_spaced_chapter_prefix_is_not_repeated(self) -> None:
        chapter = Chapter(chapter_index=2, title="第 2 章 稀里糊涂的合租协议")

        self.assertEqual(
            _chapter_display_title(chapter),
            "第 2 章 稀里糊涂的合租协议",
        )

    def test_plain_title_receives_one_canonical_prefix(self) -> None:
        chapter = Chapter(chapter_index=3, title="第一顿家常饭")

        self.assertEqual(_chapter_display_title(chapter), "第 3 章 第一顿家常饭")


class NovelExportMetadataTests(unittest.TestCase):
    def build_novel(self):
        return SimpleNamespace(
            title="测试小说",
            genre="都市",
            premise="两个陌生人在雨夜相遇。",
            target_words=300000,
            brief={
                "chapter_word_min": 2200,
                "chapter_word_max": 3200,
                "story_era": "2026 年",
                "story_location": "上海",
            },
        )

    def test_actual_chapter_models_override_current_account_models(self) -> None:
        novel = self.build_novel()
        chapter = Chapter(
            chapter_index=1,
            title="雨夜",
            content="正文。",
            context_snapshot={
                "word_guard": {"model": "kimi-k2.6"},
                "chapter_review_cycle": {
                    "writer_model": "kimi-k2.6",
                    "reviewer_model": "deepseek-v4-pro",
                },
            },
        )
        preferences = {
            "llm": {
                "model": "new-writer",
                "api_key": "writer-secret-must-not-export",
                "base_url": "https://private-writer.example/v1",
            },
            "review_llm": {
                "enabled": True,
                "model": "new-reviewer",
                "api_key": "reviewer-secret-must-not-export",
                "base_url": "https://private-reviewer.example/v1",
            },
            "web_search": {
                "enabled": True,
                "tavily_api_key": "tavily-secret-must-not-export",
            },
        }

        metadata = _build_export_metadata(
            novel,
            [chapter],
            preferences,
            exported_at=datetime(2026, 7, 19, 3, 10),
        )
        markdown = _build_markdown_export(novel, [chapter], metadata)

        self.assertIn("- 正文生成模型：kimi-k2.6", markdown)
        self.assertIn("- 质量审校模型：deepseek-v4-pro", markdown)
        self.assertIn("- 审校配置：独立审校模型 API", markdown)
        self.assertIn("- Tavily 网络检索：已开启", markdown)
        self.assertIn("- 单章目标字数：2200-3200 字", markdown)
        self.assertIn("- 导出时间：2026-07-19 03:10", markdown)
        self.assertNotIn("secret", markdown)
        self.assertNotIn("private-writer.example", markdown)

    def test_txt_export_places_generation_information_before_novel_content(self) -> None:
        novel = self.build_novel()
        chapter = Chapter(chapter_index=1, title="雨夜", content="第一章正文。")
        preferences = {
            "llm": {"model": "kimi-k2.6"},
            "review_llm": {"enabled": False},
            "web_search": {"enabled": False},
        }

        metadata = _build_export_metadata(
            novel,
            [chapter],
            preferences,
            exported_at=datetime(2026, 7, 19, 4, 5),
        )
        content = _build_txt_export(novel, [chapter], metadata)

        self.assertTrue(content.startswith("测试小说\n\n【生成信息】"))
        self.assertIn("正文生成模型：kimi-k2.6", content)
        self.assertIn("质量审校模型：kimi-k2.6", content)
        self.assertIn("审校配置：与正文模型共用 API", content)
        self.assertLess(content.index("【生成信息】"), content.index("第一章正文。"))


if __name__ == "__main__":
    unittest.main()
