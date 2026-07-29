"""章节字数护栏的偏差校准回归测试。"""

import unittest

from app.services.chapter_word_guard import (
    _build_correction_target_range,
    build_calibrated_chapter_word_target,
    build_safe_chapter_word_range,
    generate_chapter_with_word_guard,
)


class FakeChapterClient:
    def __init__(self, lengths: list[int]) -> None:
        self.lengths = lengths
        self.calls: list[list[dict[str, str]]] = []

    def generate_chapter(self, messages, max_tokens=None, temperature=None):
        self.calls.append(messages)
        length = self.lengths[len(self.calls) - 1]
        return {"title": "测试章", "summary": "摘要", "content": "字" * length}


class ChapterWordGuardCalibrationTests(unittest.TestCase):
    def test_default_hard_range_is_2500_to_2800(self) -> None:
        target = build_safe_chapter_word_range(None)

        self.assertEqual(target, {"min": 2600, "max": 2700, "unit": "字"})

    def test_initial_prompt_target_stays_inside_hard_acceptance_range(self) -> None:
        target = build_safe_chapter_word_range({"min": 2200, "max": 3200})

        self.assertEqual(target, {"min": 2600, "max": 2800, "unit": "字"})

    def test_same_model_history_scales_next_prompt_before_generation(self) -> None:
        target = build_calibrated_chapter_word_target(
            {"min": 2200, "max": 3200},
            [
                {
                    "word_guard": {
                        "model_calibration_key": "same-model",
                        "attempts": [
                            {
                                "prompt_target_min": 2600,
                                "prompt_target_max": 2800,
                                "actual_words": 4320,
                            }
                        ],
                    }
                }
            ],
            "same-model",
        )

        self.assertEqual(target["sample_count"], 1)
        self.assertEqual(target["observed_ratio"], 1.6)
        self.assertEqual(target["applied_ratio"], 1.3)
        self.assertEqual(target["prompt_target_min"], 2000)
        self.assertEqual(target["prompt_target_max"], 2200)

    def test_different_model_history_is_not_reused(self) -> None:
        target = build_calibrated_chapter_word_target(
            {"min": 2200, "max": 3200},
            [
                {
                    "word_guard": {
                        "model_calibration_key": "other-model",
                        "attempts": [
                            {
                                "prompt_target_min": 2600,
                                "prompt_target_max": 2800,
                                "actual_words": 4320,
                            }
                        ],
                    }
                }
            ],
            "same-model",
        )

        self.assertEqual(target["sample_count"], 0)
        self.assertEqual(target["prompt_target_min"], 2600)
        self.assertEqual(target["prompt_target_max"], 2800)

    def test_overproduction_ratio_aggressively_reduces_next_prompt_target(self) -> None:
        target = _build_correction_target_range(
            {
                "actual_words": 4572,
                "prompt_target_min": 2400,
                "prompt_target_max": 3000,
                "status": "too_long",
            },
            {"min": 2200, "max": 3200},
        )

        self.assertLess(target["max"], 2400)
        self.assertGreaterEqual(target["min"], 500)

    def test_underproduction_correction_is_bounded(self) -> None:
        target = _build_correction_target_range(
            {
                "actual_words": 1438,
                "prompt_target_min": 2400,
                "prompt_target_max": 3000,
                "status": "too_short",
            },
            {"min": 2200, "max": 3200},
        )

        self.assertLessEqual(target["max"], 3800)
        self.assertGreaterEqual(target["min"], 3000)

    def test_second_attempt_can_pass_without_using_extra_rounds(self) -> None:
        client = FakeChapterClient([4500, 2800])

        result, report = generate_chapter_with_word_guard(
            llm_client=client,
            prompt_messages=[{"role": "user", "content": "生成章节"}],
            word_range={"min": 2200, "max": 3200},
        )

        self.assertEqual(len(result["content"]), 2800)
        self.assertTrue(report["accepted"])
        self.assertEqual(report["selected_attempt"], 2)
        self.assertIn("真实偏差做有限校准", client.calls[1][-1]["content"])

    def test_nearest_version_is_accepted_after_three_failed_attempts(self) -> None:
        client = FakeChapterClient([3200, 2300, 2900])

        result, report = generate_chapter_with_word_guard(
            llm_client=client,
            prompt_messages=[{"role": "user", "content": "生成章节"}],
            word_range={"min": 2500, "max": 2800},
        )

        self.assertEqual(len(result["content"]), 2900)
        self.assertTrue(report["accepted"])
        self.assertTrue(report["accepted_by_nearest"])
        self.assertFalse(report["needs_revision"])
        self.assertEqual(report["selected_attempt"], 3)


if __name__ == "__main__":
    unittest.main()
