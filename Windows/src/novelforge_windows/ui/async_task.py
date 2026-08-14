"""Run blocking model calls away from the Qt UI thread."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class TaskSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class FunctionTask(QRunnable):
    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function()
        except Exception as exc:  # UI boundary: convert domain/network errors to a message.
            self.signals.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


class TaskRunner:
    def __init__(self) -> None:
        self.pool = QThreadPool.globalInstance()

    def submit(
        self,
        function: Callable[[], Any],
        *,
        on_success: Callable[[Any], None],
        on_error: Callable[[str], None],
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        task = FunctionTask(function)
        task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(on_error)
        if on_finished is not None:
            task.signals.finished.connect(on_finished)
        self.pool.start(task)

