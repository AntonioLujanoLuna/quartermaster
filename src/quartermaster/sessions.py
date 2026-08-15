"""Minimal session lifecycle and continuity operations."""

from __future__ import annotations

import uuid
from typing import Any

from .clock import iso_now
from .combat import CombatService
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty
from .handles import HandleRepository
from .loot import LootDropService
from .receipts import ReceiptRepository, ReceiptResult


class SessionError(RuntimeError):
    """Raised when a session lifecycle precondition fails."""


class SessionService:
    def __init__(
        self,
        store: SQLiteStore,
        receipts: ReceiptRepository | None = None,
        loot_drops: LootDropService | None = None,
        combat: CombatService | None = None,
    ) -> None:
        self.store = store
        self.receipts = receipts
        self.loot_drops = loot_drops or LootDropService(store, receipts or ReceiptRepository(store), HandleRepository(store))
        self.combat = combat or CombatService(store, receipts)

    def start_session(self, *, session_number: int | None = None, operation_id: str | None = None) -> dict[str, Any]:
        operation_id = operation_id or str(uuid.uuid4())
        with self.store.transaction() as connection:
            active = connection.execute(
                "SELECT id, session_number FROM sessions WHERE status = 'ACTIVE'"
            ).fetchone()
            if active is not None:
                return {
                    "status": "ACTIVE_EXISTS",
                    "active_session_id": active["id"],
                    "active_session_number": active["session_number"],
                }
            if session_number is None:
                session_number = int(connection.execute("SELECT COALESCE(MAX(session_number), 0) + 1 FROM sessions").fetchone()[0])
            session_id = str(uuid.uuid4())
            now = iso_now()
            connection.execute(
                "INSERT INTO sessions(id, session_number, status, started_at) VALUES (?, ?, 'ACTIVE', ?)",
                (session_id, session_number, now),
            )
            append_event(
                connection,
                operation_id=operation_id,
                actor_id=None,
                event_type="SESSION_STARTED",
                payload={"session_id": session_id, "session_number": session_number},
                destination=f"session:{session_id}",
            )
            mark_projection_dirty(connection, target_id="session-surface", target_type="STATE", destination=f"session:{session_id}")
            return {"status": "STARTED", "session_id": session_id, "session_number": session_number}

    def end_session(self, session_id: str, *, where_ended: str | None = None, operation_id: str | None = None) -> dict[str, Any]:
        operation_id = operation_id or str(uuid.uuid4())
        with self.store.transaction() as connection:
            return self._end_in_transaction(connection, operation_id, session_id, where_ended)

    def start_interaction(self, interaction_id: str, *, actor_id: str | None = None) -> ReceiptResult:
        if self.receipts is None:
            raise SessionError("receipt repository is required for interaction operations")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="session",
            mutation=lambda connection, operation_id: self._start_in_transaction(connection, operation_id),
        )

    def end_interaction(self, interaction_id: str, *, actor_id: str | None, where_ended: str) -> ReceiptResult:
        if self.receipts is None:
            raise SessionError("receipt repository is required for interaction operations")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="session",
            mutation=lambda connection, operation_id: self._end_active_in_transaction(connection, operation_id, where_ended),
        )

    def _start_in_transaction(self, connection: Any, operation_id: str) -> dict[str, Any]:
        active = connection.execute("SELECT id, session_number FROM sessions WHERE status = 'ACTIVE'").fetchone()
        if active is not None:
            return {"status": "ACTIVE_EXISTS", "active_session_id": active["id"], "active_session_number": active["session_number"]}
        session_number = int(connection.execute("SELECT COALESCE(MAX(session_number), 0) + 1 FROM sessions").fetchone()[0])
        session_id = str(uuid.uuid4())
        now = iso_now()
        connection.execute("INSERT INTO sessions(id, session_number, status, started_at) VALUES (?, ?, 'ACTIVE', ?)", (session_id, session_number, now))
        append_event(connection, operation_id=operation_id, actor_id=None, event_type="SESSION_STARTED", payload={"session_id": session_id, "session_number": session_number}, destination=f"session:{session_id}")
        mark_projection_dirty(connection, target_id="session-surface", target_type="STATE", destination=f"session:{session_id}")
        return {"status": "STARTED", "session_id": session_id, "session_number": session_number}

    def _end_active_in_transaction(self, connection: Any, operation_id: str, where_ended: str) -> dict[str, Any]:
        active = connection.execute("SELECT id FROM sessions WHERE status = 'ACTIVE'").fetchone()
        if active is None:
            return {"status": "NO_ACTIVE_SESSION"}
        return self._end_in_transaction(connection, operation_id, active["id"], where_ended)

    def _end_in_transaction(self, connection: Any, operation_id: str, session_id: str, where_ended: str | None) -> dict[str, Any]:
        session = connection.execute("SELECT * FROM sessions WHERE id = ? AND status = 'ACTIVE'", (session_id,)).fetchone()
        if session is None:
            raise SessionError("active session not found")
        now = iso_now()
        connection.execute(
            "UPDATE sessions SET status = 'CLOSED', ended_at = ?, where_ended = ? WHERE id = ? AND status = 'ACTIVE'",
            (now, where_ended, session_id),
        )
        closed_combats = self.combat.close_session_encounters(
            connection, session_id=session_id, operation_id=operation_id
        )
        closed_drops = self.loot_drops.close_session_drops(connection, session_id=session_id, operation_id=operation_id)
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=None,
            event_type="SESSION_CLOSED",
            payload={"session_id": session_id, "session_number": session["session_number"], "where_ended": where_ended},
            destination=f"session:{session_id}",
        )
        mark_projection_dirty(connection, target_id="session-surface", target_type="STATE", destination=f"session:{session_id}")
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {
            "status": "CLOSED",
            "session_id": session_id,
            "session_number": session["session_number"],
            "closed_drops": closed_drops,
            "closed_combats": closed_combats,
        }
