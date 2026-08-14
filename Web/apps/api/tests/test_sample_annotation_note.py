import unittest
from threading import Lock
from time import sleep
from unittest.mock import patch

from app.services.llm_client import LLMConfig
from app.services.sample_annotation_note import (
    AnnotationNoteGenerationError,
    AnnotationNoteDraft,
    AnnotationNoteJob,
    generate_annotation_note,
    generate_annotation_notes_parallel,
)


class SampleAnnotationNoteTests(unittest.TestCase):
    def test_regular_annotation_uses_model_without_web_search(self):
        config = LLMConfig(
            base_url="https://example.test/v1",
            api_key="secret",
            model="review-model",
        )
        with (
            patch(
                "app.services.sample_annotation_note.build_review_llm_config",
                return_value=config,
            ),
            patch(
                "app.services.sample_annotation_note.build_llm_config",
                return_value=None,
            ),
            patch("app.services.sample_annotation_note.LLMClient") as client_type,
        ):
            client_type.return_value.complete_text.return_value = (
                "反问和落差把人物关系写得很鲜明。可复用为先认真铺垫，再用短句拆台。"
            )
            draft = generate_annotation_note(
                preferences={},
                quote_text="你最多有张身份证。",
                surrounding_text="朋友提醒他注意身份，他立刻拆台。",
                categories=["humor", "dialogue"],
            )

        self.assertEqual(draft.provider, "review_model")
        self.assertFalse(draft.used_web_search)
        self.assertIn("短句拆台", draft.note)
        client_type.return_value.search_web.assert_not_called()

    def test_internet_meme_forces_deepseek_web_search_and_builds_template(self):
        config = LLMConfig(
            base_url="https://api.deepseek.com",
            api_key="secret",
            model="deepseek-v4-flash",
            reasoning_effort="low",
        )
        sources = [
            {
                "title": "网络词语说明",
                "url": "https://example.test/meme",
                "snippet": "该表达常用于夸张地自我肯定。",
            }
        ]
        with (
            patch(
                "app.services.sample_annotation_note.build_review_llm_config",
                return_value=config,
            ),
            patch(
                "app.services.sample_annotation_note.build_llm_config",
                return_value=None,
            ),
            patch("app.services.sample_annotation_note.LLMClient") as client_type,
        ):
            client_type.return_value.search_web.return_value = sources
            client_type.return_value.complete_text.return_value = (
                "梗义：用夸张复读表达突然的自我认同。"
                "此处：角色把小优势喊成天赋，形成反差。"
                "通用模板：发现微小长处后，用重复感叹把它升级成惊人身份。"
            )
            draft = generate_annotation_note(
                preferences={},
                quote_text="甜菜！老子就是甜菜！！",
                surrounding_text="角色刚发现自己似乎有一点天赋。",
                categories=["internet_meme", "humor"],
            )

        self.assertEqual(draft.provider, "deepseek_web_search")
        self.assertTrue(draft.used_web_search)
        self.assertEqual(draft.source_count, 1)
        self.assertIn("通用模板：", draft.note)
        client_type.return_value.search_web.assert_called_once()
        search_query = client_type.return_value.search_web.call_args.args[0]
        self.assertIn("甜菜", search_query)

    def test_internet_meme_never_falls_back_to_non_deepseek_model(self):
        config = LLMConfig(
            base_url="https://example.test/v1",
            api_key="secret",
            model="review-model",
        )
        with (
            patch(
                "app.services.sample_annotation_note.build_review_llm_config",
                return_value=config,
            ),
            patch(
                "app.services.sample_annotation_note.build_llm_config",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(
                AnnotationNoteGenerationError,
                "DeepSeek 官方 deepseek-v4-flash",
            ):
                generate_annotation_note(
                    preferences={},
                    quote_text="抽象热梗",
                    surrounding_text="上下文",
                    categories=["internet_meme"],
                )

    def test_generated_note_is_capped_at_three_sentences(self):
        config = LLMConfig(
            base_url="https://example.test/v1",
            api_key="secret",
            model="review-model",
        )
        with (
            patch(
                "app.services.sample_annotation_note.build_review_llm_config",
                return_value=config,
            ),
            patch(
                "app.services.sample_annotation_note.build_llm_config",
                return_value=None,
            ),
            patch("app.services.sample_annotation_note.LLMClient") as client_type,
        ):
            client_type.return_value.complete_text.return_value = "第一句。第二句。第三句。多余句。"
            draft = generate_annotation_note(
                preferences={},
                quote_text="原句",
                surrounding_text="上下文",
                categories=["dialogue"],
            )

        self.assertEqual(draft.note, "第一句。第二句。第三句。")

    def test_batch_generation_is_parallel_ordered_and_failure_isolated(self):
        active = 0
        max_active = 0
        lock = Lock()

        def fake_generate(*, quote_text, **_kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                sleep(0.03)
                if quote_text == "失败原句":
                    raise RuntimeError("单条失败")
                return AnnotationNoteDraft(
                    note=f"说明：{quote_text}",
                    provider="review_model",
                    model="review-model",
                    used_web_search=False,
                )
            finally:
                with lock:
                    active -= 1

        jobs = [
            AnnotationNoteJob("a", "第一条", "上下文", ["dialogue"]),
            AnnotationNoteJob("b", "失败原句", "上下文", ["humor"]),
            AnnotationNoteJob("c", "第三条", "上下文", ["pacing"]),
        ]
        with patch(
            "app.services.sample_annotation_note.generate_annotation_note",
            side_effect=fake_generate,
        ):
            results = generate_annotation_notes_parallel(
                preferences={},
                jobs=jobs,
                max_workers=4,
            )

        self.assertEqual([item.annotation_id for item in results], ["a", "b", "c"])
        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(results[1].error, "单条失败")
        self.assertEqual(results[2].draft.note, "说明：第三条")


if __name__ == "__main__":
    unittest.main()
