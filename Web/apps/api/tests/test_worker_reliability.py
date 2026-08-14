import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.generation_task import GenerationTask
from worker import main as worker_main


class FakeRedis:
    def __init__(self, task_id: str | None = None) -> None:
        self.task_id = task_id
        self.processing: list[str] = []
        self.ready: list[str] = []
        self.removed: list[tuple[str, int, str]] = []

    def blmove(
        self,
        _source: str,
        destination: str,
        timeout: int,
        src: str,
        dest: str,
    ) -> str | None:
        del timeout
        if src != "LEFT" or dest != "RIGHT":
            raise AssertionError("queue consumption must preserve FIFO ordering")
        if self.task_id is not None:
            self.processing.append(self.task_id)
        return self.task_id

    def lrange(self, _queue: str, _start: int, _end: int) -> list[str]:
        return list(self.processing)

    def lrem(self, queue: str, count: int, value: str) -> int:
        self.removed.append((queue, count, value))
        self.processing = [item for item in self.processing if item != value]
        return 1

    def lpos(self, queue: str, value: str):
        values = self.ready if not queue.endswith(":processing") else self.processing
        try:
            return values.index(value)
        except ValueError:
            return None

    def rpush(self, _queue: str, value: str) -> int:
        self.ready.append(value)
        return len(self.ready)


class WorkerReliabilityTests(unittest.TestCase):
    def test_successful_execution_is_acknowledged_after_db_work(self) -> None:
        task_id = str(uuid4())
        redis = FakeRedis(task_id)
        db = MagicMock()

        with (
            patch.object(worker_main, "SessionLocal", return_value=nullcontext(db)),
            patch.object(worker_main, "execute_task", return_value=True) as execute,
        ):
            consumed = worker_main.consume_once(redis, "agent_tasks")

        self.assertTrue(consumed)
        execute.assert_called_once_with(db, task_id)
        self.assertEqual(
            redis.removed,
            [("agent_tasks:processing", 1, task_id)],
        )

    def test_process_failure_before_ack_leaves_task_in_processing_queue(self) -> None:
        task_id = str(uuid4())
        redis = FakeRedis(task_id)
        db = MagicMock()

        with (
            patch.object(worker_main, "SessionLocal", return_value=nullcontext(db)),
            patch.object(worker_main, "execute_task", side_effect=RuntimeError("crash")),
        ):
            with self.assertRaises(RuntimeError):
                worker_main.consume_once(redis, "agent_tasks")

        self.assertEqual(redis.removed, [])
        self.assertEqual(redis.processing, [task_id])

    def test_expired_processing_lease_is_requeued(self) -> None:
        task = GenerationTask(
            id=uuid4(),
            novel_id=uuid4(),
            task_type="generate_chapter",
            status="running",
            progress=42,
            result_payload={},
        )
        task.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        redis = FakeRedis()
        redis.processing = [str(task.id)]
        db = MagicMock()
        db.get.return_value = task
        db.scalars.side_effect = [
            MagicMock(all=MagicMock(return_value=[])),
            MagicMock(all=MagicMock(return_value=[task])),
        ]

        with patch.object(worker_main, "SessionLocal", return_value=nullcontext(db)):
            result = worker_main.recover_worker_queue(redis, "agent_tasks")

        self.assertEqual(result["recovered"], 1)
        self.assertEqual(task.status, "queued")
        self.assertIn(str(task.id), redis.ready)
        self.assertEqual(
            task.result_payload["queue_recovery"]["reason"],
            "worker_lease_expired",
        )


if __name__ == "__main__":
    unittest.main()
