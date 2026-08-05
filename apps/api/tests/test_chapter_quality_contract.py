"""人工标注问题沉淀到生成质量契约的回归测试。"""

import unittest

from app.services.chapter_quality_contract import (
    CHAPTER_REVIEW_ISSUE_TYPES,
    build_generation_quality_contract,
    build_review_quality_contract,
)


class ChapterQualityContractTests(unittest.TestCase):
    def test_generation_contract_covers_annotated_failure_modes(self) -> None:
        contract = build_generation_quality_contract(1)
        text = str(contract)

        self.assertIn("时间、地点和动作切换", text)
        self.assertIn("身份、权力关系", text)
        self.assertIn("道具、衣物、食物、门锁", text)
        self.assertIn("消费、卫生、职业习惯", text)
        self.assertIn("不要连续铺陈天气、灯光、家具、服装、食物、品牌和生活动作", text)
        self.assertIn("手续、费用、登记、采购、通勤、清单和规则不得逐项展开", text)
        self.assertIn("环境描写是否过量", str(build_review_quality_contract(1)))
        self.assertIn("算法、变量或数学题", text)
        self.assertIn("固定口癖、感情比喻或连续笑点", text)
        self.assertIn("至少形成4个分散的因果型喜剧节拍", text)
        self.assertIn("语气词、停顿、改口、打断", text)
        self.assertIn("前10%必须发生具体扰动", text)
        self.assertIn("具体的未完成动作、新信息、危险、选择或关系变化", text)
        self.assertIn("即时目标、阻力、策略、反制和局部变化", text)
        self.assertIn("答非所问、错位联想、一本正经跑偏", text)
        self.assertIn("不同说话节奏、回避习惯与情绪泄漏方式", text)
        self.assertIn("storytelling_craft", contract)
        self.assertIn("下一章第一拍", text)
        self.assertTrue(contract["first_chapter_extra"])

    def test_later_chapter_does_not_receive_first_chapter_only_rules(self) -> None:
        contract = build_generation_quality_contract(3)
        self.assertEqual(contract["first_chapter_extra"], [])

    def test_reviewer_contract_has_severity_rubric_and_expanded_types(self) -> None:
        contract = build_review_quality_contract(1)

        self.assertIn("high", contract["severity_rubric"])
        self.assertIn("ending_hook", CHAPTER_REVIEW_ISSUE_TYPES)
        self.assertIn("detail_relevance", CHAPTER_REVIEW_ISSUE_TYPES)
        self.assertIn("viewpoint_knowledge", CHAPTER_REVIEW_ISSUE_TYPES)
        self.assertIn("supporting_element_dominance", CHAPTER_REVIEW_ISSUE_TYPES)
        self.assertIn("genre_delivery", CHAPTER_REVIEW_ISSUE_TYPES)


if __name__ == "__main__":
    unittest.main()
