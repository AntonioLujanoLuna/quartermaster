"""Character records and lifecycle transitions."""

from __future__ import annotations

import uuid
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, session_event_destination
from .receipts import ReceiptRepository, ReceiptResult


CHARACTER_LIFECYCLES = ("ACTIVE", "DEAD", "RETIRED", "DEPARTED")
_ALLOWED_TRANSITIONS = {
    "ACTIVE": {"DEAD", "RETIRED", "DEPARTED"},
    "DEAD": {"ACTIVE"},
    "RETIRED": {"ACTIVE"},
    "DEPARTED": {"ACTIVE"},
}


class CharacterError(RuntimeError):
    """Raised for invalid character or lifecycle operations."""


class CharacterService:
    def __init__(self, store: SQLiteStore, receipts: ReceiptRepository) -> None:
        self.store = store
        self.receipts = receipts

    def list_characters(self) -> list[dict[str, Any]]:
        with self.store.connection_lock:
            rows = self.store._require_connection().execute(
                """SELECT id, name, discord_user_id, lifecycle, created_at, updated_at
                     FROM characters
                    ORDER BY name, id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        name: str,
        discord_user_id: str | None = None,
    ) -> ReceiptResult:
        normalized_name = " ".join(name.split())
        if not normalized_name:
            raise CharacterError("character name is required")
        if discord_user_id is not None:
            discord_user_id = discord_user_id.strip() or None
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="character",
            mutation=lambda connection, operation_id: self._create_in_transaction(
                connection, operation_id, actor_id, normalized_name, discord_user_id
            ),
        )

    def _create_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        name: str,
        discord_user_id: str | None,
    ) -> dict[str, Any]:
        character_id = str(uuid.uuid4())
        now = iso_now()
        connection.execute(
            """INSERT INTO characters(id, name, discord_user_id, lifecycle, created_at, updated_at)
               VALUES (?, ?, ?, 'ACTIVE', ?, ?)""",
            (character_id, name, discord_user_id, now, now),
        )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="CHARACTER_CREATED",
            payload={"character_id": character_id, "name": name, "discord_user_id": discord_user_id, "lifecycle": "ACTIVE"},
            destination=session_event_destination(connection),
        )
        return {"status": "CREATED", "character_id": character_id, "name": name, "lifecycle": "ACTIVE"}

    def transition_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        character_id: str,
        lifecycle: str,
    ) -> ReceiptResult:
        normalized_lifecycle = lifecycle.strip().upper()
        if normalized_lifecycle not in CHARACTER_LIFECYCLES:
            raise CharacterError(f"unknown lifecycle: {lifecycle}")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="character-lifecycle",
            mutation=lambda connection, operation_id: self._transition_in_transaction(
                connection, operation_id, actor_id, character_id, normalized_lifecycle
            ),
        )

    def _transition_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        character_id: str,
        lifecycle: str,
    ) -> dict[str, Any]:
        character = connection.execute(
            "SELECT id, name, lifecycle FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        if character is None:
            raise CharacterError("character not found")
        current = str(character["lifecycle"])
        if lifecycle not in _ALLOWED_TRANSITIONS[current]:
            raise CharacterError(f"cannot transition {current} to {lifecycle}")
        now = iso_now()
        connection.execute(
            "UPDATE characters SET lifecycle = ?, updated_at = ? WHERE id = ?",
            (lifecycle, now, character_id),
        )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="CHARACTER_LIFECYCLE_CHANGED",
            payload={"character_id": character_id, "name": character["name"], "from": current, "to": lifecycle},
            destination=session_event_destination(connection),
        )
        return {
            "status": "LIFECYCLE_CHANGED",
            "character_id": character_id,
            "name": character["name"],
            "from": current,
            "to": lifecycle,
        }

