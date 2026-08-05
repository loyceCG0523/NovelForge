"""单章 Reviewer 建议 -> Writer 局部补丁闭环测试。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.models.chapter import Chapter
from app.services.chapter_review_cycle import (
    ChapterReviewRequestError,
    MAX_CHAPTER_SUGGESTIONS,
    apply_chapter_patches_with_isolation,
    build_dynamic_chapter_review_config,
    build_rule_based_chapter_suggestions,
    build_chapter_patch_prompt,
    build_chapter_patch_coherence_prompt,
    build_chapter_review_prompt,
    build_review_coverage_report,
    execute_streaming_chapter_review_request,
    extract_referenced_paragraph_indexes,
    get_expected_audit_dimensions,
    merge_chapter_suggestions,
    normalize_chapter_writer_patches,
    normalize_chapter_patch_coherence_checks,
    normalize_chapter_suggestions,
    validate_chapter_patch_coherence,
)
from app.services.llm_client import LLMConfig


class ChapterReviewCycleTests(unittest.TestCase):
    def build_chapter(self) -> Chapter:
        return Chapter(
            chapter_index=3,
            title="雨停之前",
            word_count=42,
            content="林予安推开门。\n\n门明明锁着，她却已经站在屋里。\n\n她说：“我们走吧。”",
        )

    def test_reviewer_receives_every_paragraph_but_is_limited_to_chapter_scope(self) -> None:
        chapter = self.build_chapter()
        messages = build_chapter_review_prompt(chapter)
        system_prompt = messages[0]["content"]
        user_prompt = messages[1]["content"]
        combined_prompt = system_prompt + user_prompt

        self.assertIn("只能检查当前章节内部", system_prompt)
        self.assertIn("不得评价跨章连续性", system_prompt)
        self.assertIn("不得重写正文", system_prompt)
        self.assertIn("现实中的顾虑、成本和拒绝", combined_prompt)
        self.assertIn("删掉这句是否不影响情节", combined_prompt)
        self.assertIn("环境描写是否过量", combined_prompt)
        self.assertIn("职业刻板", combined_prompt)
        self.assertIn("破折号", combined_prompt)
        self.assertIn("不得只机械删除", system_prompt)
        self.assertIn("柔和意象和情绪总结", combined_prompt)
        self.assertIn('"quality_contract"', user_prompt)
        self.assertIn('"audit_results"', user_prompt)
        self.assertIn("最多返回 20 条", user_prompt)
        self.assertLess(len(system_prompt), 600)
        self.assertEqual(MAX_CHAPTER_SUGGESTIONS, 20)
        for paragraph in chapter.content.split("\n\n"):
            self.assertIn(paragraph, user_prompt)

    def test_review_uses_one_minute_silence_window_without_total_limit(self) -> None:
        config = LLMConfig(
            base_url="https://example.com/v1",
            api_key="test",
            model="reviewer",
            max_retries=2,
        )

        small_config, small = build_dynamic_chapter_review_config(
            config,
            [{"role": "user", "content": "短提示"}],
        )
        large_config, large = build_dynamic_chapter_review_config(
            config,
            [{"role": "user", "content": "长" * 20000}],
        )

        self.assertEqual(small["first_token_timeout_seconds"], 60)
        self.assertEqual(large["first_token_timeout_seconds"], 60)
        self.assertIsNone(small_config.total_timeout_seconds)
        self.assertIsNone(large_config.total_timeout_seconds)
        self.assertIsNone(small["total_timeout_seconds"])
        self.assertIsNone(large["total_timeout_seconds"])
        self.assertEqual(small_config.max_retries, 1)
        self.assertEqual(large_config.max_retries, 1)

    def test_chapter_review_request_streams_json_then_parses_at_the_end(self) -> None:
        config = LLMConfig(
            base_url="https://example.com/v1",
            api_key="test",
            model="reviewer",
        )
        client = unittest.mock.MagicMock()
        client.last_request_telemetry = {
            "mode": "non_stream_fallback",
            "status": "completed",
        }

        def fake_complete_json(_messages, **kwargs):
            self.assertTrue(kwargs["stream"])
            kwargs["on_raw_delta"]('{"audit_results":')
            kwargs["on_raw_delta"]("[]}")
            return '{"audit_results":[]}', {"audit_results": []}

        client.complete_json.side_effect = fake_complete_json
        with patch(
            "app.services.chapter_review_cycle.LLMClient",
            return_value=client,
        ) as client_class:
            raw, parsed, telemetry = execute_streaming_chapter_review_request(
                config,
                [{"role": "user", "content": "请审校"}],
            )

        self.assertEqual(raw, '{"audit_results":[]}')
        self.assertEqual(parsed, {"audit_results": []})
        self.assertTrue(telemetry["streaming"])
        self.assertEqual(telemetry["output_chars"], len('{"audit_results":[]}'))
        self.assertEqual(telemetry["transport"]["mode"], "non_stream_fallback")
        self.assertGreater(telemetry["remaining_context_tokens"], 10000)
        self.assertNotIn("max_tokens", client.complete_json.call_args.kwargs)
        self.assertLessEqual(client_class.call_args.args[0].max_retries, 1)

    def test_failed_chapter_review_request_preserves_transport_telemetry(self) -> None:
        config = LLMConfig(
            base_url="https://example.com/v1",
            api_key="test",
            model="reviewer",
        )
        client = unittest.mock.MagicMock()
        client.last_request_telemetry = {
            "mode": "non_stream_fallback",
            "status": "empty",
            "reasoning_only": True,
        }
        client.complete_json.side_effect = RuntimeError("最终 content 为空")

        with patch(
            "app.services.chapter_review_cycle.LLMClient",
            return_value=client,
        ):
            with self.assertRaises(ChapterReviewRequestError) as raised:
                execute_streaming_chapter_review_request(
                    config,
                    [{"role": "user", "content": "请审校"}],
                )

        telemetry = raised.exception.telemetry
        self.assertEqual(telemetry["output_chars"], 0)
        self.assertTrue(telemetry["transport"]["reasoning_only"])

    def test_review_coverage_requires_every_dimension_with_evidence(self) -> None:
        chapter = self.build_chapter()
        dimensions = [
            item["dimension"]
            for item in json.loads(
                build_chapter_review_prompt(chapter)[1]["content"].split("\n\n", 1)[1]
            )["quality_contract"]["audit_order"]
        ]
        complete = {
            "audit_results": [
                {
                    "dimension": dimension,
                    "status": "pass",
                    "paragraph_indexes": [1],
                    "evidence": "第1段提供代表性依据",
                    "conclusion": "该维度未发现问题",
                }
                for dimension in dimensions
            ],
            "suggestions": [],
        }

        report = build_review_coverage_report(
            complete,
            paragraph_count=3,
            expected_dimensions=tuple(dimensions),
        )

        self.assertTrue(report["valid"])
        self.assertEqual(report["coverage_percent"], 100)

        complete["audit_results"][0]["evidence"] = ""
        incomplete = build_review_coverage_report(
            complete,
            paragraph_count=3,
            expected_dimensions=tuple(dimensions),
        )

        self.assertFalse(incomplete["valid"])
        self.assertIn(dimensions[0], incomplete["missing_dimensions"])

    def test_meme_fit_is_not_required_when_chapter_adopts_no_meme(self) -> None:
        chapter = self.build_chapter()
        expected = get_expected_audit_dimensions(chapter)
        prompt_payload = json.loads(
            build_chapter_review_prompt(chapter)[1]["content"].split("\n\n", 1)[1]
        )
        prompt_dimensions = [
            item["dimension"]
            for item in prompt_payload["quality_contract"]["audit_order"]
        ]
        parsed = {
            "audit_results": [
                {
                    "dimension": dimension,
                    "status": "pass",
                    "paragraph_indexes": [1],
                    "evidence": "第 1 段提供代表性依据",
                    "conclusion": "该维度未发现问题",
                }
                for dimension in expected
            ]
        }

        report = build_review_coverage_report(
            parsed,
            paragraph_count=3,
            expected_dimensions=expected,
        )

        self.assertNotIn("meme_fit", expected)
        self.assertNotIn("meme_fit", prompt_dimensions)
        self.assertTrue(report["valid"])
        self.assertEqual(report["covered_count"], report["expected_count"])

    def test_meme_fit_remains_required_when_chapter_adopts_a_meme(self) -> None:
        chapter = self.build_chapter()
        chapter.content += "\n\n她看着他：“这很邪修。”"
        chapter.context_snapshot = {
            "meme_reference_pack": {
                "references": [{"entry_id": "meme-1", "phrase": "邪修"}],
            },
            "chapter_progress": {
                "meme_usage_plan": [
                    {"phrase": "邪修", "decision": "use", "speaker": "她"},
                ],
            },
        }

        expected = get_expected_audit_dimensions(chapter)
        prompt_payload = json.loads(
            build_chapter_review_prompt(chapter)[1]["content"].split("\n\n", 1)[1]
        )
        prompt_dimensions = [
            item["dimension"]
            for item in prompt_payload["quality_contract"]["audit_order"]
        ]

        self.assertIn("meme_fit", expected)
        self.assertIn("meme_fit", prompt_dimensions)

    def test_reviewer_receives_compact_current_chapter_contract(self) -> None:
        chapter = self.build_chapter()
        chapter.context_snapshot = {
            "novel": {
                "brief": {
                    "characters": [
                        {
                            "name": "沈栀夏",
                            "occupation": "高薪算法工程师",
                            "detailed_setting": "生活技能一般，但爱干净。",
                        }
                    ]
                }
            },
            "story_bible": {
                "content": {
                    "world_rules": {"story_era": "2026 年", "story_location": "上海"},
                    "style_rules": {"anti_ai_rules": ["避免职业刻板比喻"]},
                }
            },
            "target": {
                "task_input": {
                    "chapter_plan": {
                        "core_event": "两位主角相遇",
                        "ending_hook": "女主提出一项让男主无法立刻答应的条件",
                    },
                    "unrelated_large_payload": "不得进入审校提示词",
                }
            },
        }

        messages = build_chapter_review_prompt(chapter)
        user_prompt = messages[1]["content"]

        self.assertIn("高薪算法工程师", user_prompt)
        self.assertIn("生活技能一般，但爱干净", user_prompt)
        self.assertIn("女主提出一项让男主无法立刻答应的条件", user_prompt)
        self.assertNotIn("unrelated_large_payload", user_prompt)

    def test_rule_based_short_sentence_checker_is_connected_to_review_cycle(self) -> None:
        chapter = Chapter(
            chapter_index=1,
            title="雨夜",
            word_count=18,
            content="“明天。”她说，“买碗。还有筷子。”",
        )

        suggestions = build_rule_based_chapter_suggestions(chapter)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["issue_type"], "prose_rhythm")
        self.assertEqual(suggestions[0]["paragraph_indexes"], [1])
        self.assertEqual(suggestions[0]["source"], "punctuation_style_checker")

    def test_rule_based_checker_finds_reversed_chinese_quotes(self) -> None:
        chapter = Chapter(
            chapter_index=1,
            title="雨夜",
            word_count=20,
            content='林予安被公司正式“毕业“了。\n\n他问：”毕业有学位证吗？“',
        )

        suggestions = build_rule_based_chapter_suggestions(chapter)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["issue_type"], "grammar")
        self.assertEqual(suggestions[0]["severity"], "high")
        self.assertEqual(suggestions[0]["paragraph_indexes"], [1, 2])
        self.assertIn("共涉及 2 段", suggestions[0]["problem"])
        self.assertIn("只修正成对标点", suggestions[0]["repair_scope"])

    def test_rule_based_checker_accepts_balanced_nested_punctuation(self) -> None:
        chapter = Chapter(
            chapter_index=1,
            title="雨夜",
            word_count=20,
            content='她问：“你读过《长夜》吗？”他答：“读过‘旧版’。”',
        )

        suggestions = build_rule_based_chapter_suggestions(chapter)

        self.assertEqual(suggestions, [])

    def test_rule_suggestions_take_priority_without_exceeding_twenty(self) -> None:
        rule = [
            {
                "issue_type": "prose_rhythm",
                "paragraph_indexes": [1],
                "problem": "机械短句",
                "suggestion": "合并",
            }
        ]
        reviewer = [
            {
                "issue_type": "grammar",
                "paragraph_indexes": [index],
                "problem": f"问题 {index}",
                "suggestion": "修改",
            }
            for index in range(1, 25)
        ]

        merged = merge_chapter_suggestions(rule, reviewer)

        self.assertEqual(len(merged), 20)
        self.assertEqual(merged[0]["issue_type"], "prose_rhythm")
        self.assertEqual(
            [item["suggestion_index"] for item in merged],
            list(range(1, 21)),
        )

    def test_normalizer_keeps_only_locatable_actionable_suggestions(self) -> None:
        suggestions = normalize_chapter_suggestions(
            {
                "suggestions": [
                    {
                        "issue_type": "chapter_logic",
                        "severity": "high",
                        "paragraph_indexes": [1, 2],
                        "problem": "开门动作与门锁状态矛盾",
                        "evidence": "第一段推门，第二段称门锁着",
                        "suggestion": "补充开锁动作或调整第二段状态",
                    },
                    {
                        "issue_type": "grammar",
                        "severity": "medium",
                        "paragraph_indexes": [99],
                        "problem": "无法定位",
                        "suggestion": "不应保留",
                    },
                    {
                        "issue_type": "ai_style",
                        "severity": "low",
                        "paragraph_indexes": [3],
                        "problem": "缺少可执行建议",
                        "suggestion": "",
                    },
                ]
            },
            paragraph_count=3,
        )

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["paragraph_indexes"], [1, 2])
        self.assertEqual(suggestions[0]["suggestion_index"], 1)

    def test_normalizer_adds_paragraphs_explicitly_named_in_suggestion_text(self) -> None:
        suggestions = normalize_chapter_suggestions(
            {
                "suggestions": [
                    {
                        "issue_type": "chapter_logic",
                        "severity": "medium",
                        "paragraph_indexes": [28, 30],
                        "problem": "第28段与第30段说箱子已经清完。",
                        "evidence": "第153-155段又出现箱子。",
                        "suggestion": "把第155段的快递箱改成杂物。",
                    }
                ]
            },
            paragraph_count=169,
        )

        self.assertEqual(
            suggestions[0]["paragraph_indexes"],
            [28, 30, 153, 154, 155],
        )

    def test_normalizer_preserves_expanded_quality_dimension_and_repair_intent(self) -> None:
        suggestions = normalize_chapter_suggestions(
            {
                "suggestions": [
                    {
                        "issue_type": "dialogue_realism",
                        "severity": "medium",
                        "paragraph_indexes": [2],
                        "problem": "上下级对白不符合真实职场中的体面需求。",
                        "evidence": "领导当众使用明显羞辱性措辞。",
                        "reader_impact": "读者会觉得冲突由作者强行制造。",
                        "repair_scope": "保留裁员带来的打击。",
                        "suggestion": "改为礼貌但冷酷的绩效和编制说法。",
                    }
                ]
            },
            paragraph_count=3,
        )

        self.assertEqual(suggestions[0]["issue_type"], "dialogue_realism")
        self.assertEqual(suggestions[0]["reader_impact"], "读者会觉得冲突由作者强行制造。")
        self.assertEqual(suggestions[0]["repair_scope"], "保留裁员带来的打击。")

    def test_paragraph_reference_extraction_ignores_impossible_ranges(self) -> None:
        self.assertEqual(
            extract_referenced_paragraph_indexes("第3-5段和第9段", paragraph_count=10),
            [3, 4, 5, 9],
        )
        self.assertEqual(
            extract_referenced_paragraph_indexes("第1-99段", paragraph_count=100),
            [],
        )

    def test_reviewer_suggestion_count_is_bounded(self) -> None:
        raw = {
            "suggestions": [
                {
                    "issue_type": "grammar",
                    "severity": "low",
                    "paragraph_indexes": [1],
                    "problem": f"问题 {index}",
                    "suggestion": f"建议 {index}",
                }
                for index in range(MAX_CHAPTER_SUGGESTIONS + 5)
            ]
        }

        self.assertEqual(
            len(normalize_chapter_suggestions(raw, paragraph_count=3)),
            MAX_CHAPTER_SUGGESTIONS,
        )

    def test_writer_prompt_requires_minimal_paragraph_patches(self) -> None:
        chapter = self.build_chapter()
        suggestion = {
            "suggestion_index": 1,
            "issue_type": "chapter_logic",
            "severity": "high",
            "paragraph_indexes": [1, 2],
            "problem": "门锁状态矛盾",
            "evidence": "两段动作冲突",
            "suggestion": "补充开锁动作",
        }
        messages = build_chapter_patch_prompt(chapter, [suggestion])
        system_prompt = messages[0]["content"]
        payload_text = messages[1]["content"].split("\n\n", 1)[1]
        payload = json.loads(payload_text)

        self.assertIn("不得重写整章", system_prompt)
        self.assertIn("replace 只返回完整单段", system_prompt)
        self.assertIn("人物主体混段时用 split", system_prompt)
        self.assertIn("不要返回 old_text", system_prompt)
        self.assertIn("repair_scope", system_prompt)
        self.assertIn("不能生硬补解释", system_prompt)
        self.assertIn("前后各 2 段", system_prompt)
        self.assertIn("delete 冗余段", system_prompt)
        self.assertIn("处理破折号问题时", system_prompt)
        self.assertIn("换另一种破折号", system_prompt)
        self.assertIn("异常缩短或丢失关键表述的补丁会被拒绝", system_prompt)
        self.assertLess(len(system_prompt), 700)
        output_contract = messages[1]["content"].split("\n\n", 1)[0]
        self.assertIn("new_paragraphs", output_contract)
        self.assertNotIn('"old_text":', output_contract)
        self.assertNotIn('"chapter_index":', output_contract)
        self.assertEqual(payload["target_suggestions"], [suggestion])
        self.assertEqual(len(payload["paragraphs"]), 3)
        self.assertEqual(payload["target_paragraph_indexes"], [1, 2])
        self.assertEqual(payload["editable_paragraph_indexes"], [1, 2, 3])

    def test_writer_only_receives_target_paragraph_and_two_neighbors_each_side(self) -> None:
        chapter = self.build_chapter()
        chapter.content = "\n\n".join(f"第 {index} 段。" for index in range(1, 8))

        messages = build_chapter_patch_prompt(
            chapter,
            [{"paragraph_indexes": [4], "suggestion": "修复第四段"}],
        )
        payload = json.loads(messages[1]["content"].split("\n\n", 1)[1])

        self.assertEqual(payload["target_paragraph_indexes"], [4])
        self.assertEqual(payload["editable_paragraph_indexes"], [2, 3, 4, 5, 6])
        self.assertEqual(
            [item["paragraph_index"] for item in payload["paragraphs"]],
            [2, 3, 4, 5, 6],
        )
        self.assertEqual(payload["paragraphs"][2]["scope"], "target")

    def test_semantic_patch_coherence_prompt_compares_the_modified_dialogue_chain(self) -> None:
        chapter = Chapter(
            chapter_index=1,
            title="雨夜",
            word_count=80,
            content="\n\n".join(
                [
                    "“你住哪儿？”",
                    "“前面。”",
                    "“方便的话，借我躲个雨。”",
                    "她看了他几秒。",
                    "“你刚才说我不像正常人。”",
                ]
            ),
        )
        suggestions = [
            {
                "issue_type": "behavior_realism",
                "paragraph_indexes": [3],
                "repair_scope": "保留男主请求跟随女主的剧情功能",
                "suggestion": "增加现实阻碍和安全边界",
            }
        ]
        patches = [
            {
                "paragraph_index": 3,
                "operation": "replace",
                "new_text": "“走吧。”她看了他一眼，“继续研究。”",
            }
        ]

        prompt = build_chapter_patch_coherence_prompt(chapter, suggestions, patches)
        payload = json.loads(prompt[1]["content"].split("\n", 1)[1])

        self.assertIn("提问—回答", prompt[0]["content"])
        self.assertEqual(payload["patch_indexes"], [3])
        self.assertIn("借我躲个雨", str(payload["before_window"]))
        self.assertIn("继续研究", str(payload["after_window"]))
        self.assertIn("你刚才说我不像正常人", str(payload["after_window"]))

    def test_semantic_patch_is_rejected_when_neighbor_reply_loses_its_premise(self) -> None:
        suggestions = [
            {
                "issue_type": "behavior_realism",
                "paragraph_indexes": [119],
            }
        ]
        patches = [
            {
                "paragraph_index": 119,
                "new_text": "“走吧。”她没直接回答，“继续研究。”",
            }
        ]

        accepted, rejected = normalize_chapter_patch_coherence_checks(
            {
                "checks": [
                    {
                        "paragraph_index": 119,
                        "status": "fail",
                        "broken_links": [
                            "P121仍由女方说“你刚才说我不像正常人”，但P119已改为女方主动邀请，回应链失去男方请求这一前提"
                        ],
                        "reason": "对话意图断裂",
                    }
                ]
            },
            patches,
            suggestions,
        )

        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]["code"], "neighbor_coherence")
        self.assertIn("回应链", rejected[0]["message"])

    def test_style_only_patch_skips_semantic_model_gate(self) -> None:
        patches = [{"paragraph_index": 2, "new_text": "“因为，”她顿了顿。"}]
        accepted, rejected = normalize_chapter_patch_coherence_checks(
            {},
            patches,
            [{"issue_type": "ai_style", "paragraph_indexes": [2]}],
        )

        self.assertEqual(rejected, [])
        self.assertEqual(accepted[0]["coherence_validation"]["status"], "skipped_style_only")

    def test_coherence_check_retries_invalid_json_then_accepts_patch(self) -> None:
        chapter = self.build_chapter()
        suggestions = [
            {
                "issue_type": "state_continuity",
                "paragraph_indexes": [2],
                "repair_scope": "保留原有因果关系",
            }
        ]
        patches = [
            {
                "paragraph_index": 2,
                "operation": "replace",
                "new_text": "门去年冬天就锁着，她已经站在屋里。",
            }
        ]
        config = LLMConfig(
            base_url="https://example.com/v1",
            api_key="test",
            model="reviewer",
            max_retries=1,
        )
        success = {
            "checks": [
                {
                    "paragraph_index": 2,
                    "status": "pass",
                    "broken_links": [],
                    "reason": "时间锚点明确，前后因果连续",
                }
            ]
        }

        with patch(
            "app.services.chapter_review_cycle.execute_streaming_chapter_review_request",
            side_effect=[
                ChapterReviewRequestError(
                    "Expecting value: line 1 column 1 (char 0)",
                    {"output_chars": 0, "transport": {"status": "empty"}},
                ),
                ('{"checks":[]}', success, {"output_chars": 88}),
            ],
        ) as request:
            accepted, rejected, audit = validate_chapter_patch_coherence(
                chapter,
                suggestions,
                patches,
                config,
            )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[0].max_retries, 0)
        self.assertEqual(rejected, [])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["attempt"], 2)
        self.assertEqual(audit["attempts"][0]["status"], "failed")

    def test_coherence_check_keeps_original_after_two_invalid_responses(self) -> None:
        chapter = self.build_chapter()
        suggestions = [
            {
                "issue_type": "state_continuity",
                "paragraph_indexes": [2],
            }
        ]
        patches = [{"paragraph_index": 2, "new_text": "补充时间锚点。"}]
        config = LLMConfig(
            base_url="https://example.com/v1",
            api_key="test",
            model="reviewer",
        )

        with patch(
            "app.services.chapter_review_cycle.execute_streaming_chapter_review_request",
            side_effect=[
                ChapterReviewRequestError("empty", {"output_chars": 0}),
                ChapterReviewRequestError("invalid json", {"output_chars": 6}),
            ],
        ):
            accepted, rejected, audit = validate_chapter_patch_coherence(
                chapter,
                suggestions,
                patches,
                config,
            )

        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]["code"], "coherence_check_failed")
        self.assertIn("连续两次", rejected[0]["message"])
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(len(audit["attempts"]), 2)

    def test_writer_old_text_is_canonicalized_by_paragraph_index(self) -> None:
        chapter = self.build_chapter()
        suggestions = [{"paragraph_indexes": [2]}]

        accepted, rejected, canonicalized = normalize_chapter_writer_patches(
            {
                "patches": [
                    {
                        "chapter_index": 3,
                        "paragraph_index": 2,
                        "old_text": "她却已经站在屋里。",
                        "new_text": "门锁没有扣死，她已经站在屋里。",
                        "reason": "修复门锁状态",
                    }
                ]
            },
            chapter,
            suggestions,
        )

        self.assertEqual(rejected, [])
        self.assertEqual(canonicalized, [2])
        self.assertEqual(accepted[0]["old_text"], "门明明锁着，她却已经站在屋里。")
        self.assertEqual(accepted[0]["paragraph_index"], 2)

    def test_writer_patch_chapter_is_always_bound_by_server(self) -> None:
        chapter = self.build_chapter()
        accepted, rejected, _ = normalize_chapter_writer_patches(
            {
                "patches": [
                    {
                        "chapter_index": 1,
                        "paragraph_index": 2,
                        "operation": "replace",
                        "new_text": "门去年冬天就锁着，她已经站在屋里。",
                        "reason": "补充时间锚点",
                    }
                ]
            },
            chapter,
            [
                {
                    "issue_type": "state_continuity",
                    "paragraph_indexes": [2],
                }
            ],
        )

        self.assertEqual(rejected, [])
        self.assertEqual(accepted[0]["chapter_index"], chapter.chapter_index)
        self.assertEqual(accepted[0]["chapter_binding"]["model_value"], 1)
        self.assertTrue(accepted[0]["chapter_binding"]["canonicalized"])

    def test_invalid_patch_does_not_discard_valid_sibling_patch(self) -> None:
        chapter = self.build_chapter()
        suggestions = [{"paragraph_indexes": [1, 2]}]

        accepted, rejected, _ = normalize_chapter_writer_patches(
            {
                "patches": [
                    {
                        "chapter_index": 3,
                        "paragraph_index": 1,
                        "new_text": "林予安用钥匙打开门。",
                    },
                    {
                        "chapter_index": 3,
                        "paragraph_index": 2,
                        "new_text": "门锁着。\n\n她已经站在屋里。",
                    },
                ]
            },
            chapter,
            suggestions,
        )

        self.assertEqual([patch["paragraph_index"] for patch in accepted], [1])
        self.assertEqual(rejected[0]["paragraph_index"], 2)
        self.assertEqual(rejected[0]["code"], "multiple_paragraphs")

    def test_punctuation_patch_rejects_p40_style_information_loss(self) -> None:
        original = (
            "“我自己设计的。”沈栀夏歪了歪头，"
            "“我参考了用户调研中的需求分析模型，把室友筛选转化为多维度评估。"
            "通过预设场景观察候选人的反应模式，来判断是否适合——”“等等。”"
        )
        chapter = Chapter(
            chapter_index=2,
            title="奇怪的面试",
            word_count=len(original),
            content="前段。\n\n" + original + "\n\n后段。",
        )
        suggestions = [
            {
                "source": "llm_reviewer",
                "issue_type": "ai_style",
                "paragraph_indexes": [2],
                "problem": "对话打断使用了破折号。",
                "evidence": "来判断是否适合——”“等等。”",
                "repair_scope": "保留男主打断女主长段解释的剧情功能。",
                "suggestion": "去掉破折号，用人物动作承载打断。",
            }
        ]

        accepted, rejected, _ = normalize_chapter_writer_patches(
            {
                "patches": [
                    {
                        "chapter_index": 2,
                        "paragraph_index": 2,
                        "operation": "replace",
                        "new_text": (
                            "“来判断是否适合。”沈栀夏似乎还要继续，"
                            "林予安抬起手，“等等。”"
                        ),
                        "reason": "去掉破折号并增加打断动作",
                    }
                ]
            },
            chapter,
            suggestions,
        )

        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]["code"], "information_loss")
        self.assertFalse(rejected[0]["validation"]["valid"])
        self.assertLess(rejected[0]["validation"]["length_retention"], 0.68)

    def test_punctuation_patch_accepts_full_information_with_new_interruption(self) -> None:
        original = (
            "“我自己设计的。”沈栀夏歪了歪头，"
            "“我参考了用户调研中的需求分析模型，把室友筛选转化为多维度评估。"
            "通过预设场景观察候选人的反应模式，来判断是否适合——”“等等。”"
        )
        revised = (
            "“我自己设计的。”沈栀夏歪了歪头，"
            "“我参考了用户调研中的需求分析模型，把室友筛选转化为多维度评估。"
            "通过预设场景观察候选人的反应模式，来判断是否适合。”"
            "她似乎还要继续，林予安抬起手：“等等。”"
        )
        chapter = Chapter(
            chapter_index=2,
            title="奇怪的面试",
            word_count=len(original),
            content=original,
        )
        suggestions = [
            {
                "source": "llm_reviewer",
                "issue_type": "ai_style",
                "paragraph_indexes": [1],
                "problem": "对话打断使用了破折号。",
                "suggestion": "去掉破折号，用人物动作承载打断。",
            }
        ]

        accepted, rejected, _ = normalize_chapter_writer_patches(
            {
                "patches": [
                    {
                        "chapter_index": 2,
                        "paragraph_index": 1,
                        "operation": "replace",
                        "new_text": revised,
                        "reason": "保留完整解释并改用动作打断",
                    }
                ]
            },
            chapter,
            suggestions,
        )

        self.assertEqual(rejected, [])
        self.assertEqual(len(accepted), 1)
        self.assertTrue(accepted[0]["information_retention"]["valid"])

    def test_retry_scope_rejects_unrequested_paragraph_without_losing_target(self) -> None:
        chapter = self.build_chapter()
        chapter.content = "\n\n".join(f"第 {index} 段。" for index in range(1, 7))
        suggestions = [{"paragraph_indexes": [2]}]

        accepted, rejected, _ = normalize_chapter_writer_patches(
            {
                "patches": [
                    {"chapter_index": 3, "paragraph_index": 2, "new_text": "第二段已修复。"},
                    {"chapter_index": 3, "paragraph_index": 5, "new_text": "第五段越界修改。"},
                ]
            },
            chapter,
            suggestions,
            retry_paragraph_indexes={2},
        )

        self.assertEqual([patch["paragraph_index"] for patch in accepted], [2])
        self.assertEqual(rejected[0]["paragraph_index"], 5)
        self.assertEqual(rejected[0]["code"], "out_of_scope")

    def test_writer_can_merge_target_with_neighbor_and_delete_redundant_paragraph(self) -> None:
        chapter = self.build_chapter()
        chapter.context_snapshot = {
            "constraints": {"chapter_word_range": {"min": 1, "max": 200}}
        }
        suggestions = [{"paragraph_indexes": [2]}]

        accepted, rejected, _ = normalize_chapter_writer_patches(
            {
                "patches": [
                    {
                        "chapter_index": 3,
                        "paragraph_index": 2,
                        "operation": "replace",
                        "new_text": "门没有锁，她已经站在屋里，朝他招了招手。",
                        "reason": "统一门锁状态并保留人物动作",
                    },
                    {
                        "chapter_index": 3,
                        "paragraph_index": 3,
                        "operation": "delete",
                        "new_text": "",
                        "reason": "对白动作已经合并到第2段，删除重复承接",
                    },
                ]
            },
            chapter,
            suggestions,
        )

        self.assertEqual(rejected, [])
        self.assertEqual([patch["scope"] for patch in accepted], ["target", "context_neighbor"])
        with patch(
            "app.services.event_revision_service.build_word_guard_report",
            return_value={"within_range": True, "delta": 0},
        ):
            content, _validation, applied, application_rejections = (
                apply_chapter_patches_with_isolation(chapter, accepted)
            )
        self.assertEqual(application_rejections, [])
        self.assertEqual(len(applied), 2)
        self.assertEqual(
            content,
            "林予安推开门。\n\n门没有锁，她已经站在屋里，朝他招了招手。",
        )

    def test_writer_can_split_dialogue_from_another_characters_reaction(self) -> None:
        chapter = self.build_chapter()
        chapter.content = (
            "雨还没停。\n\n"
            "“进来会弄湿我地板，门口成本更低。”林予安张了张嘴。他头一回见人这样算收留成本。\n\n"
            "门内安静下来。"
        )
        chapter.context_snapshot = {
            "constraints": {"chapter_word_range": {"min": 1, "max": 300}}
        }
        suggestions = [{"issue_type": "prose_rhythm", "paragraph_indexes": [2]}]

        accepted, rejected, _ = normalize_chapter_writer_patches(
            {
                "patches": [
                    {
                        "paragraph_index": 2,
                        "operation": "split",
                        "new_paragraphs": [
                            "“进来会弄湿我地板，门口成本更低。”",
                            "林予安张了张嘴。他头一回见人这样算收留成本。",
                        ],
                        "reason": "台词与听者反应属于不同人物",
                    }
                ]
            },
            chapter,
            suggestions,
        )

        self.assertEqual(rejected, [])
        self.assertEqual(accepted[0]["operation"], "split")
        with patch(
            "app.services.event_revision_service.build_word_guard_report",
            return_value={"within_range": True, "delta": 0},
        ):
            content, _validation, applied, application_rejections = (
                apply_chapter_patches_with_isolation(chapter, accepted)
            )
        self.assertEqual(application_rejections, [])
        self.assertEqual(len(applied), 1)
        self.assertEqual(
            content,
            "雨还没停。\n\n“进来会弄湿我地板，门口成本更低。”\n\n"
            "林予安张了张嘴。他头一回见人这样算收留成本。\n\n门内安静下来。",
        )

    def test_application_guard_failure_isolated_to_one_patch(self) -> None:
        chapter = self.build_chapter()
        patches = [
            {
                "chapter_index": 3,
                "paragraph_index": 1,
                "old_text": "林予安推开门。",
                "new_text": "林予安用钥匙打开门。",
            },
            {
                "chapter_index": 3,
                "paragraph_index": 2,
                "old_text": "门明明锁着，她却已经站在屋里。",
                "new_text": "这是一个异常冗长且不应通过字数守卫的替换段落。",
            },
        ]

        def fake_apply(target, target_patches):
            if len(target_patches) == 2:
                raise ValueError("第 3 章局部修订后超出字数范围")
            paragraph_index = target_patches[0]["paragraph_index"]
            if isinstance(target, SimpleNamespace):
                if paragraph_index == 2:
                    raise ValueError("第 3 章局部修订后超出字数范围")
                return target.content.replace("林予安推开门。", "林予安用钥匙打开门。"), {}
            return chapter.content.replace("林予安推开门。", "林予安用钥匙打开门。"), {
                "before": {},
                "after": {},
            }

        with patch(
            "app.services.chapter_review_cycle.apply_paragraph_patches",
            side_effect=fake_apply,
        ):
            content, _validation, accepted, rejected = apply_chapter_patches_with_isolation(
                chapter,
                patches,
            )

        self.assertIn("用钥匙打开门", content)
        self.assertEqual([item["paragraph_index"] for item in accepted], [1])
        self.assertEqual(rejected[0]["paragraph_index"], 2)
        self.assertEqual(rejected[0]["code"], "application_guard")

if __name__ == "__main__":
    unittest.main()
