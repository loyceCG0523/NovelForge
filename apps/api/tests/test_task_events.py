"""任务事件序号并发保护测试。"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.services.task_events import emit_task_event


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


if __name__ == "__main__":
    unittest.main()
