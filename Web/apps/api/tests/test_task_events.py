"""任务事件序号并发保护测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.services.task_events import ModelThinkingPublisher, emit_task_event


class TaskEventSequenceTests(unittest.TestCase):
    def test_source_task_row_is_locked_before_sequence_is_allocated(self) -> None:
        db = MagicMock()
        task = SimpleNamespace(id=uuid4(), novel_id=uuid4(), progress=33)
        db.scalar.side_effect = [task.id, 7]

        event = emit_task_event(
            db,
            task,
            title="测试事件",
            commit=False,
        )

        lock_statement = db.scalar.call_args_list[0].args[0]
        compiled = str(
            lock_statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        self.assertIn("FOR UPDATE", compiled)
        self.assertEqual(event.sequence_no, 8)
        db.flush.assert_called_once()

    def test_model_thinking_publisher_coalesces_reasoning_deltas(self) -> None:
        task_id = uuid4()
        task = SimpleNamespace(id=task_id, novel_id=uuid4(), progress=20)
        db = MagicMock()
        db.get.return_value = task
        session = MagicMock()
        session.__enter__.return_value = db
        emitted = []

        publisher = ModelThinkingPublisher(
            task_id,
            source_step_key="chapter_1_draft",
            model_role="writer",
            model="writer-model",
            title="正文模型 · 第 1 章初稿",
            chapter_index=1,
            min_chars=4,
            min_interval_seconds=60,
        )
        with patch("app.db.session.SessionLocal", return_value=session), patch(
            "app.services.task_events.emit_task_event",
            side_effect=lambda *_args, **kwargs: emitted.append(kwargs),
        ):
            publisher.start()
            publisher.append_activity({"attempt": 1, "reasoning_delta": "ab"})
            publisher.append_activity({"attempt": 1, "reasoning_delta": "cd"})
            publisher.finish()

        self.assertEqual(
            [item["event_type"] for item in emitted],
            ["model_thinking_reset", "model_thinking_delta", "model_thinking_end"],
        )
        self.assertEqual(emitted[1]["payload"]["delta"], "abcd")
        self.assertEqual(emitted[1]["payload"]["model_role"], "writer")


if __name__ == "__main__":
    unittest.main()
