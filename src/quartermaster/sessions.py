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
from .narrative import render_entry
from .receipts import ReceiptRepository, ReceiptResult

#: How much of the last session a continuity surface reads back. This is a
#: recap, not the record: the export holds every line, and a table that has to
#: scroll to find where it stopped is being handed the ledger, not a reminder.
CONTINUITY_RECAP_LINES = 8


#: A recording link is somebody pasting a URL, so it is bounded like every other
#: typed field here rather than trusted for being a URL.
RECORDING_URL_LIMIT = 500


class SessionError(RuntimeError):
    """Raised when a session lifecycle precondition fails."""


def normalize_recording_url(value: str | None) -> str | None:
    """Check a pasted recording link, or refuse it.

    The table already uses a recorder; where the recording ended up was going
    into a channel message that scrolls away, while the one thing that does not
    scroll away — where the evening stopped — is written down. This is the other
    half of that sentence.

    Only http and https. Every surface that shows this renders it as a link, and
    a `javascript:` URL rendered as a link is a script the next person to open
    the continuity panel runs. Whether the link resolves is not checked and
    cannot be: Quartermaster makes no network calls outside the adapter, and a
    recording that has not finished uploading yet is still the right answer.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > RECORDING_URL_LIMIT:
        raise SessionError(f"the recording link must be at most {RECORDING_URL_LIMIT} characters")
    lowered = text.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise SessionError("the recording link must start with http:// or https://")
    # A link is rendered into Discord messages and into a web page. Neither
    # should have to guess where one ends.
    if any(character.isspace() for character in text):
        raise SessionError("the recording link must not contain spaces")
    return text


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

    def end_session(
        self,
        session_id: str,
        *,
        where_ended: str | None = None,
        recording_url: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = operation_id or str(uuid.uuid4())
        with self.store.transaction() as connection:
            return self._end_in_transaction(connection, operation_id, session_id, where_ended, recording_url)

    def continuity(self, *, limit: int = CONTINUITY_RECAP_LINES) -> dict[str, Any]:
        """What the table needs to pick up where it left off.

        The product is a continuity companion and this is the question it was
        built to answer: where did we stop, and what had happened by then. The
        endpoint is the one narrative line End Session asks a DM for; the recap
        is derived, because everything else the table could want was already
        written down as it happened.

        The window is the last session that was closed — the one they played —
        and the lines are the end of it, because "where did we stop" is a
        question about the end of an evening. `total` is how many lines that
        session actually holds, so a surface can say what it is not showing.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.store.read() as connection:
            active = connection.execute(
                "SELECT session_number, started_at FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
            previous = connection.execute(
                """SELECT session_number, started_at, ended_at, where_ended, recording_url
                     FROM sessions WHERE status = 'CLOSED'
                    ORDER BY session_number DESC LIMIT 1"""
            ).fetchone()
            recap: list[str] = []
            total = 0
            if previous is not None:
                # The played session's span, read the way the export reads it:
                # `ledger_entries` carries no session, but its timestamps and
                # the session's come from the same clock in the same format,
                # and a closed session is bounded at both ends by its own rows.
                window: list[Any] = [previous["started_at"]]
                clause = "created_at >= ?"
                if active is not None:
                    clause += " AND created_at < ?"
                    window.append(active["started_at"])
                total = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM ledger_entries WHERE {clause}", tuple(window)
                    ).fetchone()[0]
                )
                rows = connection.execute(
                    f"""SELECT event_type, payload FROM ledger_entries
                         WHERE {clause}
                      ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                    (*window, limit),
                ).fetchall()
                recap = [render_entry(row["event_type"], row["payload"]) for row in reversed(rows)]
        return {
            "active_session_number": int(active["session_number"]) if active else None,
            "previous": dict(previous) if previous else None,
            "recap": recap,
            "recap_total": total,
        }

    def start_interaction(self, interaction_id: str, *, actor_id: str | None = None) -> ReceiptResult:
        if self.receipts is None:
            raise SessionError("receipt repository is required for interaction operations")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="session",
            mutation=lambda connection, operation_id: self._start_in_transaction(connection, operation_id),
        )

    def end_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        where_ended: str,
        recording_url: str | None = None,
    ) -> ReceiptResult:
        if self.receipts is None:
            raise SessionError("receipt repository is required for interaction operations")
        recording = normalize_recording_url(recording_url)
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="session",
            mutation=lambda connection, operation_id: self._end_active_in_transaction(connection, operation_id, where_ended, recording),
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

    def _end_active_in_transaction(
        self, connection: Any, operation_id: str, where_ended: str, recording_url: str | None = None
    ) -> dict[str, Any]:
        active = connection.execute("SELECT id FROM sessions WHERE status = 'ACTIVE'").fetchone()
        if active is None:
            return {"status": "NO_ACTIVE_SESSION"}
        return self._end_in_transaction(connection, operation_id, active["id"], where_ended, recording_url)

    def _end_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        session_id: str,
        where_ended: str | None,
        recording_url: str | None = None,
    ) -> dict[str, Any]:
        session = connection.execute("SELECT * FROM sessions WHERE id = ? AND status = 'ACTIVE'", (session_id,)).fetchone()
        if session is None:
            raise SessionError("active session not found")
        now = iso_now()
        recording = normalize_recording_url(recording_url)
        connection.execute(
            "UPDATE sessions SET status = 'CLOSED', ended_at = ?, where_ended = ?, recording_url = ? WHERE id = ? AND status = 'ACTIVE'",
            (now, where_ended, recording, session_id),
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
            payload={
                "session_id": session_id,
                "session_number": session["session_number"],
                "where_ended": where_ended,
                "recording_url": recording,
            },
            destination=f"session:{session_id}",
        )
        mark_projection_dirty(connection, target_id="session-surface", target_type="STATE", destination=f"session:{session_id}")
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {
            "status": "CLOSED",
            "session_id": session_id,
            "session_number": session["session_number"],
            "recording_url": recording,
            "closed_drops": closed_drops,
            "closed_combats": closed_combats,
        }
