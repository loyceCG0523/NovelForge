import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.generation_graph_checkpoint import GenerationGraphCheckpoint
from app.models.generation_task import GenerationTask
from app.services.graph_checkpoint import save_graph_checkpoint


class GraphCheckpointTests(unittest.TestCase):
    def test_checkpoint_is_created_and_versioned(self) -> None:
        task = GenerationTask(
            id=uuid4(),
            novel_id=uuid4(),
            task_type="produce_novel",
            status="running",
        )
        db = MagicMock()
        db.scalar.return_value = None

        created = save_graph_checkpoint(
            db,
            task=task,
            graph_name="NovelProductionGraph",
            node_name="produce_next_event",
            state={"produced_event_count": 1},
        )

        self.assertIsInstance(created, GenerationGraphCheckpoint)
        self.assertEqual(created.version, 1)
        self.assertEqual(created.state["produced_event_count"], 1)
        db.add.assert_called_once_with(created)

        db.scalar.return_value = created
        updated = save_graph_checkpoint(
            db,
            task=task,
            graph_name="NovelProductionGraph",
            node_name="finalize",
            state={"stop_reason": "waiting_event_child"},
            status="waiting",
        )

        self.assertIs(updated, created)
        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.status, "waiting")


if __name__ == "__main__":
    unittest.main()
