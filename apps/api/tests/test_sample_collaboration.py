import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException

from app.api.sample_collaboration import update_sample_annotation
from app.models.sample_collaboration import SampleAnnotation, SampleTextSegment
from app.schemas.sample_analysis import SampleAnnotationUpdate
from app.services.sample_collaboration import (
    ANNOTATION_CATEGORIES,
    annotation_read_payloads,
    build_annotation_hash,
    load_and_validate_selection,
    load_selection_context,
    selection_context_from_content,
    validate_categories,
)


class SampleCollaborationTests(unittest.TestCase):
    def test_categories_are_fixed_deduplicated_and_bounded(self):
        categories = validate_categories(["humor", "dialogue", "humor"])
        self.assertEqual(categories, ["humor", "dialogue"])
        self.assertIn("abstract", ANNOTATION_CATEGORIES)
        with self.assertRaises(HTTPException):
            validate_categories(["自定义污染分类"])

    @patch("app.services.sample_collaboration.read_text_object")
    def test_selection_must_match_minio_source_exactly(self, read_text_object):
        read_text_object.return_value = "甲说了一句很离谱的话。乙沉默了。"
        segment = SimpleNamespace(source_object_key="segments/1.txt")
        selected = load_and_validate_selection(
            segment,
            start_offset=0,
            end_offset=11,
            quote_text="甲说了一句很离谱的话。",
        )
        self.assertEqual(selected, "甲说了一句很离谱的话。")
        with self.assertRaises(HTTPException):
            load_and_validate_selection(
                segment,
                start_offset=0,
                end_offset=11,
                quote_text="客户端伪造的原句",
            )

    def test_annotation_hash_is_category_order_independent(self):
        first = build_annotation_hash("原句", ["humor", "dialogue"], "说明")
        second = build_annotation_hash("原句", ["dialogue", "humor"], "说明")
        self.assertEqual(first, second)

    def test_annotation_update_contract_accepts_reselected_text(self):
        payload = SampleAnnotationUpdate(
            version=3,
            start_offset=12,
            end_offset=20,
            quote_text="重新选择",
            categories=["dialogue"],
            note="保留原说明",
        )
        self.assertEqual(payload.start_offset, 12)
        self.assertEqual(payload.end_offset, 20)
        self.assertEqual(payload.quote_text, "重新选择")

    def test_annotation_update_persists_reselected_range(self):
        annotation_id = uuid4()
        analysis_id = uuid4()
        segment_id = uuid4()
        user_id = uuid4()
        annotation = SimpleNamespace(
            id=annotation_id,
            sample_analysis_id=analysis_id,
            segment_id=segment_id,
            creator_id=user_id,
            start_offset=10,
            end_offset=30,
            quote_text="原来的较长选区",
            categories=["humor"],
            note="原说明",
            status="trusted_private",
            trust_score=1.0,
            content_hash="old-hash",
            embedding=[0.1],
            embedding_model="test",
            version=2,
        )
        segment = SimpleNamespace(id=segment_id, sample_analysis_id=analysis_id)
        analysis = SimpleNamespace(id=analysis_id, visibility="private")
        event = SimpleNamespace()
        db = MagicMock()
        db.get.side_effect = lambda model, _object_id: (
            annotation if model is SampleAnnotation else segment if model is SampleTextSegment else None
        )
        payload = SampleAnnotationUpdate(
            version=2,
            start_offset=10,
            end_offset=16,
            quote_text="新选区文本",
            categories=["dialogue"],
            note="保留后的说明",
        )

        with (
            patch("app.api.sample_collaboration.get_accessible_sample", return_value=analysis),
            patch("app.api.sample_collaboration.load_and_validate_selection", return_value="新选区文本"),
            patch("app.api.sample_collaboration.emit_collaboration_event", return_value=event),
            patch("app.api.sample_collaboration.publish_collaboration_event"),
            patch("app.api.sample_collaboration._enqueue_annotation_index"),
            patch(
                "app.api.sample_collaboration.annotation_read_payload",
                return_value={"id": annotation_id, "quote_text": "新选区文本"},
            ),
        ):
            result = update_sample_annotation(
                annotation_id=annotation_id,
                payload=payload,
                current_user=SimpleNamespace(id=user_id),
                db=db,
            )

        self.assertEqual(annotation.start_offset, 10)
        self.assertEqual(annotation.end_offset, 16)
        self.assertEqual(annotation.quote_text, "新选区文本")
        self.assertEqual(annotation.version, 3)
        self.assertEqual(result["quote_text"], "新选区文本")
        revision = db.add.call_args_list[0].args[0]
        self.assertEqual(revision.snapshot["end_offset"], 30)
        self.assertEqual(revision.snapshot["quote_text"], "原来的较长选区")

    @patch("app.services.sample_collaboration.read_text_object")
    def test_note_context_is_limited_to_nearby_text(self, read_text_object):
        read_text_object.return_value = "前" * 500 + "人工选区" + "后" * 500
        segment = SimpleNamespace(source_object_key="segments/2.txt")
        selected, context = load_selection_context(
            segment,
            start_offset=500,
            end_offset=504,
            quote_text="人工选区",
        )
        self.assertEqual(selected, "人工选区")
        self.assertEqual(len(context), 644)
        self.assertTrue(context.startswith("前" * 320))
        self.assertTrue(context.endswith("后" * 320))

    def test_batch_context_reuses_loaded_chapter_without_changing_selection(self):
        selected, context = selection_context_from_content(
            "开头。人工标注句。结尾。",
            start_offset=3,
            end_offset=9,
            quote_text="人工标注句。",
        )
        self.assertEqual(selected, "人工标注句。")
        self.assertIn(selected, context)

    def test_annotation_list_batches_related_queries(self):
        current_user_id = uuid4()
        other_user_id = uuid4()
        first_id = uuid4()
        second_id = uuid4()
        segment_id = uuid4()
        analysis_id = uuid4()
        creator = SimpleNamespace(id=other_user_id, display_name="标注者")
        annotations = [
            SimpleNamespace(
                id=first_id,
                sample_analysis_id=analysis_id,
                segment_id=segment_id,
                creator_id=other_user_id,
                start_offset=1,
                end_offset=3,
                quote_text="第一条",
                categories=["humor"],
                note="说明",
                status="trusted",
                trust_score=0.8,
                version=1,
                created_at=None,
                updated_at=None,
            ),
            SimpleNamespace(
                id=second_id,
                sample_analysis_id=analysis_id,
                segment_id=segment_id,
                creator_id=other_user_id,
                start_offset=4,
                end_offset=7,
                quote_text="第二条",
                categories=["dialogue"],
                note="",
                status="pending",
                trust_score=0.35,
                version=1,
                created_at=None,
                updated_at=None,
            ),
        ]
        votes_result = MagicMock()
        votes_result.all.return_value = [
            SimpleNamespace(annotation_id=first_id, user_id=current_user_id, value=1),
            SimpleNamespace(annotation_id=first_id, user_id=other_user_id, value=-1),
        ]
        creators_result = MagicMock()
        creators_result.all.return_value = [creator]
        reports_result = MagicMock()
        reports_result.all.return_value = [(first_id, 2)]
        db = MagicMock()
        db.scalars.side_effect = [votes_result, creators_result]
        db.execute.return_value = reports_result

        payloads = annotation_read_payloads(
            db,
            annotations,
            SimpleNamespace(id=current_user_id),
        )

        self.assertEqual(db.scalars.call_count, 2)
        db.execute.assert_called_once()
        self.assertEqual(payloads[0]["creator_name"], "标注者")
        self.assertEqual(payloads[0]["upvotes"], 1)
        self.assertEqual(payloads[0]["downvotes"], 1)
        self.assertEqual(payloads[0]["report_count"], 2)
        self.assertEqual(payloads[0]["current_user_vote"], 1)
        self.assertEqual(payloads[1]["upvotes"], 0)


if __name__ == "__main__":
    unittest.main()
