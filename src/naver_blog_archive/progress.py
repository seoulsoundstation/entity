from __future__ import annotations

from collections.abc import Callable
from threading import Event


class OperationCancelled(KeyboardInterrupt):
    """A cooperative stop that leaves saved work available to the next run."""

    def __init__(self, message: str = '사용자가 중단했습니다.'):
        super().__init__(message)
        self.report: dict | None = None


class TaskControl:
    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self._cancelled = Event()
        self.on_event = on_event

    def cancel(self) -> None:
        self._cancelled.set()

    def check(self) -> None:
        if self._cancelled.is_set():
            raise OperationCancelled()

    def wait(self, seconds: float) -> None:
        if self._cancelled.wait(max(0, seconds)):
            raise OperationCancelled()

    def emit(self, phase: str, message: str, **fields) -> None:
        if self.on_event is not None:
            self.on_event({'phase': phase, 'message': message, **fields})
