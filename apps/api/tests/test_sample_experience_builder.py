import unittest
from unittest.mock import patch

from app.services.sample_experience_builder import (
    build_experience_markdown,
    build_sample_experience_document,
    split_sample_for_parallel_analysis,
)
from app.services.llm_client import LLMConfig
from app.services.sample_passage_indexer import (
    build_sample_experience_candidates,
)


class SampleExperienceBuilderTests(unittest.TestCase):
    def test_large_sample_is_split_into_at_most_ten_parts(self):
        chunks = [f"第 {index} 块\n" + ("内容。" * 600) for index in range(30)]

        parts = split_sample_for_parallel_analysis(
            chunks,
            max_parts=10,
            target_part_chars=1000,
        )

        self.assertEqual(len(parts), 10)
        self.assertTrue(all(part.strip() for part in parts))
        self.assertEqual(
            sum(part.count("内容。") for part in parts),
            30 * 600,
        )

    def test_experience_cards_are_rendered_and_indexable(self):
        document = {
            "schema_version": "sample_experience.v1",
            "sample_title": "测试样本",
            "source_genre": "轻喜剧",
            "model": "review-model",
            "part_count": 2,
            "plot_principles": ["让人物主动选择并承担代价"],
            "expression_principles": ["用答非所问表现回避"],
            "anti_patterns": ["不要只复述情绪"],
            "plot_experiences": [
                {
                    "part_index": 1,
                    "title": "善意隐瞒改变冲突",
                    "setup": "朋友准备离开",
                    "trigger": "旧秘密被发现",
                    "character_desire": "保护对方",
                    "conflict_and_escalation": "解释会暴露更大秘密",
                    "character_choice": "选择承认一半事实",
                    "turn_or_reframe": "争执变成信任测试",
                    "payoff": "关系发生真实变化",
                    "consequence": "下一次选择更困难",
                    "why_effective": "选择同时有收益和代价",
                    "transferable_pattern": "用不完整坦白重构冲突",
                    "applicable_genres": ["现实"],
                    "applicable_scenes": ["关系转折"],
                    "quality_score": 94,
                }
            ],
            "expression_experiences": [
                {
                    "part_index": 2,
                    "title": "用动作代替拒绝",
                    "category": "dialogue",
                    "original_excerpt": "她没回答，只把第二只杯子收回柜子。",
                    "context": "邀请被委婉拒绝",
                    "relationship": "熟人",
                    "emotion": "克制",
                    "speech_act": "拒绝",
                    "response_pattern": "对方停止追问",
                    "why_effective": "动作同时传达态度和关系距离",
                    "transferable_technique": "用改变共享物品数量表达关系退缩",
                    "usage_boundary": "不能用于需要明确答复的正式场景",
                    "applicable_scenes": ["关系降温"],
                    "quality_score": 92,
                }
            ],
        }

        markdown = build_experience_markdown(document)
        candidates = build_sample_experience_candidates(document)

        self.assertIn("优秀剧情设计经验", markdown)
        self.assertIn("精彩语句与表达经验", markdown)
        self.assertEqual(
            {item["passage_type"] for item in candidates},
            {"plot_experience", "expression_experience"},
        )
        self.assertTrue(
            all(
                item["metadata_payload"]["experience"]
                for item in candidates
            )
        )

    @patch(
        "app.services.sample_experience_builder.iter_text_object_chunks"
    )
    @patch(
        "app.services.sample_experience_builder.LLMClient.complete_json"
    )
    def test_parallel_analysis_builds_downloadable_document(
        self,
        complete_json,
        iter_chunks,
    ):
        iter_chunks.return_value = iter(
            [
                "第一章\n" + ("人物做出选择并承担后果。" * 6000),
                "第二章\n" + ("两个人用动作和短句互相试探。" * 6000),
            ]
        )
        complete_json.return_value = (
            "{}",
            {
                "overview": "样本善于让行动改变关系。",
                "part_summary": "本部分包含清晰选择和生活化对话。",
                "plot_principles": ["行动必须产生后果"],
                "expression_principles": ["动作承担潜台词"],
                "anti_patterns": ["避免解释情绪"],
                "plot_experiences": [
                    {
                        "part_index": 1,
                        "title": "选择带来关系后果",
                        "transferable_pattern": "让人物为主动选择失去退路",
                        "quality_score": 90,
                    }
                ],
                "expression_experiences": [
                    {
                        "part_index": 1,
                        "title": "动作式拒绝",
                        "original_excerpt": "她把杯子收了回去。",
                        "transferable_technique": "用物品变化代替直接解释",
                        "quality_score": 90,
                    }
                ],
            },
        )

        document = build_sample_experience_document(
            sample_title="测试样本",
            source_genre="现实",
            source_object_key="sample.txt",
            llm_config=LLMConfig(
                base_url="https://example.com/v1",
                api_key="test",
                model="review-model",
            ),
        )

        self.assertLessEqual(document["part_count"], 10)
        self.assertTrue(document["plot_experiences"])
        self.assertTrue(document["expression_experiences"])
        self.assertIn("创作经验文档", document["markdown"])
        self.assertEqual(
            complete_json.call_count,
            document["part_count"] + 1,
        )
        first_prompt = "\n".join(
            message["content"]
            for message in complete_json.call_args_list[0].args[0]
        )
        self.assertIn("dialogue_reaction_chain", first_prompt)
        self.assertIn("chapter_ending_handoff", first_prompt)
        self.assertIn("刺激、人物化理解/回避、可见反应", first_prompt)
        self.assertIn("身份自抬后被字面降格", first_prompt)
        self.assertIn("亮点条目低于88分不要保留", first_prompt)


if __name__ == "__main__":
    unittest.main()
