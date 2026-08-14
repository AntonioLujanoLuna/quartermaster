"""Fair state projection scheduling and FIFO event delivery."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from .clock import iso_now
from .currency import currency_from_row
from .db import SQLiteStore
from .transport import DiscordTransport, RateLimitedError

_MAX_RETRY_BACKOFF_SECONDS = 300.0


def _plus_seconds(timestamp: str, seconds: float) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def render_state(store: SQLiteStore, target_id: str) -> dict[str, Any]:
    with store.read() as connection:
        if target_id == "party-stash":
            rows = connection.execute(
            """SELECT item_name, quantity, provenance
               FROM inventory_stacks
              WHERE owner_type = 'PARTY' AND owner_id = 'party'
              ORDER BY last_acquired_at DESC, item_name"""
            ).fetchall()
            drops = connection.execute(
            """SELECT loot.id, loot.expires_at, item.item_name, item.remaining_quantity
                 FROM loot_drops AS loot
                 JOIN loot_drop_items AS item ON item.drop_id = loot.id
                WHERE loot.status = 'OPEN' AND item.remaining_quantity > 0
                ORDER BY loot.created_at, item.created_at"""
            ).fetchall()
            drop_payload: dict[str, list[dict[str, Any]]] = {}
            for drop in drops:
                drop_payload.setdefault(drop["id"], []).append(
                    {"item_name": drop["item_name"], "remaining": drop["remaining_quantity"], "expires_at": drop["expires_at"]}
                )
            treasury = connection.execute(
                "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
            ).fetchone()
            return {
                "surface": "PARTY STASH",
                "treasury": currency_from_row(treasury) if treasury else None,
                "items": [
                    {"item_name": row["item_name"], "quantity": row["quantity"], "provenance": row["provenance"]}
                    for row in rows
                ],
                "loot_drops": [{"drop_id": drop_id, "items": items} for drop_id, items in drop_payload.items()],
            }
        if target_id == "session-surface":
            active = connection.execute(
            "SELECT session_number, started_at FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
            previous = connection.execute(
            "SELECT session_number, where_ended FROM sessions WHERE status = 'CLOSED' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
            return {
                "surface": "SESSION",
                "active": dict(active) if active else None,
                "previous": dict(previous) if previous else None,
            }
        if target_id == "dm-surface":
            return {
                "surface": "QUARTERMASTER",
                "active_session_count": connection.execute("SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE'").fetchone()[0],
                "stash_count": connection.execute("SELECT COUNT(*) FROM inventory_stacks WHERE owner_type = 'PARTY'").fetchone()[0],
            }
        raise ValueError(f"unknown state projection target: {target_id}")


class StateProjectionScheduler:
    def __init__(self, store: SQLiteStore, transport: DiscordTransport, *, now: Callable[[], str] = iso_now) -> None:
        self.store = store
        self.transport = transport
        self.now = now

    def run_once(self) -> bool:
        target = self._claim_next_target()
        if target is None:
            return False
        target_id = target["target_id"]
        try:
            payload = render_state(self.store, target_id)
            message_id = self.transport.upsert_state(target_id, target["destination"], payload, target["discord_message_id"])
        except RateLimitedError as error:
            self._record_failure(target_id, f"rate limited: {error}", error.retry_after_seconds)
            return False
        except Exception as error:
            self._record_failure(target_id, str(error), 1.0)
            return False
        self._record_success(target, message_id)
        return True

    async def run_once_async(self, transport: Any | None = None) -> bool:
        """Deliver one state target through an async transport."""
        target = await asyncio.to_thread(self._claim_next_target)
        if target is None:
            return False
        target_id = target["target_id"]
        transport = transport or self.transport
        try:
            payload = await asyncio.to_thread(render_state, self.store, target_id)
            result = transport.upsert_state(target_id, target["destination"], payload, target["discord_message_id"])
            message_id = await result if inspect.isawaitable(result) else result
        except RateLimitedError as error:
            await asyncio.to_thread(
                self._record_failure, target_id, f"rate limited: {error}", error.retry_after_seconds
            )
            return False
        except Exception as error:
            await asyncio.to_thread(self._record_failure, target_id, str(error), 1.0)
            return False
        await asyncio.to_thread(self._record_success, target, message_id)
        return True

    def _record_success(self, target: Any, message_id: str) -> None:
        """Retire only the revision this delivery actually rendered.

        The revision is the one captured when the target was claimed. Re-reading
        `desired_revision` here instead would credit this payload with any
        mutation committed during the Discord round-trip and clear `dirty_since`
        along with it, so that change would never be rendered and health would
        still report a clean projection.
        """
        target_id = target["target_id"]
        now = self.now()
        rendered = int(target["desired_revision"])
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE projection_targets
                   SET discord_message_id = ?, delivered_revision = MAX(delivered_revision, ?),
                       dirty_since = CASE WHEN desired_revision <= ? THEN NULL ELSE dirty_since END,
                       in_flight = 0, next_attempt_at = NULL, last_error = NULL, updated_at = ?
                 WHERE target_id = ?""",
                (message_id, rendered, rendered, now, target_id),
            )

    def _claim_next_target(self) -> Any:
        now = self.now()
        with self.store.transaction() as connection:
            row = connection.execute(
                """SELECT *,
                          ((julianday(?) - julianday(dirty_since)) / freshness_budget_seconds) AS normalized_lateness
                     FROM projection_targets
                    WHERE target_type = 'STATE'
                      AND dirty_since IS NOT NULL
                      AND in_flight = 0
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY normalized_lateness DESC, priority DESC, dirty_since ASC
                    LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE projection_targets SET in_flight = 1, updated_at = ? WHERE target_id = ? AND in_flight = 0",
                (now, row["target_id"]),
            )
            return row

    def _record_failure(self, target_id: str, message: str, retry_after: float) -> None:
        now = self.now()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE projection_targets SET in_flight = 0, next_attempt_at = ?, last_error = ?, updated_at = ? WHERE target_id = ?",
                (_plus_seconds(now, retry_after), message, now, target_id),
            )


class EventOutboxWorker:
    def __init__(
        self,
        store: SQLiteStore,
        transport: DiscordTransport,
        *,
        now: Callable[[], str] = iso_now,
        max_failures: int = 8,
    ) -> None:
        if max_failures <= 0:
            raise ValueError("max_failures must be positive")
        self.store = store
        self.transport = transport
        self.now = now
        self.max_failures = max_failures

    def run_once(self) -> bool:
        event = self._next_event()
        if event is None:
            return False
        try:
            self.transport.deliver_event(event["destination"], event["event_type"], json.loads(event["payload"]))
        except RateLimitedError as error:
            self._retry(event["id"], f"rate limited: {error}", error.retry_after_seconds, rate_limited=True)
            return False
        except Exception as error:
            self._retry(event["id"], str(error), 1.0)
            return False
        self._mark_delivered(event["id"])
        return True

    async def run_once_async(self, transport: Any | None = None) -> bool:
        """Deliver one event through an async transport."""
        event = await asyncio.to_thread(self._next_event)
        if event is None:
            return False
        transport = transport or self.transport
        try:
            result = transport.deliver_event(event["destination"], event["event_type"], json.loads(event["payload"]))
            if inspect.isawaitable(result):
                await result
        except RateLimitedError as error:
            await asyncio.to_thread(
                partial(
                    self._retry,
                    event["id"],
                    f"rate limited: {error}",
                    error.retry_after_seconds,
                    rate_limited=True,
                )
            )
            return False
        except Exception as error:
            await asyncio.to_thread(self._retry, event["id"], str(error), 1.0)
            return False
        await asyncio.to_thread(self._mark_delivered, event["id"])
        return True

    def _mark_delivered(self, event_id: int) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE event_outbox SET status = 'DELIVERED', delivered_at = ?, last_error = NULL WHERE id = ?",
                (self.now(), event_id),
            )

    def _next_event(self) -> Any:
        now = self.now()
        with self.store.read() as connection:
            return connection.execute(
                """SELECT event.*
                     FROM event_outbox AS event
                    WHERE event.status = 'PENDING'
                      AND event.next_attempt_at <= ?
                      AND NOT EXISTS (
                          SELECT 1 FROM event_outbox AS earlier
                           WHERE earlier.destination = event.destination
                             AND earlier.id < event.id
                             AND earlier.status = 'PENDING'
                      )
                    ORDER BY event.id
                    LIMIT 1""",
                (now,),
            ).fetchone()

    def _retry(self, event_id: int, message: str, retry_after: float, *, rate_limited: bool = False) -> None:
        """Reschedule a failed delivery, or dead-letter it once it looks poisoned.

        Rate limits are the transport working as intended, so they are retried at
        the delay Discord asks for and never count toward the failure budget. Hard
        failures back off exponentially and are capped: without a terminal state,
        one permanently undeliverable event holds the per-destination FIFO gate
        shut and blocks every later event to that thread forever.
        """
        now = self.now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT failure_count FROM event_outbox WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return
            failure_count = int(row["failure_count"]) + (0 if rate_limited else 1)
            if not rate_limited and failure_count >= self.max_failures:
                connection.execute(
                    """UPDATE event_outbox
                          SET status = 'FAILED', attempt_count = attempt_count + 1,
                              failure_count = ?, last_error = ?, failed_at = ?
                        WHERE id = ?""",
                    (failure_count, message, now, event_id),
                )
                return
            delay = (
                retry_after
                if rate_limited
                else min(retry_after * (2 ** (failure_count - 1)), _MAX_RETRY_BACKOFF_SECONDS)
            )
            connection.execute(
                """UPDATE event_outbox
                      SET attempt_count = attempt_count + 1, failure_count = ?,
                          next_attempt_at = ?, last_error = ?
                    WHERE id = ?""",
                (failure_count, _plus_seconds(now, delay), message, event_id),
            )
