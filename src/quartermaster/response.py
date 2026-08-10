"""Atomic initial interaction response state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from time import monotonic
from typing import Any, Callable

from .receipts import ReceiptRepository, ReceiptResult


class ResponseState(StrEnum):
    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    RESPONDED = "RESPONDED"
    DEFERRED = "DEFERRED"


class ResponseStateError(RuntimeError):
    """Raised when an interaction response is attempted twice."""


@dataclass(frozen=True)
class FastExecutionResult:
    value: Any
    deferred: bool


@dataclass(frozen=True)
class DeferredExecutionResult:
    receipt: ReceiptResult
    deferred: bool


class DeferredExecutionError(RuntimeError):
    """Raised after a deferred operation has been durably marked FAILED."""

    def __init__(self, receipt: ReceiptResult) -> None:
        super().__init__(receipt.logical_response.get("message", "deferred operation failed"))
        self.receipt = receipt


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


async def execute_fast(
    interaction: Any,
    operation: Callable[[], Any],
    *,
    soft_deadline_seconds: float = 1.2,
    write_active: Callable[[], bool] | None = None,
    ephemeral: bool = False,
) -> FastExecutionResult:
    """Run bounded local work without blocking Discord's acknowledgement loop."""
    if soft_deadline_seconds <= 0:
        raise ValueError("soft deadline must be positive")
    controller = ResponseController(soft_deadline_seconds=soft_deadline_seconds)
    task = asyncio.create_task(asyncio.to_thread(operation))
    done, _ = await asyncio.wait({task}, timeout=soft_deadline_seconds)
    active = write_active() if write_active is not None else False
    if not done and not active:
        controller.defer()
        await interaction.response.defer(ephemeral=ephemeral)
        return FastExecutionResult(await task, True)

    value = await task
    controller.respond(value)
    return FastExecutionResult(value, False)


async def execute_deferred(
    interaction: Any,
    receipts: ReceiptRepository,
    operation: Callable[[], Any],
    *,
    actor_id: str | None,
    response_kind: str,
    ephemeral: bool = False,
) -> DeferredExecutionResult:
    """Persist PROCESSING before acknowledgement and finish in a worker thread."""
    interaction_id = str(interaction.id)
    initial = await asyncio.to_thread(
        receipts.begin_deferred,
        interaction_id,
        actor_id=actor_id,
        response_kind=response_kind,
    )
    if initial.status != "PROCESSING":
        return DeferredExecutionResult(initial, False)

    await interaction.response.defer(ephemeral=ephemeral)
    try:
        value = await asyncio.to_thread(operation)
    except Exception as error:
        failure = await asyncio.to_thread(
            receipts.fail_deferred,
            interaction_id,
            {"error": "DEFERRED_OPERATION_FAILED", "message": str(error), "retryable": True},
        )
        raise DeferredExecutionError(failure) from error
    committed = await asyncio.to_thread(receipts.commit_deferred, interaction_id, value)
    return DeferredExecutionResult(committed, True)
