import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.auto_novel_run import AutoNovelRun
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.services.novel_production_handoff import complete_event_child_handoff
from worker.graphs.novel_production_graph import _create_child_event_task


class AsyncEventHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.novel = Novel(
            id=uuid4(),
            owner_id=uuid4(),
            title="异步事件测试",
            current_chapter_index=4,
            target_words=100_000,
        )
        self.parent = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="produce_novel",
            status="waiting",
            progress=30,
            result_payload={},
            error_message="",
        )
        self.run = AutoNovelRun(
            id=uuid4(),
            novel_id=self.novel.id,
            task_id=self.parent.id,
            status="running",
            stage="waiting_event_child",
            target_words=100_000,
            current_words=10_000,
            produced_event_count=0,
            max_event_count=10,
            last_error="",
            payload={"events": [], "production_mode": "auto"},
        )

    @patch("worker.graphs.novel_production_graph.push_task_to_queue")
    @patch("worker.graphs.novel_production_graph.initialize_task_todo")
    def test_graph_creates_a_real_queued_child_task(self, initialize_todo, push_task) -> None:
        db = MagicMock()

        def assign_id(item) -> None:
            if isinstance(item, GenerationTask) and item.id is None:
                item.id = uuid4()

        db.add.side_effect = assign_id
        child = _create_child_event_task(
            db,
            self.parent,
            self.novel,
            self.run,
            event_index=1,
            chapter_count=8,
            production_pacing={"production_mode": "auto"},
        )

        self.assertEqual(child.status, "queued")
        self.assertEqual(
            child.result_payload["input"]["parent_task_id"],
            str(self.parent.id),
        )
        initialize_todo.assert_called_once()
        push_task.assert_called_once_with(str(child.id))

    @patch("app.services.novel_production_handoff.push_task_to_queue")
    @patch("app.services.novel_production_handoff.emit_task_event")
    @patch("app.services.novel_production_handoff._current_words", return_value=12_345)
    def test_child_completion_requeues_parent(
        self,
        _current_words,
        _emit_event,
        push_task,
    ) -> None:
        child = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="generate_story_event",
            status="completed",
            progress=100,
            result_payload={
                "input": {
                    "parent_task_id": str(self.parent.id),
                    "auto_run_id": str(self.run.id),
                    "production_event_index": 1,
                }
            },
        )
        self.run.payload["pending_child_task_id"] = str(child.id)
        db = MagicMock()
        db.get.side_effect = lambda model, identity: (
            self.parent
            if identity == self.parent.id
            else self.run if identity == self.run.id else None
        )

        result = complete_event_child_handoff(
            db,
            child_task=child,
            output={
                "generated_chapters": [{"word_count": 2_000}],
                "contract": {"status": "success"},
            },
        )

        self.assertTrue(result["parent_requeued"])
        self.assertEqual(self.parent.status, "queued")
        self.assertEqual(self.run.current_words, 12_345)
        self.assertEqual(self.run.payload["pending_child_task_id"], "")
        push_task.assert_called_once()

    @patch("app.services.novel_production_handoff.push_task_to_queue")
    @patch("app.services.novel_production_handoff.emit_task_event")
    @patch("app.services.novel_production_handoff._current_words", return_value=12_345)
    def test_degraded_child_pauses_parent_for_review(
        self,
        _current_words,
        _emit_event,
        push_task,
    ) -> None:
        child = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="generate_story_event",
            status="completed",
            progress=100,
            result_payload={
                "input": {
                    "parent_task_id": str(self.parent.id),
                    "auto_run_id": str(self.run.id),
                    "production_event_index": 1,
                }
            },
        )
        self.run.payload["pending_child_task_id"] = str(child.id)
        db = MagicMock()
        db.get.side_effect = lambda model, identity: (
            self.parent
            if identity == self.parent.id
            else self.run if identity == self.run.id else None
        )

        result = complete_event_child_handoff(
            db,
            child_task=child,
            output={
                "generated_chapters": [],
                "contract": {
                    "status": "degraded",
                    "failure": {"code": "planner_fallback_used"},
                },
            },
        )

        self.assertFalse(result["parent_requeued"])
        self.assertEqual(self.parent.status, "completed")
        self.assertEqual(self.run.status, "paused")
        self.assertEqual(
            self.run.payload["stop_reason"],
            "degraded_output_review_required",
        )
        push_task.assert_not_called()

    @patch("app.services.novel_production_handoff.push_task_to_queue")
    @patch("app.services.novel_production_handoff.emit_task_event")
    @patch("app.services.novel_production_handoff._current_words", return_value=12_345)
    def test_pause_requested_during_child_run_is_not_lost(
        self,
        _current_words,
        _emit_event,
        push_task,
    ) -> None:
        child = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="generate_story_event",
            status="completed",
            progress=100,
            result_payload={
                "input": {
                    "parent_task_id": str(self.parent.id),
                    "auto_run_id": str(self.run.id),
                    "production_event_index": 1,
                }
            },
        )
        self.run.status = "paused"
        self.run.payload.update(
            {
                "pending_child_task_id": str(child.id),
                "pause_requested": True,
            }
        )
        db = MagicMock()
        db.get.side_effect = lambda model, identity: (
            self.parent
            if identity == self.parent.id
            else self.run if identity == self.run.id else None
        )

        result = complete_event_child_handoff(
            db,
            child_task=child,
            output={
                "generated_chapters": [],
                "contract": {"status": "success"},
            },
        )

        self.assertEqual(result["stop_reason"], "paused_after_event")
        self.assertEqual(self.parent.status, "completed")
        self.assertEqual(self.run.status, "paused")
        push_task.assert_not_called()

    @patch("app.services.novel_production_handoff.push_task_to_queue")
    @patch("app.services.novel_production_handoff.emit_task_event")
    def test_replayed_child_completion_is_idempotent(
        self,
        _emit_event,
        push_task,
    ) -> None:
        child = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="generate_story_event",
            status="completed",
            progress=100,
            result_payload={
                "input": {
                    "parent_task_id": str(self.parent.id),
                    "auto_run_id": str(self.run.id),
                }
            },
        )
        self.run.payload.update(
            {
                "pending_child_task_id": "",
                "last_child_task_id": str(child.id),
            }
        )
        db = MagicMock()
        db.get.side_effect = lambda model, identity: (
            self.parent
            if identity == self.parent.id
            else self.run if identity == self.run.id else None
        )

        result = complete_event_child_handoff(
            db,
            child_task=child,
            output={"contract": {"status": "success"}},
        )

        self.assertFalse(result["handled"])
        self.assertEqual(result["reason"], "already_handed_off")
        db.commit.assert_not_called()
        push_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
