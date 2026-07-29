import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.auto_novel_run import AutoNovelRun
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from worker.graphs.novel_production_graph import run_novel_production_graph


class NovelProductionGraphIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.novel = Novel(
            id=uuid4(),
            owner_id=uuid4(),
            title="Graph 集成测试",
            target_words=100_000,
            current_chapter_index=0,
            status="draft",
        )
        self.task = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="produce_novel",
            status="running",
            progress=10,
            error_message="",
            result_payload={"input": {}},
        )
        self.run = AutoNovelRun(
            id=uuid4(),
            novel_id=self.novel.id,
            task_id=self.task.id,
            status="running",
            stage="initializing",
            target_words=100_000,
            current_words=0,
            produced_event_count=0,
            max_event_count=5,
            last_error="",
            payload={"events": [], "production_mode": "auto"},
        )
        self.task.result_payload["input"]["auto_run_id"] = str(self.run.id)
        self.child = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="generate_story_event",
            status="queued",
            progress=0,
            result_payload={},
        )
        self.db = MagicMock()
        self.db.get.side_effect = lambda model, identity: (
            self.run
            if model is AutoNovelRun
            else self.child if model is GenerationTask and identity == self.child.id else None
        )

    def _patches(self, persisted=None):
        return (
            patch(
                "worker.graphs.novel_production_graph.get_story_bible_context",
                return_value={"content": {}},
            ),
            patch(
                "worker.graphs.novel_production_graph._current_word_count",
                return_value=self.run.current_words,
            ),
            patch(
                "worker.graphs.novel_production_graph._narrative_completion_snapshot",
                return_value={
                    "narrative_closed": False,
                    "open_foreshadowing_count": 0,
                },
            ),
            patch(
                "worker.graphs.novel_production_graph._create_child_event_task",
                return_value=self.child,
            ),
            patch(
                "worker.graphs.novel_production_graph.emit_task_event",
            ),
            patch(
                "worker.graphs.novel_production_graph.load_graph_checkpoint",
                return_value=persisted,
            ),
            patch(
                "worker.graphs.novel_production_graph.save_graph_checkpoint",
            ),
        )

    def test_full_graph_yields_after_scheduling_async_child(self) -> None:
        patches = self._patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3] as create_child,
            patches[4],
            patches[5],
            patches[6] as save_checkpoint,
        ):
            output = run_novel_production_graph(
                db=self.db,
                task=self.task,
                novel=self.novel,
            )

        self.assertTrue(output["deferred"])
        self.assertEqual(output["child_task_id"], str(self.child.id))
        self.assertEqual(output["contract"]["status"], "waiting")
        create_child.assert_called_once()
        self.assertGreaterEqual(save_checkpoint.call_count, 4)

    def test_persisted_waiting_checkpoint_resumes_after_child_handoff(self) -> None:
        self.run.produced_event_count = 1
        self.run.current_words = 8_000
        self.run.payload = {
            "events": [{"child_task_id": str(uuid4())}],
            "production_mode": "auto",
            "pending_child_task_id": "",
        }
        persisted = SimpleNamespace(
            node_name="finalize",
            state={
                "auto_run_id": str(self.run.id),
                "produced_event_count": 0,
                "stop_reason": "waiting_event_child",
                "pending_child_task_id": str(uuid4()),
            },
        )
        patches = self._patches(persisted)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3] as create_child,
            patches[4],
            patches[5],
            patches[6],
        ):
            output = run_novel_production_graph(
                db=self.db,
                task=self.task,
                novel=self.novel,
            )

        self.assertTrue(output["deferred"])
        self.assertEqual(
            create_child.call_args.kwargs["event_index"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
