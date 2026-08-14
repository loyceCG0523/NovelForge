import unittest

from app.services.sample_chapter_splitter import (
    locate_quote_in_units,
    split_sample_chapters,
)


class SampleChapterSplitterTests(unittest.TestCase):
    def test_splits_chinese_markdown_and_special_chapter_headings(self):
        text = (
            "书名与简介\n这是一段前置说明。\n\n"
            "# 第一章 初见\n第一章正文。\n人物开始对话。\n\n"
            "第 2 章：误会\n第二章正文。\n\n"
            "番外：雨夜\n番外正文。"
        )
        units = split_sample_chapters(text)
        self.assertEqual(
            [unit.title for unit in units],
            ["前置内容", "第一章 初见", "第 2 章：误会", "番外：雨夜"],
        )
        self.assertEqual(units[1].unit_type, "chapter")
        self.assertTrue(units[1].content.startswith("# 第一章 初见"))
        self.assertIn("人物开始对话。", units[1].content)

    def test_does_not_split_inline_chapter_reference(self):
        text = "他说第一章写得很好，但这只是正文里的普通句子。" * 500
        units = split_sample_chapters(text, fallback_chars=4000)
        self.assertTrue(units)
        self.assertTrue(all(unit.unit_type == "fallback" for unit in units))
        self.assertTrue(all(unit.title.startswith("未识别章节") for unit in units))

    def test_quote_relocation_prefers_nearest_original_position(self):
        text = "第一章\n重复原句。\n其他内容。\n第二章\n重复原句。\n结尾。"
        units = split_sample_chapters(text)
        located = locate_quote_in_units(
            units,
            "重复原句。",
            approximate_offset=text.rfind("重复原句。"),
        )
        self.assertIsNotNone(located)
        unit_index, start, end = located
        self.assertEqual(unit_index, 1)
        self.assertEqual(units[unit_index].content[start:end], "重复原句。")

    def test_empty_text_has_no_units(self):
        self.assertEqual(split_sample_chapters(" \n\n "), [])


if __name__ == "__main__":
    unittest.main()
