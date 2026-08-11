"""Transactional ledger, domain event, outbox, and projection bookkeeping."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .clock import iso_now


_PROJECTION_DEFAULTS: dict[str, tuple[float, int]] = {
    "party-stash": (2.0, 100),
    "session-surface": (5.0, 50),
    "dm-surface": (10.0, 10),
}


def append_event(
    connection: Any,
    *,
    operation_id: str,
    actor_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    destination: str,
) -> int:
    now = iso_now()
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    connection.execute(
        "INSERT INTO ledger_entries(id, operation_id, actor_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), operation_id, actor_id, event_type, serialized, now),
    )
    event = connection.execute(
        "INSERT INTO domain_events(operation_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
        (operation_id, event_type, serialized, now),
    )
    sequence = int(event.lastrowid)
    connection.execute(
        """INSERT INTO event_outbox(
            destination, event_type, payload, status, next_attempt_at
        ) VALUES (?, ?, ?, 'PENDING', ?)""",
        (destination, event_type, json.dumps({"sequence": sequence, **payload}, sort_keys=True), now),
    )
    return sequence


def session_event_destination(connection: Any, session_id: str | None = None) -> str:
    """Resolve an event destination to a durable session identity."""
    if session_id is None:
        active = connection.execute(
            "SELECT id FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
        session_id = str(active["id"]) if active is not None else None
    return f"session:{session_id}" if session_id is not None else "session:unassigned"


def mark_projection_dirty(connection: Any, *, target_id: str, target_type: str, destination: str) -> None:
    now = iso_now()
    freshness_budget, priority = _PROJECTION_DEFAULTS.get(target_id, (5.0, 0))
    revision = int(connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM domain_events").fetchone()[0])
    connection.execute(
        """INSERT INTO projection_targets(
            target_id, target_type, destination, desired_revision, dirty_since,
            freshness_budget_seconds, priority, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_id) DO UPDATE SET
            destination = excluded.destination,
            dirty_since = COALESCE(projection_targets.dirty_since, excluded.dirty_since),
            desired_revision = MAX(projection_targets.desired_revision, excluded.desired_revision),
            updated_at = excluded.updated_at""",
        (target_id, target_type, destination, revision, now, freshness_budget, priority, now),
    )
