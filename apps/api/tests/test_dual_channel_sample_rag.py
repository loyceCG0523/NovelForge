import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.models.sample_analysis import SampleAnalysis
from app.services.sample_passage_indexer import extract_sample_passage_candidates
from app.services.sample_rag import build_expression_query
from worker.graphs.event_generation_graph import (
    _build_event_plan_prompt,
    _build_simulated_event_plan,
    _candidate_diversity_report,
    _event_plan_quality_report,
    _normalize_event_plan,
)


class DualChannelSampleRagTests(unittest.TestCase):
    def test_language_query_uses_relationship_not_fixed_plot_direction(self):
        novel = SimpleNamespace(genre="现实", premise="成长")
        context = {
            "target": {
                "task_input": {
                    "chapter_plan": {
                        "core_event": "只用于验证、不应进入语言查询的核心事件",
                        "ending_hook": "只用于验证、不应进入语言查询的章尾钩子",
                        "character_beats": ["母女都想和解，但谁也不肯先道歉"],
                    }
                }
            },
            "recent_chapters": [
                {
                    "chapter_progress": {
                        "ending_state": {
                            "location": "厨房",
                            "characters": {"母亲": "假装忙碌", "女儿": "站在门口"},
                            "open_actions": ["女儿还没说出离开的真正原因"],
                        }
                    }
                }
            ],
            "memories": [],
            "constraints": {"story_era": "当代"},
        }

        query = build_expression_query(novel, context)

        self.assertIn("母女都想和解", query)
        self.assertIn("答非所问", query)
        self.assertNotIn("不应进入语言查询的核心事件", query)
        self.assertNotIn("不应进入语言查询的章尾钩子", query)

    @patch("app.services.sample_passage_indexer.iter_text_object_chunks")
    def test_indexer_creates_complete_plot_windows(self, iter_chunks):
        causal_paragraph = (
            "消息突然传来，他发现原定的见面已经被人提前取消。但是朋友拒绝说明原因，"
            "他只好决定绕过对方去找另一个知情人。没想到那个人其实一直在等他，反而提出"
            "一个必须立刻选择的条件。这个选择会让他失去原来的退路，因此也惹来新的关系"
            "冲突。两个人没有马上争吵，而是各自带着误解去完成不同的行动，结果在同一个"
            "地方再次撞见，才意识到此前的信息被第三个人故意错开。"
        )
        iter_chunks.return_value = iter(
            ["第一章\n" + "\n\n".join([causal_paragraph] * 7)]
        )
        analysis = SampleAnalysis(source_object_key="sample.txt")

        passages = extract_sample_passage_candidates(analysis)
        plot_windows = [
            item for item in passages if item["passage_type"] == "plot_window"
        ]

        self.assertTrue(plot_windows)
        self.assertGreaterEqual(len(plot_windows[0]["content"]), 700)
        mechanism = plot_windows[0]["metadata_payload"]["mechanism_coverage"]
        self.assertTrue(mechanism["trigger"])
        self.assertTrue(mechanism["choice"])
        self.assertTrue(mechanism["reversal"])
        self.assertTrue(mechanism["consequence"])

    def test_event_prompt_requires_divergent_candidates_before_final_plan(self):
        novel = SimpleNamespace(
            title="测试书",
            genre="现实",
            premise="一家人重新学习相处",
            brief={
                "characters": [{"name": "林禾"}],
                "legacy_duplicate_marker": "不应进入事件规划提示词",
            },
            current_chapter_index=8,
        )
        task_input = {
            "story_bible": {
                "summary": "不需要重复的全书摘要",
                "content": {
                    "main_plot": {
                        "premise": "一家人重新学习相处",
                        "planned_events": [
                            {
                                "key": "event-1",
                                "title": "最早未完成节点",
                                "planned_time": "第9章",
                            },
                            {
                                "key": "event-2",
                                "title": "紧随其后的节点",
                                "planned_time": "第10章",
                            },
                        ],
                    },
                    "narrative_contract": {
                        "primary_reader_promise": "家庭关系发生真实变化",
                    },
                },
            },
            "plot_reference_pack": {
                "references": [
                    {
                        "passage_id": "plot-1",
                        "excerpt": "一段包含人物选择、误判和后果的真实情节窗口。",
                        "mechanism": {"choice": True, "reversal": True},
                        "technique": "抽取触发、阻力、选择、转折与代价",
                    }
                ]
            },
        }

        prompt = "\n".join(
            item["content"]
            for item in _build_event_plan_prompt(
                novel,
                task_input,
                chapter_count=6,
                start_index=9,
            )
        )

        self.assertIn("至少 4 个真正不同", prompt)
        self.assertIn("一段包含人物选择", prompt)
        self.assertIn("candidate_directions", prompt)
        self.assertIn("淘汰最直觉", prompt)
        self.assertEqual(prompt.count("一段包含人物选择、误判和后果的真实情节窗口。"), 1)
        self.assertIn('"technique"', prompt)
        self.assertIn("narrative_contract", prompt)
        self.assertIn("primary_promise_served", prompt)
        self.assertIn("secondary_element_role", prompt)
        self.assertIn("reader_payoff", prompt)
        self.assertIn("流程完成", prompt)
        self.assertIn("最早未完成节点", prompt)
        self.assertIn("紧随其后的节点", prompt)
        self.assertIn("相邻小事件可合并", prompt)
        self.assertNotIn("所有章节服务同一事件", prompt)
        self.assertNotIn("不应进入事件规划提示词", prompt)
        self.assertNotIn("不需要重复的全书摘要", prompt)
        self.assertEqual(prompt.count("一家人重新学习相处"), 1)

    def test_event_plan_preserves_candidate_reasoning_for_audit(self):
        novel = SimpleNamespace(
            title="测试书",
            genre="现实",
            premise="成长",
            brief={},
        )
        raw = {
            "event_title": "旧屋协商",
            "event_goal": "完成搬迁决定",
            "core_conflict": "家庭成员对旧屋去留意见不同",
            "candidate_directions": [
                {
                    "direction": "由弟弟主动隐瞒估价结果",
                    "conflict_source": "弟弟的主动行为",
                    "character_choice": "姐姐决定暂不揭穿",
                    "turn_or_reframe": "争房变成保护父亲知情权",
                    "cost_or_consequence": "姐姐失去母亲信任",
                    "difference_from_other_candidates": "冲突由保护行为而非利益争夺驱动",
                }
            ],
            "selection_rationale": "没有采用最直觉的遗产争夺。",
            "adapted_plot_mechanisms": ["善意隐瞒改变冲突性质"],
            "chapter_plans": [],
        }

        normalized = _normalize_event_plan(
            raw,
            novel,
            chapter_count=1,
            start_index=9,
            task_input={"test_run_scope": "first_chapter"},
        )

        self.assertEqual(len(normalized["candidate_directions"]), 1)
        self.assertIn("遗产争夺", normalized["selection_rationale"])
        self.assertEqual(
            normalized["adapted_plot_mechanisms"],
            ["善意隐瞒改变冲突性质"],
        )

    def test_diversity_gate_rejects_four_cosmetic_variants(self):
        raw = {
            "candidate_directions": [
                {
                    "conflict_source": "同一个误会",
                    "character_choice": "主角追问",
                    "turn_or_reframe": "对方承认",
                    "cost_or_consequence": "关系紧张",
                }
                for _ in range(4)
            ]
        }

        report = _candidate_diversity_report(raw)

        self.assertFalse(report["passed"])
        self.assertEqual(report["candidate_count"], 4)
        self.assertEqual(report["unique_structure_count"], 1)

    def test_type_quality_gate_requires_varied_engines_and_reader_payoff(self):
        candidates = [
            {
                "plot_engine": engine,
                "primary_promise_served": "兑现当前作品的主类型承诺",
                "secondary_element_role": "只提供压力或行动手段",
                "dramatic_escalation": "选择带来更高代价",
                "reader_payoff": "局势发生阶段性变化",
            }
            for engine in ("目标冲突", "关系变化", "信息反转", "限时选择")
        ]
        chapters = [
            {
                "plot_engine": "目标冲突",
                "dramatic_turn": "行动产生意外后果",
                "reader_payoff": "主角取得阶段进展",
            }
        ]
        raw = {
            "genre_alignment": "服务当前作品类型",
            "dramatic_escalation": "阻力逐步升级",
            "major_reversal": "新信息改变判断",
            "reader_payoff": "兑现阶段承诺",
            "candidate_directions": candidates,
            "chapter_plans": chapters,
        }

        passed = _event_plan_quality_report(
            raw,
            {"primary_genre": "任意类型", "primary_reader_promise": "阶段承诺"},
        )
        missing_payoff = _event_plan_quality_report(
            {
                **raw,
                "candidate_directions": [
                    {**item, "reader_payoff": ""}
                    for item in candidates
                ],
            },
            {"primary_genre": "任意类型", "primary_reader_promise": "阶段承诺"},
        )

        self.assertTrue(passed["passed"])
        self.assertFalse(missing_payoff["passed"])

    def test_simulation_fallback_is_genre_adaptive_not_book_specific(self):
        novel = SimpleNamespace(
            title="测试书",
            genre="悬疑",
            premise="一个旧谜团重新出现",
            brief={"selling_points": "真相反转"},
        )
        plan = _build_simulated_event_plan(
            novel,
            {"story_bible": {}},
            chapter_count=4,
            start_index=1,
        )

        self.assertTrue(plan["planning_quality"]["passed"])
        self.assertEqual(plan["narrative_contract"]["primary_genre"], "悬疑")
        self.assertNotIn("校园", plan["event_title"])
        self.assertNotIn("产品经理", str(plan))


if __name__ == "__main__":
    unittest.main()
