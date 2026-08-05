import json
import unittest
from types import SimpleNamespace

from app.services.prompt_context import (
    compact_expression_reference_pack,
    compact_recent_chapters,
    compact_review_issues,
    compact_story_bible,
    latest_chapter_progress,
)
from app.services.prompt_builder import build_chapter_prompt
from app.services.story_bible_builder import build_story_bible_prompt


class PromptContextTests(unittest.TestCase):
    def test_sample_context_drops_retrieval_query_and_legacy_report(self):
        expression_pack = compact_expression_reference_pack(
            {
                "status": "completed",
                "query": "只用于向量检索，不应进入章节模型" * 100,
                "references": [
                    {"passage_id": str(index), "passage_type": "dialogue", "excerpt": f"片段{index}"}
                    for index in range(12)
                ],
            }
        )
        self.assertEqual(expression_pack["minimum_required"], 1)
        self.assertTrue(expression_pack["requirement_satisfied"])
        self.assertIn("至少一条", expression_pack["usage_policy"]["required"])
        story_bible = compact_story_bible(
            {
                "content": {
                    "style_rules": {
                        "sample_style_references": [{"full_report": "旧报告" * 100}],
                        "sample_style_rules": [f"短规则{index}" for index in range(10)],
                    }
                }
            }
        )

        self.assertNotIn("query", expression_pack)
        self.assertEqual(len(expression_pack["references"]), 10)
        rules = story_bible["content"]["style_rules"]
        self.assertNotIn("sample_style_references", rules)
        self.assertEqual(len(rules["sample_style_rules"]), 6)

    def test_only_latest_chapter_keeps_full_content(self):
        old_content = "旧章开头" + ("旧" * 1200) + "旧章结尾"
        latest_content = "最新章完整正文" * 100
        compacted = compact_recent_chapters(
            [
                {"chapter_index": 1, "content": old_content, "summary": "第一章"},
                {
                    "chapter_index": 2,
                    "content": latest_content,
                    "summary": "第二章旧摘要",
                    "chapter_progress": {
                        "actual_summary": "第二章实际摘要",
                        "completed_beats": ["已经发生的节点"],
                        "ending_state": {"location": "楼道"},
                    },
                },
            ]
        )

        self.assertEqual(compacted[0]["content_scope"], "ending_excerpt")
        self.assertEqual(len(compacted[0]["content"]), 800)
        self.assertNotIn("旧章开头", compacted[0]["content"])
        self.assertEqual(compacted[1]["content"], latest_content)

        progress = latest_chapter_progress(compacted)
        self.assertEqual(progress["actual_summary"], "第二章实际摘要")
        self.assertEqual(progress["completed_beats"], ["已经发生的节点"])
        self.assertEqual(progress["ending_state"]["location"], "楼道")

    def test_review_issues_drop_internal_payload_and_limit_history(self):
        compacted = compact_review_issues(
            [
                {
                    "id": f"issue-{index}",
                    "issue_type": "ai_style",
                    "severity": "low",
                    "message": f"问题 {index}",
                    "payload": {
                        "suggestion": f"建议 {index}",
                        "raw_response": "不应进入生成提示词" * 50,
                    },
                }
                for index in range(12)
            ]
        )

        self.assertEqual(len(compacted), 10)
        self.assertNotIn("id", compacted[0])
        self.assertNotIn("raw_response", json.dumps(compacted, ensure_ascii=False))

    def test_story_bible_prompt_sends_schema_outline_not_fallback_copy(self):
        novel = SimpleNamespace(
            title="测试书",
            genre="现实",
            target_words=100000,
            premise="普通人重新开始生活",
            brief={},
        )

        messages = build_story_bible_prompt(novel)
        payload = json.loads(messages[1]["content"].split("\n")[-1])

        self.assertEqual(payload["required_schema"]["schema_version"], "string")
        self.assertEqual(
            payload["required_schema"]["generation_policy"]["event_chapter_count"],
            "integer",
        )
        self.assertNotIn("强冲突", messages[1]["content"])
        self.assertLess(len(messages[1]["content"]), 2500)

    def test_chapter_prompt_uses_compacted_history_and_progress_boundary(self):
        old_prefix = "不应进入模型输入的第一章开头"
        latest_content = "第二章完整正文"
        context = {
            "novel": {"title": "测试书", "genre": "现实", "premise": "生存", "brief": {}},
            "story_bible": {"content": {"rule": "唯一设定"}},
            "target": {
                "chapter_index": 3,
                "task_input": {
                    "story_event": {"event_title": "搬家"},
                    "chapter_plan": {"chapter_index": 3, "core_event": "调查脚步声"},
                    "next_chapter_boundary": {"chapter_index": 4, "core_event": "找到房东"},
                },
            },
            "constraints": {"chapter_word_range": {"min": 2500, "max": 2800}},
            "generation_guidance": {
                "chapter_goal": "调查脚步声",
                "chapter_word_range": {"min": 2500, "max": 2800},
                "continuity_reminders": [],
                "anti_ai_reminders": [],
            },
            "recent_chapters": [
                {
                    "chapter_index": 1,
                    "content": old_prefix + ("旧" * 1000) + "第一章结尾",
                    "summary": "第一章摘要",
                },
                {
                    "chapter_index": 2,
                    "content": latest_content,
                    "summary": "第二章摘要",
                    "chapter_progress": {
                        "actual_summary": "第二章实际写到搬入新住处",
                        "completed_beats": ["搬入新住处"],
                        "ending_state": {"location": "新住处"},
                    },
                },
            ],
            "memories": [],
            "timeline_entries": [],
            "foreshadowing": [],
            "review_issues": [],
            "sample_style_references": [],
            "research_sources": [],
        }

        messages = build_chapter_prompt(context)
        user_prompt = messages[1]["content"]
        combined_prompt = "\n".join(message["content"] for message in messages)

        self.assertNotIn(old_prefix, user_prompt)
        self.assertIn(latest_content, user_prompt)
        self.assertIn("搬入新住处", user_prompt)
        self.assertIn("找到房东", user_prompt)
        self.assertIn('"chapter_quality_contract"', user_prompt)
        self.assertIn("不要连续铺陈天气、灯光、家具、服装、食物、品牌和生活动作", user_prompt)
        self.assertIn('"dimension":"scene_continuity"', user_prompt)
        self.assertIn("输出前逐项核对 rules", user_prompt)
        self.assertIn("ending_hook 是必须实现的剧情功能", user_prompt)
        self.assertIn("连续两个及以上短句", combined_prompt)
        self.assertIn("是否属于同一语义链", combined_prompt)
        self.assertNotIn("连续切成三个及以上的短句", combined_prompt)
        self.assertIn("尽量少用破折号", combined_prompt)
        self.assertIn("不用破折号代替", combined_prompt)
        self.assertIn("可观察细节", combined_prompt)
        self.assertIn("听者按性格误读/回避/抓错重点", combined_prompt)
        self.assertIn("章际接力", combined_prompt)
        self.assertIn("禁止每个念头机械切成单行", combined_prompt)

    def test_chapter_prompt_uses_precalibrated_word_range_override(self):
        context = {
            "novel": {"title": "测试书", "genre": "现实", "premise": "生存", "brief": {}},
            "story_bible": {},
            "target": {"chapter_index": 2, "task_input": {}},
            "constraints": {"chapter_word_range": {"min": 2200, "max": 3200}},
            "generation_guidance": {
                "chapter_goal": "继续剧情",
                "chapter_word_range": {"min": 2200, "max": 3200},
                "continuity_reminders": [],
                "anti_ai_reminders": [],
            },
            "recent_chapters": [],
            "memories": [],
            "foreshadowing": [],
            "review_issues": [],
        }

        messages = build_chapter_prompt(
            context,
            chapter_word_range_override={"min": 1600, "max": 1800, "unit": "字"},
        )
        combined_prompt = "\n".join(message["content"] for message in messages)

        self.assertIn("1600-1800", combined_prompt)
        self.assertNotIn("2200-3200", combined_prompt)

    def test_comedy_chapter_prompt_enforces_density_and_anti_ledger_rules(self):
        context = {
            "novel": {
                "title": "轻喜剧测试书",
                "genre": "都市、轻喜剧",
                "premise": "两个室友共同生活",
                "brief": {"style_reference": "笑点密集、乐观"},
            },
            "story_bible": {},
            "target": {"chapter_index": 2, "task_input": {}},
            "constraints": {"chapter_word_range": {"min": 2200, "max": 2600}},
            "generation_guidance": {
                "chapter_goal": "处理新的同居麻烦",
                "chapter_word_range": {"min": 2200, "max": 2600},
                "continuity_reminders": [],
                "anti_ai_reminders": [],
            },
            "recent_chapters": [],
            "memories": [],
            "foreshadowing": [],
            "review_issues": [],
        }

        combined_prompt = "\n".join(
            message["content"] for message in build_chapter_prompt(context)
        )

        self.assertIn("4—6个分散的因果型喜剧节拍", combined_prompt)
        self.assertIn("流程逐项写全", combined_prompt)
        self.assertIn('"tone_pacing_contract"', combined_prompt)
        self.assertEqual(combined_prompt.count('"tone_pacing_contract"'), 1)

    def test_chapter_prompt_uses_story_bible_instead_of_repeating_full_brief(self):
        context = {
            "novel": {
                "title": "测试书",
                "genre": "现实",
                "premise": "从旧城出发",
                "brief": {"legacy_duplicate_marker": "不应重复进入正文提示词"},
            },
            "story_bible": {
                "summary": "不需要重复的整书摘要",
                "content": {
                    "main_plot": {"premise": "从旧城出发", "core_conflict": "去留选择"},
                    "world_rules": {"story_era": "当代"},
                    "pacing_plan": {"global_phases": [{"phase": "不应逐章重复的全量阶段"}]},
                },
            },
            "target": {"chapter_index": 1, "task_input": {}},
            "constraints": {"chapter_word_range": {"min": 2200, "max": 2600}},
            "generation_guidance": {
                "chapter_goal": "建立开端",
                "chapter_word_range": {"min": 2200, "max": 2600},
                "continuity_reminders": [],
            },
            "recent_chapters": [],
            "memories": [],
            "timeline_entries": [],
            "foreshadowing": [],
            "review_issues": [],
        }

        combined_prompt = "\n".join(
            message["content"] for message in build_chapter_prompt(context)
        )

        self.assertNotIn("不应重复进入正文提示词", combined_prompt)
        self.assertNotIn("不应逐章重复的全量阶段", combined_prompt)
        self.assertNotIn("不需要重复的整书摘要", combined_prompt)
        self.assertEqual(combined_prompt.count("从旧城出发"), 1)

    def test_chapter_prompt_includes_expression_excerpts_and_copy_policy(self):
        context = {
            "novel": {"title": "测试书", "genre": "现实", "premise": "生存", "brief": {}},
            "story_bible": {},
            "target": {"chapter_index": 2, "task_input": {}},
            "constraints": {"chapter_word_range": {"min": 2200, "max": 2600}},
            "generation_guidance": {
                "chapter_goal": "雨夜对峙",
                "chapter_word_range": {"min": 2200, "max": 2600},
                "continuity_reminders": [],
                "anti_ai_reminders": [],
            },
            "recent_chapters": [],
            "memories": [],
            "foreshadowing": [],
            "review_issues": [],
            "expression_reference_pack": {
                "status": "completed",
                "query": "雨夜对峙",
                "references": [
                    {
                        "passage_id": "p1",
                        "passage_type": "metaphor",
                        "excerpt": "雨水敲着铁皮棚，像有人在门外数一笔旧账。",
                        "technique": "用声音意象把环境压力转成人物心理压力",
                    }
                ],
            },
        }

        combined_prompt = "\n".join(
            message["content"] for message in build_chapter_prompt(context)
        )

        self.assertIn("雨水敲着铁皮棚", combined_prompt)
        self.assertIn("用声音意象", combined_prompt)
        self.assertIn("通用短语可以直接使用", combined_prompt)
        self.assertIn("禁止近似改写原句", combined_prompt)


if __name__ == "__main__":
    unittest.main()
