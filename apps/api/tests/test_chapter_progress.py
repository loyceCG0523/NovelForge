import unittest

from app.services.chapter_progress import (
    apply_revised_next_plan,
    compact_next_chapter_boundary,
    compact_story_event_for_chapter,
    normalize_chapter_progress,
)


class ChapterProgressTests(unittest.TestCase):
    def test_normalize_progress_keeps_actual_ending_and_rebases_next_plan(self):
        current_plan = {"chapter_index": 1, "core_event": "收到催租通知"}
        next_plan = {
            "chapter_index": 2,
            "title": "寻找住处",
            "function": "推进",
            "core_event": "寻找廉价住处并搬家",
            "character_beats": ["向同学求助"],
            "foreshadowing_actions": [],
            "ending_hook": "发现异常房东",
        }
        raw = {
            "actual_summary": "主角收到催租通知，并已经找到房间搬入。",
            "completed_beats": ["收到催租通知", "收拾行李"],
            "ending_state": {
                "story_time": "当晚",
                "location": "新住处",
                "characters": {"主角": "已经搬入"},
                "open_actions": ["调查房东"],
                "final_scene": "门外传来脚步声。",
            },
            "consumed_next_beats": ["寻找住处并搬家"],
            "revised_next_chapter_plan": {
                "chapter_index": 999,
                "title": "陌生脚步",
                "core_event": "调查新住处的异常声响",
                "ending_hook": "门锁自行转动",
            },
        }

        progress = normalize_chapter_progress(
            raw,
            summary="旧计划摘要",
            content="正文结尾",
            current_plan=current_plan,
            next_plan=next_plan,
        )

        self.assertEqual(progress["actual_summary"], raw["actual_summary"])
        self.assertEqual(progress["ending_state"]["location"], "新住处")
        self.assertEqual(progress["revised_next_chapter_plan"]["chapter_index"], 2)
        self.assertEqual(
            progress["revised_next_chapter_plan"]["core_event"],
            "调查新住处的异常声响",
        )

        updated = apply_revised_next_plan([current_plan, next_plan], 1, progress)
        self.assertEqual(updated[1]["chapter_index"], 2)
        self.assertEqual(updated[1]["title"], "陌生脚步")
        self.assertEqual(next_plan["title"], "寻找住处")

    def test_does_not_rebase_without_consumed_next_beats(self):
        next_plan = {"chapter_index": 2, "title": "原计划", "core_event": "原事件"}
        progress = normalize_chapter_progress(
            {"revised_next_chapter_plan": {"title": "不应采用"}},
            summary="实际摘要",
            content="实际正文",
            current_plan={"chapter_index": 1},
            next_plan=next_plan,
        )

        self.assertEqual(progress["revised_next_chapter_plan"], {})
        self.assertEqual(apply_revised_next_plan([next_plan], 0, progress)[0]["title"], "原计划")

    def test_compact_story_event_removes_nested_chapter_plans(self):
        compacted = compact_story_event_for_chapter(
            {
                "event_title": "搬家事件",
                "event_goal": "完成搬家",
                "chapter_plans": [{"core_event": "不应重复进入章节 Prompt"}],
                "research": {"large": "payload"},
            }
        )

        self.assertEqual(compacted["event_title"], "搬家事件")
        self.assertNotIn("chapter_plans", compacted)
        self.assertNotIn("research", compacted)

    def test_compact_boundaries_keep_supporting_agenda_and_comedy_plan(self):
        event = compact_story_event_for_chapter(
            {
                "event_title": "早餐店改造",
                "supporting_character_roles": [
                    {"name": "陶桂枝", "agenda": "保住早餐店"}
                ],
            }
        )
        chapter = compact_next_chapter_boundary(
            {
                "chapter_index": 9,
                "state_change": "林予安决定接下改造项目",
                "comedy_beats": ["乔弥把免费试吃改成盲测"],
                "compressed_processes": ["登记手续一句带过"],
            }
        )

        self.assertEqual(event["supporting_character_roles"][0]["name"], "陶桂枝")
        self.assertIn("盲测", chapter["comedy_beats"][0])
        self.assertIn("登记手续", chapter["compressed_processes"][0])


if __name__ == "__main__":
    unittest.main()
