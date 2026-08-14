"""Party Stash domain operations for the first functional product slice."""

from __future__ import annotations

import uuid
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty, session_event_destination
from .handles import Handle, HandleRepository
from .naming import normalize_name as _normalize_name
from .receipts import ReceiptRepository, ReceiptResult


class InventoryError(RuntimeError):
    """Raised for a domain-level inventory failure."""


class SemanticStaleness(InventoryError):
    """Raised when a relative request no longer means what the user observed."""


def normalize_name(name: str) -> str:
    normalized = _normalize_name(name)
    if not normalized:
        raise InventoryError("item name is required")
    return normalized


def active_claimant(connection: Any, actor_id: str | None) -> Any:
    """Resolve the active character a Discord actor may receive items as.

    `one_active_character_per_user_idx` keeps this to at most one row; the
    ordering makes the result deterministic anyway rather than leaving the
    recipient of a claim to whichever row SQLite happens to return first.
    """
    if actor_id is None:
        return None
    return connection.execute(
        """SELECT id, name FROM characters
            WHERE discord_user_id = ? AND lifecycle = 'ACTIVE'
            ORDER BY created_at, id
            LIMIT 1""",
        (actor_id,),
    ).fetchone()


def credit_character_stack(
    connection: Any,
    *,
    owner_id: str,
    item_name: str,
    normalized_name: str,
    quantity: int,
    provenance: str | None,
    now: str,
) -> None:
    """Move a quantity into a character's holdings, merging with any equal stack."""
    connection.execute(
        """INSERT INTO inventory_stacks(
            id, item_name, normalized_name, variant_metadata, quantity,
            provenance, owner_type, owner_id, version, last_acquired_at, updated_at
        ) VALUES (?, ?, ?, '{}', ?, ?, 'CHARACTER', ?, 1, ?, ?)
        ON CONFLICT(owner_type, owner_id, normalized_name, variant_metadata) DO UPDATE SET
            quantity = inventory_stacks.quantity + excluded.quantity,
            version = inventory_stacks.version + 1,
            last_acquired_at = excluded.last_acquired_at,
            updated_at = excluded.updated_at""",
        (str(uuid.uuid4()), item_name, normalized_name, quantity, provenance, owner_id, now, now),
    )


class InventoryService:
    def __init__(self, store: SQLiteStore, receipts: ReceiptRepository, handles: HandleRepository) -> None:
        self.store = store
        self.receipts = receipts
        self.handles = handles

    def grant_interaction(self, interaction_id: str, *, actor_id: str | None, item_name: str, quantity: int, provenance: str | None = None) -> ReceiptResult:
        if quantity <= 0:
            raise InventoryError("quantity must be positive")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="stash",
            mutation=lambda connection, operation_id: self._grant_in_transaction(connection, operation_id, actor_id, item_name, quantity, provenance),
        )

    def _grant_in_transaction(self, connection: Any, operation_id: str, actor_id: str | None, item_name: str, quantity: int, provenance: str | None) -> dict[str, Any]:
        normalized = normalize_name(item_name)
        metadata = "{}"
        now = iso_now()
        row = connection.execute(
            "SELECT * FROM inventory_stacks WHERE owner_type = 'PARTY' AND owner_id = 'party' AND normalized_name = ? AND variant_metadata = ?",
            (normalized, metadata),
        ).fetchone()
        if row is None:
            stack_id = str(uuid.uuid4())
            new_quantity = quantity
            connection.execute(
                """INSERT INTO inventory_stacks(
                    id, item_name, normalized_name, variant_metadata, quantity,
                    provenance, owner_type, owner_id, version, last_acquired_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PARTY', 'party', 1, ?, ?)""",
                (stack_id, item_name.strip(), normalized, metadata, quantity, provenance, now, now),
            )
        else:
            stack_id = row["id"]
            new_quantity = int(row["quantity"]) + quantity
            connection.execute(
                "UPDATE inventory_stacks SET quantity = ?, version = version + 1, provenance = COALESCE(?, provenance), last_acquired_at = ?, updated_at = ? WHERE id = ?",
                (new_quantity, provenance, now, now, stack_id),
            )
        append_event(connection, operation_id=operation_id, actor_id=actor_id, event_type="ITEM_GRANTED", payload={"stack_id": stack_id, "item_name": item_name.strip(), "quantity": quantity, "new_quantity": new_quantity, "provenance": provenance}, destination=session_event_destination(connection))
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {"status": "GRANTED", "stack_id": stack_id, "item_name": item_name.strip(), "quantity": quantity, "new_quantity": new_quantity}

    def create_take_handle(self, *, stack_id: str, actor_id: str | None, amount: int | str) -> str:
        with self.store.transaction() as connection:
            row = connection.execute("SELECT id, item_name, quantity, version FROM inventory_stacks WHERE id = ? AND owner_type = 'PARTY'", (stack_id,)).fetchone()
            if row is None:
                raise InventoryError("item stack not found")
            if amount == "all":
                action_amount = "all"
                mode = "RELATIVE"
            else:
                if not isinstance(amount, int) or amount <= 0:
                    raise InventoryError("amount must be positive or 'all'")
                action_amount = amount
                mode = "ABSOLUTE"
            return self.handles.create_in_transaction(
                connection,
                workflow_type="stash",
                action="take",
                actor_id=actor_id,
                payload={"stack_id": stack_id, "item_name": row["item_name"], "amount": action_amount, "mode": mode},
                read_set_snapshot={"quantity": row["quantity"], "version": row["version"]},
                single_use=True,
                ttl_seconds=300,
            )

    def prepare_take_view(self, *, actor_id: str | None, limit: int = 25) -> dict[str, Any]:
        """Snapshot the stash and mint the handles the browse controls will use.

        Take-all handles are RELATIVE and carry the quantity that was on screen,
        which is what lets a take of "everything" notice that the stash changed
        under the player and ask them to confirm the new amount instead.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT id, item_name, quantity, provenance, version, updated_at FROM inventory_stacks WHERE owner_type = 'PARTY' AND owner_id = 'party' ORDER BY last_acquired_at DESC, item_name LIMIT ?",
                (limit,),
            ).fetchall()
            # The snapshot is capped at what one component view can carry, so the
            # caller is told the real size too and can say the list is partial.
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM inventory_stacks WHERE owner_type = 'PARTY' AND owner_id = 'party'"
                ).fetchone()[0]
            )
            items = [dict(row) for row in rows]
            handles: dict[str, str] = {}
            take_all_handles: dict[str, str] = {}
            for row in rows:
                snapshot = {"quantity": row["quantity"], "version": row["version"]}
                handles[row["id"]] = self.handles.create_in_transaction(
                    connection,
                    workflow_type="stash",
                    action="take",
                    actor_id=actor_id,
                    payload={"stack_id": row["id"], "item_name": row["item_name"], "amount": 1, "mode": "ABSOLUTE"},
                    read_set_snapshot=snapshot,
                    single_use=True,
                    ttl_seconds=300,
                )
                if int(row["quantity"]) > 1:
                    take_all_handles[row["id"]] = self.handles.create_in_transaction(
                        connection,
                        workflow_type="stash",
                        action="take",
                        actor_id=actor_id,
                        payload={
                            "stack_id": row["id"],
                            "item_name": row["item_name"],
                            "amount": "all",
                            "mode": "RELATIVE",
                        },
                        read_set_snapshot=snapshot,
                        single_use=True,
                        ttl_seconds=300,
                    )
            return {
                "items": items,
                "handles": handles,
                "take_all_handles": take_all_handles,
                "total_items": total,
            }

    def take_interaction(self, interaction_id: str, *, handle_id: str, actor_id: str | None) -> ReceiptResult:
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="stash",
            mutation=lambda connection, operation_id: self._take_with_handle(connection, operation_id, handle_id, actor_id),
        )

    def confirm_take_interaction(self, interaction_id: str, *, handle_id: str, actor_id: str | None) -> ReceiptResult:
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="stash",
            mutation=lambda connection, operation_id: self._take_with_handle(
                connection, operation_id, handle_id, actor_id, allow_relative_stale=True
            ),
        )

    def _take_with_handle(
        self,
        connection: Any,
        operation_id: str,
        handle_id: str,
        actor_id: str | None,
        *,
        allow_relative_stale: bool = False,
    ) -> dict[str, Any]:
        return self.handles.consume_and_mutate_in_transaction(
            connection,
            handle_id,
            actor_id=actor_id,
            mutation=lambda transaction, handle: self._take_in_transaction(
                transaction, operation_id, actor_id, handle, allow_relative_stale=allow_relative_stale
            ),
        )

    def _take_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        handle: Handle,
        *,
        allow_relative_stale: bool = False,
    ) -> dict[str, Any]:
        stack_id = handle.payload["stack_id"]
        row = connection.execute("SELECT * FROM inventory_stacks WHERE id = ? AND owner_type = 'PARTY'", (stack_id,)).fetchone()
        if row is None:
            raise InventoryError("item stack no longer exists")
        mode = handle.payload["mode"]
        observed = int(handle.read_set_snapshot["quantity"])
        current = int(row["quantity"])
        if mode == "RELATIVE" and current != observed and not allow_relative_stale:
            raise SemanticStaleness(f"quantity changed from {observed} to {current}")
        amount = current if mode == "RELATIVE" else int(handle.payload["amount"])
        if current < amount:
            raise InventoryError(f"only {current} remain")
        # A Take is a transfer of ownership, not a deletion: the taker must be a
        # registered active character, exactly as a Loot Drop claim requires.
        taker = active_claimant(connection, actor_id)
        if taker is None:
            raise InventoryError("an active registered character is required to take Party Stash items")
        remaining = current - amount
        now = iso_now()
        if remaining == 0:
            connection.execute("DELETE FROM inventory_stacks WHERE id = ?", (stack_id,))
        else:
            connection.execute("UPDATE inventory_stacks SET quantity = ?, version = version + 1, updated_at = ? WHERE id = ?", (remaining, now, stack_id))
        credit_character_stack(
            connection,
            owner_id=str(taker["id"]),
            item_name=row["item_name"],
            normalized_name=row["normalized_name"],
            quantity=amount,
            provenance=row["provenance"],
            now=now,
        )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="ITEM_TAKEN",
            payload={
                "stack_id": stack_id,
                "item_name": row["item_name"],
                "quantity": amount,
                "remaining": remaining,
                "character_id": str(taker["id"]),
                "character_name": taker["name"],
            },
            destination=session_event_destination(connection),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {
            "status": "TAKEN",
            "stack_id": stack_id,
            "item_name": row["item_name"],
            "quantity": amount,
            "remaining": remaining,
            "character_id": str(taker["id"]),
            "character_name": taker["name"],
        }

    def browse(self) -> list[dict[str, Any]]:
        with self.store.read() as connection:
            rows = connection.execute(
                "SELECT id, item_name, quantity, provenance, version, updated_at FROM inventory_stacks WHERE owner_type = 'PARTY' AND owner_id = 'party' ORDER BY last_acquired_at DESC, item_name"
            ).fetchall()
        return [dict(row) for row in rows]
