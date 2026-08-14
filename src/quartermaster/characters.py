"""Character records and lifecycle transitions."""

from __future__ import annotations

import uuid
from typing import Any

from .clock import iso_now
from .currency import CURRENCY_DENOMINATIONS, currency_from_row, empty_currency
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty, session_event_destination
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
        with self.store.read() as connection:
            rows = connection.execute(
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

    def resolve_belongings_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        character_id: str,
        destination: str,
    ) -> ReceiptResult:
        normalized_destination = destination.strip()
        if not normalized_destination:
            raise CharacterError("resolution destination is required")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="character-resolution",
            mutation=lambda connection, operation_id: self._resolve_in_transaction(
                connection, operation_id, actor_id, character_id, normalized_destination
            ),
        )

    def _resolve_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        character_id: str,
        destination: str,
    ) -> dict[str, Any]:
        source = connection.execute(
            "SELECT id, name, lifecycle FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        if source is None:
            raise CharacterError("character not found")
        if source["lifecycle"] == "ACTIVE":
            raise CharacterError("only non-active characters can resolve belongings")
        if destination.casefold() == "party":
            destination_type = "PARTY"
            destination_id = "party"
            destination_name = "Party Stash"
        else:
            destination_character = connection.execute(
                "SELECT id, name, lifecycle FROM characters WHERE id = ?", (destination,)
            ).fetchone()
            if destination_character is None:
                raise CharacterError("resolution destination character not found")
            if destination_character["lifecycle"] != "ACTIVE":
                raise CharacterError("belongings may only resolve to an active character")
            if destination_character["id"] == character_id:
                raise CharacterError("resolution destination must differ from source")
            destination_type = "CHARACTER"
            destination_id = str(destination_character["id"])
            destination_name = str(destination_character["name"])

        stacks = connection.execute(
            """SELECT * FROM inventory_stacks
                WHERE owner_type = 'CHARACTER' AND owner_id = ?
                ORDER BY normalized_name, id""",
            (character_id,),
        ).fetchall()
        moved_items: list[dict[str, Any]] = []
        now = iso_now()
        for stack in stacks:
            existing = connection.execute(
                """SELECT id FROM inventory_stacks
                    WHERE owner_type = ? AND owner_id = ? AND normalized_name = ? AND variant_metadata = ?""",
                (destination_type, destination_id, stack["normalized_name"], stack["variant_metadata"]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO inventory_stacks(
                        id, item_name, normalized_name, variant_metadata, quantity, provenance, notes,
                        owner_type, owner_id, version, last_acquired_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        stack["item_name"],
                        stack["normalized_name"],
                        stack["variant_metadata"],
                        stack["quantity"],
                        stack["provenance"],
                        stack["notes"],
                        destination_type,
                        destination_id,
                        stack["last_acquired_at"] or now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE inventory_stacks SET quantity = quantity + ?, version = version + 1, updated_at = ? WHERE id = ?",
                    (stack["quantity"], now, existing["id"]),
                )
            connection.execute("DELETE FROM inventory_stacks WHERE id = ?", (stack["id"],))
            moved_items.append({"item_name": stack["item_name"], "quantity": stack["quantity"]})

        source_currency_row = connection.execute(
            "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'CHARACTER' AND owner_id = ?",
            (character_id,),
        ).fetchone()
        currency_moved = currency_from_row(source_currency_row) if source_currency_row else empty_currency()
        if any(currency_moved.values()):
            destination_currency_row = connection.execute(
                "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = ? AND owner_id = ?",
                (destination_type, destination_id),
            ).fetchone()
            destination_currency = currency_from_row(destination_currency_row) if destination_currency_row else empty_currency()
            updated_currency = {
                denomination: destination_currency[denomination] + currency_moved[denomination]
                for denomination in CURRENCY_DENOMINATIONS
            }
            connection.execute(
                """INSERT INTO currency_balances(owner_type, owner_id, cp, sp, ep, gp, pp, version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(owner_type, owner_id) DO UPDATE SET
                       cp = excluded.cp, sp = excluded.sp, ep = excluded.ep, gp = excluded.gp, pp = excluded.pp,
                       version = currency_balances.version + 1, updated_at = excluded.updated_at""",
                (destination_type, destination_id, updated_currency["cp"], updated_currency["sp"], updated_currency["ep"], updated_currency["gp"], updated_currency["pp"], now),
            )
            connection.execute(
                """UPDATE currency_balances
                      SET cp = 0, sp = 0, ep = 0, gp = 0, pp = 0, version = version + 1, updated_at = ?
                    WHERE owner_type = 'CHARACTER' AND owner_id = ?""",
                (now, character_id),
            )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="BELONGINGS_RESOLVED",
            payload={
                "source_character_id": character_id,
                "source_character_name": source["name"],
                "destination_type": destination_type,
                "destination_id": destination_id,
                "destination_name": destination_name,
                "items": moved_items,
                "currency": currency_moved,
            },
            destination=session_event_destination(connection),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {
            "status": "RESOLVED",
            "source_character_id": character_id,
            "source_character_name": source["name"],
            "destination_type": destination_type,
            "destination_id": destination_id,
            "destination_name": destination_name,
            "items_moved": len(moved_items),
            "currency_moved": currency_moved,
        }
