"""Atomic initial interaction response state."""

from __future__ import annotations

from enum import StrEnum
from threading import Lock
from time import monotonic
from typing import Any


class ResponseState(StrEnum):
    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    RESPONDED = "RESPONDED"
    DEFERRED = "DEFERRED"


class ResponseStateError(RuntimeError):
    """Raised when an interaction response is attempted twice."""


class ResponseController:
    def __init__(self, *, started_at: float | None = None, soft_deadline_seconds: float = 1.2) -> None:
        self._lock = Lock()
        self._state = ResponseState.UNACKNOWLEDGED
        self._started_at = monotonic() if started_at is None else started_at
        self.soft_deadline_seconds = soft_deadline_seconds

    @property
    def state(self) -> ResponseState:
        with self._lock:
            return self._state

    def respond(self, payload: Any) -> Any:
        with self._lock:
            if self._state is not ResponseState.UNACKNOWLEDGED:
                raise ResponseStateError(f"cannot respond from {self._state}")
            self._state = ResponseState.RESPONDED
            return payload

    def defer(self) -> None:
        with self._lock:
            if self._state is not ResponseState.UNACKNOWLEDGED:
                raise ResponseStateError(f"cannot defer from {self._state}")
            self._state = ResponseState.DEFERRED

    def should_fallback_to_deferred(self, *, can_defer: bool, write_active: bool, now: float | None = None) -> bool:
        elapsed = (monotonic() if now is None else now) - self._started_at
        with self._lock:
            return (
                self._state is ResponseState.UNACKNOWLEDGED
                and can_defer
                and not write_active
                and elapsed >= self.soft_deadline_seconds
            )
