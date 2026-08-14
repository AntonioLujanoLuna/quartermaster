"""Quartermaster's own record that a combat is happening.

Avrae owns initiative, HP, conditions, resources, and every mechanical result.
Quartermaster owns the fact that the table is in a fight: which session it
belongs to, which channel it is running in, when it opened, when it closed, and
what the DM said about how it ended. That is enough to answer "what is going on
right now" and to hand the spoils into the Loot Drop and Party Stash workflows
without holding a single number Avrae is authoritative for.

Nothing here talks to Avrae. Opening a Quartermaster combat does not begin an
Avrae combat, and closing one does not end it; the DM still runs the native
command. The record tracks the table's own workflow alongside Avrae's.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty
from .receipts import ReceiptRepository, ReceiptResult


class CombatError(RuntimeError):
    """Raised when a combat lifecycle precondition fails."""


def elapsed_seconds(start: str | None, end: str | None) -> float | None:
    """Seconds between two stored timestamps, or None if either is unreadable.

    A duration is decoration on every surface that shows it, so a timestamp that
    predates the current format — or that an operator edited by hand — degrades
    to "no duration shown" rather than taking down the card that carries it.
    """
    if not start or not end:
        return None
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max((ended - started).total_seconds(), 0.0)


def format_duration(seconds: float | None) -> str | None:
    """Render a duration the way a person at the table would say it."""
    if seconds is None:
        return None
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if remainder < 30 else f"{minutes + 1}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


class CombatService:
    """Open, close, and report the table's current combat."""

    def __init__(self, store: SQLiteStore, receipts: ReceiptRepository | None = None) -> None:
        self.store = store
        self.receipts = receipts

    # Interactions --------------------------------------------------------

    def open_interaction(self, interaction_id: str, *, actor_id: str | None, channel_id: str) -> ReceiptResult:
        if self.receipts is None:
            raise CombatError("receipt repository is required for interaction operations")
        if not channel_id.strip():
            raise CombatError("a channel is required to open combat")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="combat",
            mutation=lambda connection, operation_id: self._open_in_transaction(
                connection, operation_id, actor_id, channel_id.strip()
            ),
        )

    def close_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        outcome: str | None = None,
    ) -> ReceiptResult:
        if self.receipts is None:
            raise CombatError("receipt repository is required for interaction operations")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="combat",
            mutation=lambda connection, operation_id: self._close_active_in_transaction(
                connection, operation_id, actor_id, outcome
            ),
        )

    # Reads ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Quartermaster's own view of the fight, with no Avrae state in it."""
        with self.store.read() as connection:
            session = connection.execute(
                "SELECT id, session_number FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
            if session is None:
                return {"status": "NO_ACTIVE_SESSION", "session_number": None, "encounter": None, "last_closed": None, "open_drops": []}
            session_id = str(session["id"])
            now = iso_now()
            open_row = connection.execute(
                "SELECT * FROM combat_encounters WHERE session_id = ? AND status = 'OPEN'",
                (session_id,),
            ).fetchone()
            last_closed_row = connection.execute(
                """SELECT * FROM combat_encounters
                    WHERE session_id = ? AND status = 'CLOSED'
                 ORDER BY closed_at DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            return {
                "status": "OPEN" if open_row is not None else "NO_OPEN_COMBAT",
                "session_id": session_id,
                "session_number": int(session["session_number"]),
                "encounter": self._encounter_view(open_row, now) if open_row is not None else None,
                "last_closed": self._encounter_view(last_closed_row, now) if last_closed_row is not None else None,
                "open_drops": self._open_drops(connection, session_id),
            }

    # Transactional bodies -------------------------------------------------

    def _open_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        channel_id: str,
    ) -> dict[str, Any]:
        session = connection.execute(
            "SELECT id, session_number FROM sessions WHERE status = 'ACTIVE'"
        ).fetchone()
        if session is None:
            return {"status": "NO_ACTIVE_SESSION"}
        session_id = str(session["id"])
        existing = connection.execute(
            "SELECT * FROM combat_encounters WHERE session_id = ? AND status = 'OPEN'",
            (session_id,),
        ).fetchone()
        now = iso_now()
        if existing is not None:
            return {
                "status": "ALREADY_OPEN",
                "session_number": int(session["session_number"]),
                **self._encounter_view(existing, now),
            }
        encounter_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO combat_encounters(
                id, session_id, channel_id, status, opened_by, opened_at
            ) VALUES (?, ?, ?, 'OPEN', ?, ?)""",
            (encounter_id, session_id, channel_id, actor_id, now),
        )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="COMBAT_OPENED",
            payload={
                "encounter_id": encounter_id,
                "session_id": session_id,
                "session_number": int(session["session_number"]),
                "channel_id": channel_id,
            },
            destination=f"session:{session_id}",
        )
        mark_projection_dirty(
            connection, target_id="session-surface", target_type="STATE", destination=f"session:{session_id}"
        )
        return {
            "status": "OPENED",
            "session_id": session_id,
            "session_number": int(session["session_number"]),
            "encounter_id": encounter_id,
            "channel_id": channel_id,
            "opened_at": now,
            "opened_by": actor_id,
            "elapsed_seconds": 0.0,
        }

    def _close_active_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        outcome: str | None,
    ) -> dict[str, Any]:
        session = connection.execute(
            "SELECT id, session_number FROM sessions WHERE status = 'ACTIVE'"
        ).fetchone()
        if session is None:
            return {"status": "NO_ACTIVE_SESSION"}
        session_id = str(session["id"])
        closed = self._close_in_transaction(
            connection,
            operation_id,
            session_id=session_id,
            actor_id=actor_id,
            outcome=outcome,
            reason="MANUAL",
        )
        if closed is None:
            return {
                "status": "NO_OPEN_COMBAT",
                "session_id": session_id,
                "session_number": int(session["session_number"]),
                "open_drops": self._open_drops(connection, session_id),
            }
        return {
            "status": "CLOSED",
            "session_number": int(session["session_number"]),
            "open_drops": self._open_drops(connection, session_id),
            **closed,
        }

    def _close_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        *,
        session_id: str,
        actor_id: str | None,
        outcome: str | None,
        reason: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM combat_encounters WHERE session_id = ? AND status = 'OPEN'",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        now = iso_now()
        normalized_outcome = (outcome or "").strip() or None
        connection.execute(
            """UPDATE combat_encounters
                  SET status = 'CLOSED', closed_at = ?, closed_by = ?, closed_reason = ?, outcome = ?
                WHERE id = ? AND status = 'OPEN'""",
            (now, actor_id, reason, normalized_outcome, row["id"]),
        )
        duration = elapsed_seconds(row["opened_at"], now)
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="COMBAT_CLOSED",
            payload={
                "encounter_id": row["id"],
                "session_id": session_id,
                "channel_id": row["channel_id"],
                "reason": reason,
                "outcome": normalized_outcome,
                "elapsed_seconds": duration,
            },
            destination=f"session:{session_id}",
        )
        mark_projection_dirty(
            connection, target_id="session-surface", target_type="STATE", destination=f"session:{session_id}"
        )
        return {
            "encounter_id": str(row["id"]),
            "session_id": session_id,
            "channel_id": str(row["channel_id"]),
            "opened_at": str(row["opened_at"]),
            "opened_by": row["opened_by"],
            "closed_at": now,
            "closed_reason": reason,
            "outcome": normalized_outcome,
            "elapsed_seconds": duration,
        }

    def close_session_encounters(self, connection: Any, *, session_id: str, operation_id: str) -> int:
        """Close any combat still open when its session closes.

        An encounter outlives nothing: the status card reads through the active
        session, so an encounter left OPEN on a CLOSED session would never be
        visible again and would block the next session's combat from opening
        cleanly if the session were ever reopened.
        """
        closed = self._close_in_transaction(
            connection,
            operation_id,
            session_id=session_id,
            actor_id=None,
            outcome=None,
            reason="SESSION_CLOSED",
        )
        return 0 if closed is None else 1

    # Helpers -------------------------------------------------------------

    @staticmethod
    def _encounter_view(row: Any, now: str) -> dict[str, Any]:
        closed_at = row["closed_at"]
        return {
            "encounter_id": str(row["id"]),
            "channel_id": str(row["channel_id"]),
            "opened_at": str(row["opened_at"]),
            "opened_by": row["opened_by"],
            "closed_at": closed_at,
            "closed_by": row["closed_by"],
            "closed_reason": row["closed_reason"],
            "outcome": row["outcome"],
            "elapsed_seconds": elapsed_seconds(row["opened_at"], closed_at or now),
            "closed_seconds_ago": elapsed_seconds(closed_at, now) if closed_at else None,
        }

    @staticmethod
    def _open_drops(connection: Any, session_id: str) -> list[dict[str, Any]]:
        """Loot still outstanding in this session, for the closeout handoff."""
        rows = connection.execute(
            """SELECT loot.id AS drop_id,
                      COUNT(item.id) AS item_count,
                      COALESCE(SUM(item.remaining_quantity), 0) AS remaining_quantity
                 FROM loot_drops AS loot
                 LEFT JOIN loot_drop_items AS item
                        ON item.drop_id = loot.id AND item.remaining_quantity > 0
                WHERE loot.session_id = ? AND loot.status = 'OPEN'
             GROUP BY loot.id
             ORDER BY loot.created_at""",
            (session_id,),
        ).fetchall()
        return [
            {
                "drop_id": str(row["drop_id"]),
                "item_count": int(row["item_count"]),
                "remaining_quantity": int(row["remaining_quantity"]),
            }
            for row in rows
        ]
