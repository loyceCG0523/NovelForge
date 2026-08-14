"""结构化起始需求与时间线数据的回归测试。"""

import unittest
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError

from app.schemas.novel import NovelCreate
from app.services.story_bible_builder import normalize_story_bible
from app.services.timeline_service import _normalize_entries


def valid_brief() -> dict:
    return {
        "story_era": "1990年代上海",
        "story_location": "上海",
        "characters": [
            {
                "key": "lin",
                "name": "林予安",
                "gender": "男",
                "age": "25",
                "occupation": "报社编辑",
                "is_protagonist": True,
                "goal": "查清旧案",
                "detailed_setting": "谨慎、敏锐",
            }
        ],
    }


class StructuredBriefTests(unittest.TestCase):
    def test_era_and_character_required_fields_are_enforced(self) -> None:
        brief = valid_brief()
        brief["characters"][0]["occupation"] = ""
        with self.assertRaises(ValidationError):
            NovelCreate(title="测试", brief=brief)

    def test_user_facts_survive_story_bible_normalization(self) -> None:
        novel = SimpleNamespace(
            title="测试",
            genre="都市",
            target_words=100000,
            premise="旧案重启",
            brief=valid_brief(),
        )
        normalized = normalize_story_bible(
            {"world_rules": {"story_era": "2020年代"}, "main_characters": [], "main_plot": {"planned_events": []}},
            novel,
        )
        self.assertEqual(normalized["world_rules"]["story_era"], "1990年代上海")
        self.assertEqual(normalized["main_characters"][0]["occupation"], "报社编辑")
        self.assertEqual(normalized["main_plot"]["planned_events"][0]["source"], "system_generated")
        self.assertEqual(normalized["main_plot"]["planned_events"][0]["event_type"], "opening_incident")
        self.assertEqual(normalized["narrative_contract"]["primary_genre"], "都市")
        self.assertIn("只作为", normalized["narrative_contract"]["supporting_element_policy"])


class TimelineNormalizationTests(unittest.TestCase):
    def test_chapter_timeline_inherits_era_and_stable_sequence(self) -> None:
        novel = SimpleNamespace(id=uuid4(), brief=valid_brief())
        chapter = SimpleNamespace(id=uuid4(), chapter_index=6)
        records = _normalize_entries(
            [{"story_day": 3, "time_expression": "第三天上午", "summary": "主角去报社", "participants": ["林予安"]}],
            novel,
            chapter,
            None,
        )
        self.assertEqual(records[0]["sequence_no"], 601)
        self.assertEqual(records[0]["era"], "1990年代上海")
        self.assertEqual(records[0]["story_day"], 3)


if __name__ == "__main__":
    unittest.main()
