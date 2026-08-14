import unittest
from unittest.mock import patch

from app.api.sample_analyses import _annotation_index_status, _compact_report_for_read
from app.services.agents.sample_analysis_agent import analyze_sample_chunks
from app.services.llm_client import LLMConfig


class SampleAnalysisContractTests(unittest.TestCase):
    def test_annotation_index_status_uses_real_counts(self):
        self.assertEqual(
            _annotation_index_status({"annotation_count": 0}),
            "unannotated",
        )
        self.assertEqual(
            _annotation_index_status(
                {"annotation_count": 2, "indexed_count": 0, "pending_index_count": 2}
            ),
            "pending_index",
        )
        self.assertEqual(
            _annotation_index_status(
                {"annotation_count": 3, "indexed_count": 2, "pending_index_count": 1}
            ),
            "partial",
        )
        self.assertEqual(
            _annotation_index_status(
                {"annotation_count": 2, "indexed_count": 2, "pending_index_count": 0}
            ),
            "ready",
        )

    def test_v3_report_is_projected_to_compact_read_contract(self):
        compact = _compact_report_for_read(
            {
                "schema_version": "sample_analysis.v3",
                "style_fingerprint": {"huge": "data"},
                "chunk_summaries": [{"chunk": index} for index in range(100)],
                "llm_style_strategy": {
                    "available": True,
                    "style_summary": "简短总结",
                    "dialogue_guidelines": [f"对白规则 {index}" for index in range(6)],
                    "generation_guidelines": [f"叙述规则 {index}" for index in range(6)],
                    "anti_ai_guidelines": [f"反 AI {index}" for index in range(6)],
                },
                "rag_index": {"status": "completed"},
            }
        )

        self.assertNotIn("style_fingerprint", compact)
        self.assertNotIn("chunk_summaries", compact)
        self.assertEqual(len(compact["reference_profile"]["language_principles"]), 4)
        self.assertEqual(len(compact["reference_profile"]["avoid_errors"]), 3)

    @patch("app.services.agents.sample_analysis_agent.LLMClient.complete_json")
    def test_v5_report_keeps_only_three_analysis_outputs(self, complete_json):
        complete_json.return_value = (
            "{}",
            {
                "overall_evaluation": "人物说话依赖关系和共同经历。" * 40,
                "language_principles": [f"语言原则 {index}" * 40 for index in range(8)],
                "avoid_errors": [f"应避免错误 {index}" * 40 for index in range(8)],
                "ignored_field": {"large": "不应保存" * 100},
            },
        )
        report = analyze_sample_chunks(
            sample_title="测试样本",
            source_genre="现实",
            chunks=[
                "第一章\n她把碗往桌角推了推。‘你还走吗？’他没回答，只把门边那双旧鞋摆正。"
                * 20
            ],
            llm_config=LLMConfig(
                base_url="https://example.com/v1",
                api_key="test",
                model="test-model",
            ),
        )

        self.assertEqual(report["schema_version"], "sample_analysis.v5")
        self.assertEqual(
            set(report),
            {"schema_version", "agent", "analysis_mode", "sample", "reference_profile"},
        )
        self.assertNotIn("transferable_style_vector", report)
        self.assertNotIn("style_fingerprint", report)
        self.assertNotIn("chunk_summaries", report)
        profile = report["reference_profile"]
        self.assertLessEqual(len(profile["overall_evaluation"]), 1000)
        self.assertLessEqual(len(profile["language_principles"]), 6)
        self.assertLessEqual(len(profile["avoid_errors"]), 6)
        self.assertTrue(all(len(item) <= 200 for item in profile["language_principles"]))
        self.assertNotIn("plot_experiences", report)
        self.assertNotIn("expression_experiences", report)

    def test_v5_report_completes_without_llm(self):
        report = analyze_sample_chunks(
            sample_title="测试样本",
            source_genre="现实",
            chunks=["第一章\n他没有解释，只把钥匙放回原处。" * 20],
            llm_config=None,
        )

        self.assertFalse(report["reference_profile"]["available"])
        self.assertEqual(report["reference_profile"]["language_principles"], [])


if __name__ == "__main__":
    unittest.main()
