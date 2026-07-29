"""喜剧基调与事件长度约束回归测试。"""

import unittest

from app.services.tone_pacing_contract import (
    build_tone_pacing_contract,
    is_comedy_focused,
    resolve_event_chapter_count,
)


class TonePacingContractTests(unittest.TestCase):
    def test_comedy_brief_gets_dense_comedy_and_anti_ledger_rules(self) -> None:
        brief = {
            "selling_points": "高密度笑点和同居互动",
            "style_reference": "都市轻喜剧",
        }

        contract = build_tone_pacing_contract("都市、恋爱", brief)
        text = str(contract)

        self.assertTrue(is_comedy_focused("都市、恋爱", brief))
        self.assertEqual(contract["mode"], "high_density_light_comedy")
        self.assertIn("2—3个", text)
        self.assertIn("手续、准备、训练、调查、赶路、采购和规则确认", text)
        self.assertIn("连续低落", text)

    def test_non_comedy_brief_does_not_receive_comedy_quota(self) -> None:
        self.assertEqual(
            build_tone_pacing_contract("现实悬疑", {"style_reference": "冷峻克制"}),
            {},
        )

    def test_event_chapter_count_defaults_to_six_and_accepts_four(self) -> None:
        self.assertEqual(resolve_event_chapter_count({}), 6)
        self.assertEqual(resolve_event_chapter_count({"event_chapter_count": 5}), 5)
        self.assertEqual(resolve_event_chapter_count({"event_chapter_count": 2}), 4)


if __name__ == "__main__":
    unittest.main()
