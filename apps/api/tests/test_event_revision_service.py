"""事件级联合审校与修订编排回归测试。"""

import unittest
from unittest.mock import patch

from app.services.event_quality_checker import normalize_event_quality_result
from app.services.event_revision_service import (
    apply_paragraph_patches,
    apply_structural_window_patches,
    build_contiguous_window_repair_plan,
    build_patch_validation_retry_messages,
    build_patch_action_batches,
    build_dynamic_revision_config,
    build_event_repair_packages,
    execute_patch_request_with_budget_retry,
    normalize_repair_plan,
    normalize_paragraph_patches,
    normalize_structural_repair_plan,
    normalize_structural_window_patches,
    repair_package_requires_structural_replan,
    RevisionRequestError,
    split_chapter_paragraphs,
    text_hash,
)
from app.services.llm_client import LLMConfig
from app.models.chapter import Chapter


def event_issue(
    indexes: list[int],
    severity: str = "medium",
    issue_type: str = "event_logic",
) -> dict:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "message": "测试问题",
        "evidence": "测试证据",
        "suggestion": "测试建议",
        "affected_chapter_indexes": indexes,
    }


class EventRepairPackageTests(unittest.TestCase):
    def test_overlapping_cross_chapter_issues_are_repaired_together(self) -> None:
        packages = build_event_repair_packages(
            [event_issue([1, 2]), event_issue([2, 3], issue_type="event_timeline")],
            {1, 2, 3},
        )

        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["chapter_indexes"], [1, 2, 3])
        self.assertEqual(len(packages[0]["issues"]), 2)

    def test_large_component_keeps_complete_cross_chapter_context(self) -> None:
        packages = build_event_repair_packages([event_issue(list(range(1, 9)))], set(range(1, 9)))

        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["chapter_indexes"], list(range(1, 9)))
        self.assertEqual(len(packages[0]["issues"]), 1)

    def test_low_risk_and_unmapped_issues_do_not_trigger_llm_revision(self) -> None:
        packages = build_event_repair_packages(
            [event_issue([1], severity="low"), event_issue([99], severity="high")],
            {1, 2},
        )

        self.assertEqual(packages, [])


class EventQualityNormalizationTests(unittest.TestCase):
    def test_logic_and_character_consistency_issue_types_are_preserved(self) -> None:
        report = normalize_event_quality_result(
            {
                "issues": [
                    event_issue([1, 2], severity="high", issue_type="event_logic"),
                    event_issue([3], issue_type="event_character_consistency"),
                ]
            }
        )

        self.assertEqual(
            [issue["issue_type"] for issue in report["issues"]],
            ["event_logic", "event_character_consistency"],
        )


class ParagraphPatchTests(unittest.TestCase):
    def build_chapter(self) -> Chapter:
        return Chapter(
            chapter_index=2,
            title="测试章",
            content="第一段保持不变。\n\n门禁卡旁放着旧便签。\n\n第三段也保持不变。",
            summary="摘要",
            context_snapshot={"constraints": {"chapter_word_range": {"min": 10, "max": 200}}},
        )

    def test_patch_changes_only_target_paragraph(self) -> None:
        chapter = self.build_chapter()
        patches = normalize_paragraph_patches(
            {
                "patches": [
                    {
                        "chapter_index": 2,
                        "paragraph_index": 2,
                        "old_text": "门禁卡旁放着旧便签。",
                        "new_text": "门禁卡旁放着旧便签，林予安确认那是张江高科的地址。",
                        "reason": "统一地址状态",
                    }
                ]
            },
            {2: chapter},
        )

        content, validation = apply_paragraph_patches(chapter, patches)
        paragraphs = split_chapter_paragraphs(content)

        self.assertEqual(paragraphs[0], "第一段保持不变。")
        self.assertEqual(paragraphs[2], "第三段也保持不变。")
        self.assertIn("张江高科", paragraphs[1])
        self.assertEqual(patches[0]["base_content_hash"], text_hash(chapter.content))
        self.assertLessEqual(validation["after"]["delta"], validation["before"]["delta"])

    def test_event_patch_can_explicitly_split_mixed_character_paragraph(self) -> None:
        chapter = self.build_chapter()
        chapter.content = (
            "第一段保持不变。\n\n"
            "“门禁卡在这里。”林予安愣了一下。他伸手去接。\n\n"
            "第三段也保持不变。"
        )
        patches = normalize_paragraph_patches(
            {
                "patches": [
                    {
                        "chapter_index": 2,
                        "paragraph_index": 2,
                        "operation": "split",
                        "new_paragraphs": [
                            "“门禁卡在这里。”",
                            "林予安愣了一下。他伸手去接。",
                        ],
                        "reason": "说话者与听者反应分段",
                    }
                ]
            },
            {2: chapter},
        )

        content, _validation = apply_paragraph_patches(chapter, patches)

        self.assertEqual(patches[0]["operation"], "split")
        self.assertEqual(
            split_chapter_paragraphs(content),
            [
                "第一段保持不变。",
                "“门禁卡在这里。”",
                "林予安愣了一下。他伸手去接。",
                "第三段也保持不变。",
            ],
        )

    def test_patch_binds_server_text_instead_of_model_echo(self) -> None:
        chapter = self.build_chapter()
        patches = normalize_paragraph_patches(
            {
                "patches": [
                    {
                        "chapter_index": 2,
                        "paragraph_index": 2,
                        "old_text": "模型臆造的原文",
                        "new_text": "替换内容",
                    }
                ]
            },
            {2: chapter},
            expected_base_content_hashes={2: text_hash(chapter.content)},
        )

        self.assertEqual(patches[0]["old_text"], "门禁卡旁放着旧便签。")
        self.assertEqual(
            patches[0]["old_text_hash"],
            text_hash("门禁卡旁放着旧便签。"),
        )

    def test_patch_rejects_changed_server_content_before_normalization(self) -> None:
        chapter = self.build_chapter()
        base_hash = text_hash(chapter.content)
        chapter.content += "\n\n用户刚刚手动增加的段落。"

        with self.assertRaisesRegex(ValueError, "已被其他操作修改"):
            normalize_paragraph_patches(
                {
                    "patches": [
                        {
                            "chapter_index": 2,
                            "paragraph_index": 2,
                            "new_text": "替换内容",
                        }
                    ]
                },
                {2: chapter},
                expected_base_content_hashes={2: base_hash},
            )

    def test_patch_rejects_stale_chapter_version(self) -> None:
        chapter = self.build_chapter()
        patches = normalize_paragraph_patches(
            {
                "patches": [
                    {
                        "chapter_index": 2,
                        "paragraph_index": 2,
                        "old_text": "门禁卡旁放着旧便签。",
                        "new_text": "门禁卡旁放着新便签。",
                    }
                ]
            },
            {2: chapter},
        )
        chapter.content += "\n\n用户刚刚手动增加的段落。"

        with self.assertRaisesRegex(ValueError, "拒绝应用旧补丁"):
            apply_paragraph_patches(chapter, patches)

    def test_shared_repair_plan_is_normalized_by_chapter(self) -> None:
        chapter = self.build_chapter()
        plan = normalize_repair_plan(
            {
                "repair_plan": [
                    {
                        "issue_ids": ["issue_1"],
                        "canonical_facts": ["门禁卡属于小区入口"],
                        "chapter_actions": [
                            {
                                "chapter_index": 2,
                                "paragraph_indexes": [2],
                                "instruction": "明确门禁卡用途",
                            }
                        ],
                    }
                ]
            },
            {2: chapter},
        )

        self.assertEqual(plan["canonical_facts"], ["门禁卡属于小区入口"])
        self.assertEqual(
            plan["actions_by_chapter"][2][0]["paragraph_indexes"],
            [2],
        )

    def test_cross_chapter_merge_instruction_uses_structural_replan(self) -> None:
        self.assertTrue(
            repair_package_requires_structural_replan(
                {
                    "issues": [
                        {
                            "message": "第1章和第2章功能重复",
                            "suggestion": "合并第1章与第2章，压缩为一章",
                        }
                    ],
                    "repair_plan": {"actions_by_chapter": {}},
                }
            )
        )
        self.assertFalse(
            repair_package_requires_structural_replan(
                {
                    "issues": [
                        {
                            "message": "第2章一句对白不自然",
                            "suggestion": "改写该句对白",
                        }
                    ],
                    "repair_plan": {"actions_by_chapter": {}},
                }
            )
        )

    def test_large_contiguous_compression_uses_atomic_windows(self) -> None:
        chapter = Chapter(
            chapter_index=5,
            title="测试章",
            content="\n\n".join(f"第{i}段内容。" for i in range(1, 31)),
            summary="摘要",
            context_snapshot={
                "constraints": {
                    "chapter_word_range": {"min": 2200, "max": 3200}
                }
            },
        )
        plan = build_contiguous_window_repair_plan(
            chapter,
            [
                {
                    "paragraph_indexes": list(range(1, 25)),
                    "instruction": "压缩重复流程和流水账，但补充有效剧情与对话",
                    "canonical_facts": ["核验已经完成"],
                }
            ],
        )

        self.assertIsNotNone(plan)
        windows = plan["windows_by_chapter"][5]
        self.assertEqual(
            [
                (window["start_paragraph"], window["end_paragraph"])
                for window in windows
            ],
            [(3, 14), (15, 24)],
        )
        self.assertEqual(
            plan["chapter_roles"][5]["word_guard"]["target_min"],
            2200,
        )

    def test_very_large_contiguous_compression_keeps_four_atomic_windows(self) -> None:
        chapter = Chapter(
            chapter_index=4,
            title="测试章",
            content="\n\n".join(f"第{i}段内容。" for i in range(1, 50)),
            summary="摘要",
        )
        plan = build_contiguous_window_repair_plan(
            chapter,
            [{
                "paragraph_indexes": list(range(3, 44)),
                "instruction": "压缩重复流程，用人物行动和关系后果替换",
            }],
        )

        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["windows_by_chapter"][4]), 4)

    def test_small_or_non_compression_actions_keep_paragraph_patches(self) -> None:
        chapter = Chapter(
            chapter_index=5,
            title="测试章",
            content="\n\n".join(f"第{i}段内容。" for i in range(1, 20)),
            summary="摘要",
        )

        self.assertIsNone(
            build_contiguous_window_repair_plan(
                chapter,
                [
                    {
                        "paragraph_indexes": list(range(3, 14)),
                        "instruction": "修正人物称呼",
                    }
                ],
            )
        )
        self.assertIsNone(
            build_contiguous_window_repair_plan(
                chapter,
                [
                    {
                        "paragraph_indexes": list(range(3, 9)),
                        "instruction": "压缩重复流程",
                    }
                ],
            )
        )

    def test_structural_window_rewrite_preserves_chapter_boundaries(self) -> None:
        chapter = Chapter(
            chapter_index=2,
            title="测试章",
            content="\n\n".join(
                [
                    "第一段开头状态保持不变。",
                    "第二段继续承接上一章。",
                    "第三段重复说明核验流程。",
                    "第四段继续解释核验规则。",
                    "第五段仍在确认同一手续。",
                    "第六段进入新的现实麻烦。",
                    "第七段结尾状态保持不变。",
                    "第八段章尾钩子保持不变。",
                ]
            ),
            summary="摘要",
            context_snapshot={
                "constraints": {
                    "chapter_word_range": {"min": 20, "max": 500}
                }
            },
        )
        plan = normalize_structural_repair_plan(
            {
                "chapter_roles": [
                    {
                        "chapter_index": 2,
                        "unique_function": "处理现实麻烦并建立首次合作",
                        "entry_state": "承接上一章",
                        "exit_state": "保留章尾钩子",
                        "preserve_facts": ["两人仍处于试住期"],
                    }
                ],
                "window_actions": [
                    {
                        "chapter_index": 2,
                        "start_paragraph": 3,
                        "end_paragraph": 5,
                        "instruction": "压缩重复核验，改为共同处理漏水",
                        "preserve_facts": ["核验已经完成"],
                    }
                ],
            },
            {2: chapter},
        )
        old_window = split_chapter_paragraphs(chapter.content)[2:5]
        parsed = {
            "window_patches": [
                {
                    "chapter_index": 2,
                    "start_paragraph": 3,
                    "end_paragraph": 5,
                    "old_paragraphs": old_window,
                    "new_paragraphs": [
                        "核验结果落定后，楼下忽然传来漏水的喊声。",
                        "两人停止讨论手续，立刻分头确认水源和受损位置。",
                        "第一次合作没有预案，却比那套规则推进得更快。",
                    ],
                    "reason": "用现实行动替换重复手续",
                }
            ],
            "chapter_summaries": [
                {"chapter_index": 2, "summary": "两人完成核验后共同处理漏水。"}
            ],
        }
        patches, summaries = normalize_structural_window_patches(
            parsed,
            {2: chapter},
            plan,
        )
        result = apply_structural_window_patches({2: chapter}, patches)[2]
        paragraphs = split_chapter_paragraphs(result["content"])

        self.assertEqual(paragraphs[:2], split_chapter_paragraphs(chapter.content)[:2])
        self.assertEqual(paragraphs[-2:], split_chapter_paragraphs(chapter.content)[-2:])
        self.assertIn("共同处理漏水", summaries[2])
        self.assertEqual(result["validation"]["strategy"], "local_window_rewrite")


class DynamicRevisionTimeoutTests(unittest.TestCase):
    def test_revision_uses_one_minute_silence_window_without_total_limit(self) -> None:
        config = LLMConfig(
            base_url="https://example.invalid",
            api_key="test",
            model="test",
        )
        _, small = build_dynamic_revision_config(
            config,
            [{"role": "user", "content": "短输入"}],
        )
        _, large = build_dynamic_revision_config(
            config,
            [{"role": "user", "content": "长" * 30000}],
        )

        self.assertEqual(small["first_token_timeout_seconds"], 60)
        self.assertEqual(large["first_token_timeout_seconds"], 60)
        self.assertIsNone(small["total_timeout_seconds"])
        self.assertIsNone(large["total_timeout_seconds"])


class PatchBudgetRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = LLMConfig(
            base_url="https://example.invalid",
            api_key="test",
            model="test",
        )
        self.messages = [{"role": "user", "content": "生成补丁"}]

    def test_patch_targets_are_split_into_three_paragraph_batches(self) -> None:
        batches = build_patch_action_batches(
            [{"paragraph_indexes": list(range(1, 9)), "instruction": "修订"}]
        )

        self.assertEqual(
            [
                [index for action in batch for index in action["paragraph_indexes"]]
                for batch in batches
            ],
            [[1, 2, 3], [4, 5, 6], [7, 8]],
        )

    def test_validation_retry_prompt_targets_only_failed_atomic_window(self) -> None:
        messages = build_patch_validation_retry_messages(
            [{"role": "user", "content": "原始请求"}],
            {"window_patches": []},
            error="结构改写扩写过多",
            mode="window",
            window_plan={
                "windows_by_chapter": {
                    5: [{"start_paragraph": 32, "end_paragraph": 41}]
                }
            },
        )
        correction = messages[-1]["content"]

        self.assertIn("结构改写扩写过多", correction)
        self.assertIn('"start_paragraph":32', correction)
        self.assertIn("不得超过 hard_max", correction)

    def test_patch_request_uses_context_budget_once(self) -> None:
        with patch(
            "app.services.event_revision_service.execute_streaming_revision_request",
            return_value=({"patches": []}, {"output_chars": 20, "streaming": True}),
        ) as request:
            parsed, telemetry = execute_patch_request_with_budget_retry(
                self.config,
                self.messages,
            )

        self.assertEqual(parsed, {"patches": []})
        self.assertFalse(telemetry["budget_retry"])
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("max_output_tokens", request.call_args.kwargs)

    def test_revision_budget_uses_remaining_context(self) -> None:
        with patch(
            "app.services.event_revision_service.execute_streaming_revision_request",
            return_value=(
                {"patches": []},
                {"output_chars": 20, "streaming": True},
            ),
        ) as request:
            execute_patch_request_with_budget_retry(
                self.config,
                self.messages,
            )

        self.assertNotIn("max_output_tokens", request.call_args.kwargs)

    def test_other_failures_are_not_expensively_retried(self) -> None:
        first_error = RevisionRequestError(
            "网络失败",
            {"failure_kind": "request_failed"},
        )
        with patch(
            "app.services.event_revision_service.execute_streaming_revision_request",
            side_effect=first_error,
        ) as request:
            with self.assertRaises(RevisionRequestError):
                execute_patch_request_with_budget_retry(
                    self.config,
                    self.messages,
                )

        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
