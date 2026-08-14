"""确定性段落排版的回归测试。"""

import re
import unittest
from types import SimpleNamespace

from app.services.paragraph_length_checker import check_paragraph_lengths
from app.services.paragraph_formatter import (
    HARD_MAX_PARAGRAPH_CHARS,
    PREFERRED_PARAGRAPH_CHARS,
    format_chapter_paragraphs,
    meaningful_length,
)


class ParagraphFormatterTests(unittest.TestCase):
    def test_long_paragraph_is_split_without_changing_text(self) -> None:
        content = (
            "林予安把杯子放进水槽，拧开水龙头冲了一下，牛奶的白色在水流里打了个旋，消失了。"
            "擦干手的时候，他经过冰箱，发现规则表上又多了一行很小的字。"
        )

        formatted = format_chapter_paragraphs(content)
        paragraphs = formatted.split("\n\n")

        self.assertGreater(len(paragraphs), 1)
        self.assertTrue(
            all(meaningful_length(item) <= HARD_MAX_PARAGRAPH_CHARS for item in paragraphs)
        )
        self.assertTrue(all(item.endswith(("。", "！", "？")) for item in paragraphs))
        self.assertEqual(re.sub(r"\s", "", content), re.sub(r"\s", "", formatted))

    def test_unpunctuated_text_uses_strict_length_fallback(self) -> None:
        content = "甲" * 241

        formatted = format_chapter_paragraphs(content)
        lengths = [meaningful_length(item) for item in formatted.split("\n\n")]

        self.assertEqual(lengths, [HARD_MAX_PARAGRAPH_CHARS, HARD_MAX_PARAGRAPH_CHARS, 1])
        self.assertEqual(content, formatted.replace("\n", ""))

    def test_normal_sentence_is_not_split_at_comma(self) -> None:
        content = "他把门禁卡放回桌上，又核对了离职清单，确认没有遗漏后才转身离开。"

        formatted = format_chapter_paragraphs(content)

        self.assertEqual(formatted, content)
        self.assertLessEqual(meaningful_length(formatted), PREFERRED_PARAGRAPH_CHARS)

    def test_old_comma_break_is_joined_before_sentence_layout(self) -> None:
        content = "HR的语气像在念产品说明，\n门禁权限今天下午六点失效，\n工牌交到前台就行。"

        formatted = format_chapter_paragraphs(content)

        self.assertEqual(formatted, "HR的语气像在念产品说明，门禁权限今天下午六点失效，工牌交到前台就行。")

    def test_existing_paragraphs_keep_their_order(self) -> None:
        content = "第一段很短。\n第二段也很短。\n\n第三段仍然很短。"

        formatted = format_chapter_paragraphs(content)

        self.assertEqual(formatted, "第一段很短。\n\n第二段也很短。\n\n第三段仍然很短。")

    def test_continuous_dialogue_is_not_split_before_closing_quote(self) -> None:
        content = (
            "沈栀夏站在灯下，嗯了一声，像是把这件事正式记下了。"
            "然后她抬手指了指客厅角落那张折叠床：“你今晚睡那边。\n\n"
            "被子是干净的，自己铺。”"
        )

        formatted = format_chapter_paragraphs(content)

        self.assertEqual(
            formatted,
            "沈栀夏站在灯下，嗯了一声，像是把这件事正式记下了。"
            "然后她抬手指了指客厅角落那张折叠床：“你今晚睡那边。"
            "被子是干净的，自己铺。”",
        )
        self.assertNotIn("\n\n", formatted)
        self.assertLessEqual(meaningful_length(formatted), HARD_MAX_PARAGRAPH_CHARS)

    def test_closed_dialogue_and_next_action_remain_separate_paragraphs(self) -> None:
        content = "她说：“晚安。”\n\n他没有回答。"

        formatted = format_chapter_paragraphs(content)

        self.assertEqual(formatted, content)


class ParagraphLengthPolicyTests(unittest.TestCase):
    def test_chapter_long_paragraph_quotas_are_enforced(self) -> None:
        lengths = [61, 70, 81, 90]
        chapter = SimpleNamespace(content="\n\n".join("甲" * length for length in lengths))

        records = check_paragraph_lengths(chapter)

        self.assertEqual([item["payload"]["paragraph_index"] for item in records], [4])
        self.assertEqual(records[0]["payload"]["violation_reason"], "very_long_quota")

    def test_hard_max_is_always_reported(self) -> None:
        chapter = SimpleNamespace(content="甲" * 121)

        records = check_paragraph_lengths(chapter)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["severity"], "high")
        self.assertEqual(records[0]["payload"]["hard_limit"], 120)


if __name__ == "__main__":
    unittest.main()
