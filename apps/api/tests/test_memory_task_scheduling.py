import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.chapter import Chapter
from app.models.generation_task import GenerationTask
from app.models.novel import Novel
from app.services.agent_orchestrator import (
    aggregate_memory_batch_progress,
    enqueue_chapter_memory_tasks,
    retry_chapter_memory_task,
)


class MemoryTaskSchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.novel = Novel(
            id=uuid4(),
            owner_id=uuid4(),
            title="测试作品",
            genre="都市",
            status="draft",
            current_chapter_index=1,
        )
        self.chapter = Chapter(
            id=uuid4(),
            novel_id=self.novel.id,
            chapter_index=1,
            title="第一章",
            status="done",
            content="第一段。\n\n第二段。",
            word_count=8,
        )
        self.source_task = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            task_type="generate_story_event",
            status="running",
            progress=22,
        )
        self.db = MagicMock()
        self.db.scalars.return_value.all.return_value = []

        def assign_id(item) -> None:
            if isinstance(item, GenerationTask) and item.id is None:
                item.id = uuid4()

        self.db.add.side_effect = assign_id

    def test_resync_replaces_chapter_version_without_increasing_total(self) -> None:
        created_at = datetime.now(timezone.utc)
        first = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            chapter_id=self.chapter.id,
            task_type="sync_chapter_memory",
            status="completed",
            result_payload={"input": {"chapter_id": str(self.chapter.id)}},
        )
        first.created_at = created_at
        latest = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            chapter_id=self.chapter.id,
            task_type="sync_chapter_memory",
            status="completed",
            result_payload={"input": {"chapter_id": str(self.chapter.id)}},
        )
        latest.created_at = created_at + timedelta(seconds=1)

        progress = aggregate_memory_batch_progress([first, latest], expected_total=8)

        self.assertEqual(progress["total"], 8)
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(progress["resync_count"], 1)
        self.assertEqual(progress["pending"], 7)

    @patch("app.services.agent_orchestrator.push_task_to_queue")
    @patch("app.services.agent_orchestrator.emit_task_event")
    def test_chapter_is_enqueued_with_current_progress_and_content_hash(
        self,
        emit_event,
        push_task,
    ) -> None:
        tasks = enqueue_chapter_memory_tasks(
            self.db,
            novel=self.novel,
            chapters=[self.chapter],
            source_task=self.source_task,
            source_progress=24,
            sync_reason="chapter_review_complete",
        )

        self.assertEqual(len(tasks), 1)
        task_input = tasks[0].result_payload["input"]
        self.assertEqual(task_input["source_progress"], 24)
        self.assertEqual(task_input["sync_reason"], "chapter_review_complete")
        self.assertEqual(len(task_input["content_hash"]), 64)
        push_task.assert_called_once()
        self.assertTrue(
            any(call.kwargs.get("progress") == 24 for call in emit_event.call_args_list)
        )

    @patch("app.services.agent_orchestrator.push_task_to_queue")
    @patch("app.services.agent_orchestrator.emit_task_event")
    def test_same_source_and_content_version_is_not_enqueued_twice(
        self,
        _emit_event,
        push_task,
    ) -> None:
        first = enqueue_chapter_memory_tasks(
            self.db,
            novel=self.novel,
            chapters=[self.chapter],
            source_task=self.source_task,
        )
        self.db.scalars.return_value.all.return_value = first

        duplicate = enqueue_chapter_memory_tasks(
            self.db,
            novel=self.novel,
            chapters=[self.chapter],
            source_task=self.source_task,
            sync_reason="event_revision_resync",
        )

        self.assertEqual(duplicate, [])
        self.assertEqual(push_task.call_count, 1)

    @patch("app.services.agent_orchestrator.push_task_to_queue")
    @patch("app.services.agent_orchestrator.emit_task_event")
    def test_changed_chapter_content_enqueues_event_revision_resync(
        self,
        _emit_event,
        push_task,
    ) -> None:
        first = enqueue_chapter_memory_tasks(
            self.db,
            novel=self.novel,
            chapters=[self.chapter],
            source_task=self.source_task,
        )
        self.db.scalars.return_value.all.return_value = first
        self.chapter.content += "\n\n事件级修订新增内容。"

        resync = enqueue_chapter_memory_tasks(
            self.db,
            novel=self.novel,
            chapters=[self.chapter],
            source_task=self.source_task,
            sync_reason="event_revision_resync",
        )

        self.assertEqual(len(resync), 1)
        self.assertEqual(
            resync[0].result_payload["input"]["sync_reason"],
            "event_revision_resync",
        )
        self.assertEqual(push_task.call_count, 2)

    @patch("app.services.agent_orchestrator.push_task_to_queue")
    @patch("app.services.agent_orchestrator.emit_task_event")
    def test_retry_uses_single_chapter_batch_total(
        self,
        _emit_event,
        push_task,
    ) -> None:
        failed_task = GenerationTask(
            id=uuid4(),
            novel_id=self.novel.id,
            chapter_id=self.chapter.id,
            task_type="sync_chapter_memory",
            status="failed",
            progress=100,
            result_payload={
                "input": {
                    "source_task_id": str(self.source_task.id),
                    "expected_total": 8,
                    "source_progress": 22,
                }
            },
        )

        retry = retry_chapter_memory_task(
            self.db,
            failed_task=failed_task,
            source_task=self.source_task,
            chapter=self.chapter,
        )

        self.assertEqual(retry.result_payload["input"]["expected_total"], 1)
        push_task.assert_called_once()

    @patch(
        "app.services.agent_orchestrator.push_task_to_queue",
        side_effect=ConnectionError("redis unavailable"),
    )
    @patch("app.services.agent_orchestrator.emit_task_event")
    def test_redis_notification_failure_does_not_lose_persisted_memory_task(
        self,
        _emit_event,
        push_task,
    ) -> None:
        tasks = enqueue_chapter_memory_tasks(
            self.db,
            novel=self.novel,
            chapters=[self.chapter],
            source_task=self.source_task,
        )

        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0].result_payload["queue_notification_retryable"])
        self.assertEqual(
            tasks[0].result_payload["queue_notification_error"],
            "redis unavailable",
        )
        push_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
