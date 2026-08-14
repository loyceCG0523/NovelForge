import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.models.sample_analysis import SampleAnalysis
from app.services.sample_passage_indexer import extract_sample_passage_candidates
from app.services.sample_rag import (
    ChapterReferenceRequirementError,
    build_chapter_reference_pack,
    build_expression_query,
    require_minimum_chapter_references,
)
from worker.graphs.event_generation_graph import (
    _auxiliary_dominance_report,
    _build_event_plan_prompt,
    _build_scene_orchestration_prompt,
    _build_simulated_event_plan,
    _candidate_diversity_report,
    _event_plan_quality_report,
    _mark_auxiliary_semantically_passed,
    _merge_chapter_plan_replacements,
    _merge_scene_orchestration,
    _normalize_event_plan,
    _semantic_auxiliary_targets,
)


def _complete_scene_execution():
    return {
        "entry_pressure": "门外的人正在催答复",
        "protagonist_want": "主角想拖延决定",
        "opposing_want": "对方要主角现在表态",
        "tactic_turns": [
            {
                "actor": "主角",
                "tactic": "用反问试探对方底线",
                "counterforce": "对方给出不能回避的事实",
                "local_change": "主角失去继续拖延的借口",
            },
            {
                "actor": "对方",
                "tactic": "提出一个带期限的选择",
                "counterforce": "主角接受条件但改变执行方式",
                "local_change": "双方关系和下一步行动被改写",
            },
        ],
        "dialogue_pressure": {
            "surface_topic": "是否马上离开",
            "hidden_stakes": "双方都不愿承认自己害怕被抛下",
            "decisive_exchange": "一次回避后的反问迫使主角表态",
        },
        "pov_reaction_chain": {
            "observable_detail": "对方把门拉开一半却没有让路",
            "biased_interpretation": "主角以为对方还在故意刁难",
            "immediate_impulse": "主角想用玩笑掩饰自己其实想留下",
            "visible_response": "主角嘴上催促，脚却没有往门外迈",
        },
        "dialogue_reaction_chain": {
            "trigger": "对方问主角到底走不走",
            "evasion_or_misread": "主角故意讨论门口太窄",
            "countermove": "对方把门彻底关上，要求正面回答",
            "local_consequence": "主角失去回避空间并提出留下条件",
        },
        "voice_contrast": [],
        "absurd_comedy_mode": {"enabled": False},
        "ending_residual_force": {
            "last_change": "主角答应留下但提出新条件",
            "reader_question": "对方会不会接受这个条件",
            "next_chapter_first_beat": "从对方听完条件后的反应开始",
        },
    }


class DualChannelSampleRagTests(unittest.TestCase):
    def test_chapter_reference_minimum_is_a_hard_gate(self):
        accepted = require_minimum_chapter_references(
            {
                "status": "completed",
                "references": [{"annotation_id": "a1", "excerpt": "一条可信参考"}],
            }
        )

        self.assertEqual(accepted["minimum_required"], 1)
        self.assertTrue(accepted["requirement_satisfied"])
        with self.assertRaises(ChapterReferenceRequirementError):
            require_minimum_chapter_references(
                {"status": "empty", "reason": "可信标注库为空", "references": []}
            )

    @patch("app.services.sample_rag._count_available_trusted_annotations", return_value=1)
    @patch("app.services.sample_rag._retrieve_reference_pack")
    @patch("app.services.sample_rag.build_expression_query", return_value="当前章检索条件")
    def test_chapter_reference_automatically_falls_back_to_plot_annotation(
        self,
        _build_query,
        retrieve_pack,
        _count_annotations,
    ):
        retrieve_pack.side_effect = [
            {"status": "empty", "reason": "无表达标注", "references": []},
            {
                "status": "completed",
                "references": [
                    {"annotation_id": "plot-1", "excerpt": "一条可信剧情参考"}
                ],
                "total_chars": 8,
            },
        ]
        db = SimpleNamespace(get=lambda *_args, **_kwargs: SimpleNamespace(preferences={}))
        result = build_chapter_reference_pack(
            db,
            novel=SimpleNamespace(owner_id="owner"),
            context={},
        )

        self.assertEqual(result["fallback_channel"], "plot")
        self.assertEqual(len(result["references"]), 1)
        self.assertEqual(retrieve_pack.call_args_list[0].kwargs["channel"], "language")
        self.assertEqual(retrieve_pack.call_args_list[1].kwargs["channel"], "plot")

    @patch("app.services.sample_rag._count_available_trusted_annotations", return_value=0)
    @patch("app.services.sample_rag._retrieve_reference_pack")
    @patch("app.services.sample_rag.build_expression_query", return_value="当前章检索条件")
    def test_chapter_reference_gate_is_skipped_when_no_trusted_annotations_exist(
        self,
        _build_query,
        retrieve_pack,
        _count_annotations,
    ):
        """用户还没有任何可信标注时，门槛不生效，允许无参考生成。"""
        retrieve_pack.side_effect = [
            {"status": "empty", "reason": "暂无可用的可信language协同标注", "references": []},
            {"status": "empty", "reason": "暂无可用的可信plot协同标注", "references": []},
        ]
        db = SimpleNamespace(get=lambda *_args, **_kwargs: SimpleNamespace(preferences={}))
        result = build_chapter_reference_pack(
            db,
            novel=SimpleNamespace(owner_id="owner"),
            context={},
        )

        self.assertEqual(result["references"], [])
        self.assertEqual(result["minimum_required"], 0)
        self.assertFalse(result["requirement_satisfied"])
        self.assertEqual(result["requirement_skipped"], "no_trusted_annotations")

    @patch("app.services.sample_rag._count_available_trusted_annotations", return_value=3)
    @patch("app.services.sample_rag._retrieve_reference_pack")
    @patch("app.services.sample_rag.build_expression_query", return_value="当前章检索条件")
    def test_chapter_reference_gate_still_blocks_when_annotations_exist_but_retrieve_empty(
        self,
        _build_query,
        retrieve_pack,
        _count_annotations,
    ):
        """已有可信标注但一条都检索不到（如索引未建/模型过期）时，门槛仍然拦截。"""
        retrieve_pack.side_effect = [
            {"status": "empty", "reason": "暂无可用的可信language协同标注", "references": []},
            {"status": "empty", "reason": "暂无可用的可信plot协同标注", "references": []},
        ]
        db = SimpleNamespace(get=lambda *_args, **_kwargs: SimpleNamespace(preferences={}))
        with self.assertRaises(ChapterReferenceRequirementError):
            build_chapter_reference_pack(
                db,
                novel=SimpleNamespace(owner_id="owner"),
                context={},
            )

    def test_language_query_uses_relationship_not_fixed_plot_direction(self):
        novel = SimpleNamespace(genre="现实", premise="成长")
        context = {
            "target": {
                "task_input": {
                    "chapter_plan": {
                        "core_event": "只用于验证、不应进入语言查询的核心事件",
                        "ending_hook": "只用于验证、不应进入语言查询的章尾钩子",
                        "character_beats": ["母女都想和解，但谁也不肯先道歉"],
                        "scene_execution": {
                            "pov_reaction_chain": {
                                "biased_interpretation": "女儿误以为母亲不想留她"
                            },
                            "dialogue_reaction_chain": {
                                "evasion_or_misread": "母亲故意问晚饭吃什么"
                            },
                        },
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
        self.assertIn("女儿误以为母亲不想留她", query)
        self.assertIn("对白刺激→回避/抓错重点→反击", query)
        self.assertIn("身份自抬后的字面降格", query)
        self.assertIn("围观者短促补刀", query)
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
        self.assertIn("scene_execution", prompt)
        self.assertIn("策略—反制—局部变化", prompt)
        self.assertIn("一本正经跑偏", prompt)
        self.assertIn("pov_reaction_chain", prompt)
        self.assertIn("dialogue_reaction_chain", prompt)
        self.assertIn("自利解释", prompt)
        scene_prompt = "\n".join(
            message["content"]
            for message in _build_scene_orchestration_prompt(
                {"chapter_plans": []},
                {"primary_genre": "都市轻喜剧"},
            )
        )
        self.assertIn("章节场面编排", scene_prompt)
        self.assertIn("可观察细节→带私心的误读", scene_prompt)
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
                "scene_execution": _complete_scene_execution(),
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
        self.assertEqual(passed["complete_scene_execution_count"], 1)
        self.assertFalse(missing_payoff["passed"])

    def test_type_quality_gate_rejects_empty_scene_execution(self):
        candidates = [
            {
                "plot_engine": engine,
                "primary_promise_served": "类型承诺",
                "secondary_element_role": "只作辅助",
                "dramatic_escalation": "阻力升级",
                "reader_payoff": "局势变化",
            }
            for engine in ("目标冲突", "关系变化", "信息反转", "限时选择")
        ]
        report = _event_plan_quality_report(
            {
                "genre_alignment": "类型一致",
                "dramatic_escalation": "阻力升级",
                "major_reversal": "发生反转",
                "reader_payoff": "产生回报",
                "candidate_directions": candidates,
                "chapter_plans": [{
                    "chapter_index": 2,
                    "function": "冲突",
                    "plot_engine": "目标冲突",
                    "dramatic_turn": "出现麻烦",
                    "reader_payoff": "产生变化",
                    "scene_execution": {},
                }],
            },
            {"primary_genre": "现实题材", "primary_reader_promise": "人物成长"},
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["complete_scene_execution_count"], 0)

    def test_type_quality_gate_rejects_consecutive_auxiliary_topic_dominance(self):
        candidates = [
            {
                "plot_engine": engine,
                "primary_promise_served": "兑现都市喜剧",
                "secondary_element_role": "只提供一次压力",
                "dramatic_escalation": "人物行动造成后果",
                "reader_payoff": "关系和局势同时变化",
            }
            for engine in ("目标冲突", "关系变化", "信息反转", "限时选择")
        ]
        chapters = [
            {
                "chapter_index": index,
                "function": "冲突",
                "plot_engine": "关系变化",
                "dramatic_turn": "主动选择带来反转",
                "reader_payoff": "两人关系变化",
                "core_event": core_event,
                "secondary_element_role": role,
                "comedy_beats": ["铺垫与后果"] * 4,
            }
            for index, core_event, role in (
                (1, "用产品思维和用户需求分析分手", "产品经理方案制造笑点"),
                (2, "用SWOT和产品方案说服对方", "职业能力继续主导对话"),
            )
        ]
        report = _event_plan_quality_report(
            {
                "genre_alignment": "都市喜剧",
                "dramatic_escalation": "阻力升级",
                "major_reversal": "新信息改变判断",
                "reader_payoff": "关系推进",
                "candidate_directions": candidates,
                "chapter_plans": chapters,
            },
            {
                "primary_genre": "高密度都市轻喜剧",
                "primary_reader_promise": "原创笑点和关系推进",
                "supporting_element_policy": "职业和技术只作辅助，不能连续主导",
            },
        )

        self.assertFalse(report["passed"])
        self.assertFalse(report["auxiliary_dominance"]["passed"])
        self.assertEqual(
            report["auxiliary_dominance"]["violations"][0]["chapter_indexes"],
            [1, 2],
        )

    def test_auxiliary_keyword_scan_ignores_fields_that_explain_background_role(self):
        report = _auxiliary_dominance_report(
            [
                {
                    "chapter_index": index,
                    "plot_engine": "关系攻防",
                    "core_event": "两个人因误解互相试探，最后被迫共同承担后果",
                    "dramatic_turn": "一句答非所问的话暴露了真实立场",
                    "state_change": "双方从互相防备转为暂时结盟",
                    "reader_payoff": "关系推进并产生新的共同秘密",
                    "ending_hook": "门外突然出现了不该出现的人",
                    "secondary_element_role": "租房协议和物业登记只作背景压力",
                    "compressed_processes": ["合同、押金、核验手续全部一笔带过"],
                }
                for index in (2, 3)
            ],
            {
                "primary_genre": "都市轻喜剧",
                "supporting_element_policy": "租房手续只作辅助，不能连续主导",
            },
        )

        self.assertTrue(report["passed"])
        self.assertEqual(report["violations"], [])

    def test_semantic_review_can_clear_keyword_suspicion_without_user_warning(self):
        quality_report = {
            "status": "failed",
            "passed": False,
            "candidate_count": 4,
            "complete_candidate_count": 4,
            "unique_plot_engine_count": 4,
            "chapter_count": 2,
            "complete_chapter_count": 2,
            "complete_scene_execution_count": 2,
            "event_fields_complete": True,
            "comedy_delivery_passed": True,
            "first_chapter_hook_passed": True,
            "auxiliary_dominance": {
                "passed": False,
                "violations": [
                    {"topic": "housing_administration", "chapter_indexes": [2, 3]}
                ],
            },
        }

        updated = _mark_auxiliary_semantically_passed(
            quality_report,
            {"dominant_chapter_indexes": [], "assessments": []},
        )

        self.assertTrue(updated["passed"])
        self.assertEqual(updated["status"], "passed")
        self.assertEqual(updated["auxiliary_dominance"]["violations"], [])
        self.assertEqual(
            updated["auxiliary_dominance"]["keyword_violations"][0]["chapter_indexes"],
            [2, 3],
        )

    def test_semantic_review_repairs_the_whole_consecutive_suspect_group(self):
        targets = _semantic_auxiliary_targets(
            {"dominant_chapter_indexes": [2], "assessments": []},
            {
                "auxiliary_dominance": {
                    "violations": [
                        {"topic": "housing_administration", "chapter_indexes": [2, 3]},
                        {"topic": "career_technology", "chapter_indexes": [7, 8]},
                    ]
                }
            },
        )

        self.assertEqual(targets, [2, 3])

    def test_targeted_repair_preserves_good_chapters_and_requires_new_plot_engine(self):
        original = {
            "chapter_plans": [
                {"chapter_index": 1, "plot_engine": "目标冲突", "core_event": "保留原章"},
                {"chapter_index": 2, "plot_engine": "关系攻防", "core_event": "问题原章"},
            ]
        }
        replacement = {
            "chapter_index": 2,
            "plot_engine": "信息反转",
            "core_event": "一句误会迫使两人共同对外撒谎",
            "state_change": "两人从各自撇清变为临时共谋",
            "dramatic_turn": "第三人拿出与双方说法矛盾的证据",
            "reader_payoff": "关系被迫升级，谎言产生即时后果",
            "ending_hook": "真正知情的人发来一条语音",
            "comedy_beats": ["误会", "嘴硬", "错位", "回旋镖"],
            "scene_execution": _complete_scene_execution(),
        }

        merged, validation = _merge_chapter_plan_replacements(
            original,
            {"chapter_plan_replacements": [replacement]},
            [2],
        )

        self.assertTrue(validation["passed"])
        self.assertEqual(merged["chapter_plans"][0], original["chapter_plans"][0])
        self.assertEqual(merged["chapter_plans"][1]["plot_engine"], "信息反转")

        unchanged, rejected = _merge_chapter_plan_replacements(
            original,
            {
                "chapter_plan_replacements": [
                    {**replacement, "plot_engine": "关系攻防"}
                ]
            },
            [2],
        )
        self.assertFalse(rejected["passed"])
        self.assertEqual(rejected["unchanged_plot_engine_indexes"], [2])
        self.assertEqual(unchanged, original)

    def test_scene_orchestration_only_replaces_execution_fields(self):
        original = {
            "event_title": "测试事件",
            "chapter_plans": [
                {
                    "chapter_index": 1,
                    "core_event": "不可改写的宏观事件",
                    "plot_engine": "目标冲突",
                    "scene_execution": _complete_scene_execution(),
                    "comedy_beats": ["原节拍"],
                }
            ],
        }
        replacement_scene = _complete_scene_execution()
        replacement_scene["pov_reaction_chain"]["biased_interpretation"] = (
            "主角误以为对方在赶他走"
        )

        merged, validation = _merge_scene_orchestration(
            original,
            {
                "chapter_scene_directions": [
                    {
                        "chapter_index": 1,
                        "core_event": "模型试图越界改写",
                        "scene_execution": replacement_scene,
                        "comedy_beats": ["误读", "嘴硬", "反击", "回旋镖"],
                    }
                ]
            },
        )

        self.assertTrue(validation["passed"])
        self.assertEqual(
            merged["chapter_plans"][0]["core_event"],
            "不可改写的宏观事件",
        )
        self.assertEqual(
            merged["chapter_plans"][0]["scene_execution"]["pov_reaction_chain"]["biased_interpretation"],
            "主角误以为对方在赶他走",
        )
        self.assertEqual(len(merged["chapter_plans"][0]["comedy_beats"]), 4)

    def test_type_quality_gate_rejects_setup_only_first_chapter(self):
        candidates = [
            {
                "plot_engine": engine,
                "primary_promise_served": "类型承诺",
                "secondary_element_role": "只作辅助",
                "dramatic_escalation": "阻力升级",
                "reader_payoff": "局势变化",
            }
            for engine in ("目标冲突", "关系变化", "信息反转", "限时选择")
        ]
        report = _event_plan_quality_report(
            {
                "genre_alignment": "类型一致",
                "dramatic_escalation": "阻力升级",
                "major_reversal": "发生反转",
                "reader_payoff": "产生回报",
                "candidate_directions": candidates,
                "chapter_plans": [{
                    "chapter_index": 1,
                    "function": "铺垫",
                    "plot_engine": "目标冲突",
                    "dramatic_turn": "出现麻烦",
                    "reader_payoff": "产生变化",
                }],
            },
            {"primary_genre": "现实题材", "primary_reader_promise": "人物成长"},
        )

        self.assertFalse(report["first_chapter_hook_passed"])
        self.assertFalse(report["passed"])

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
