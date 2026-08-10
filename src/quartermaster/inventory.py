"""Party Stash domain operations for the first functional product slice."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty
from .handles import Handle, HandleError, HandleRepository
from .receipts import ReceiptRepository, ReceiptResult


class InventoryError(RuntimeError):
    """Raised for a domain-level inventory failure."""


class SemanticStaleness(InventoryError):
    """Raised when a relative request no longer means what the user observed."""


def normalize_name(name: str) -> str:
    normalized = " ".join(name.split()).casefold()
    if not normalized:
        raise InventoryError("item name is required")
    return normalized


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
        append_event(connection, operation_id=operation_id, actor_id=actor_id, event_type="ITEM_GRANTED", payload={"stack_id": stack_id, "item_name": item_name.strip(), "quantity": quantity, "new_quantity": new_quantity, "provenance": provenance}, destination="session:active")
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

    def take_interaction(self, interaction_id: str, *, handle_id: str, actor_id: str | None) -> ReceiptResult:
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="stash",
            mutation=lambda connection, operation_id: self._take_with_handle(connection, operation_id, handle_id, actor_id),
        )

    def _take_with_handle(self, connection: Any, operation_id: str, handle_id: str, actor_id: str | None) -> dict[str, Any]:
        return self.handles.consume_and_mutate_in_transaction(
            connection,
            handle_id,
            actor_id=actor_id,
            mutation=lambda transaction, handle: self._take_in_transaction(transaction, operation_id, actor_id, handle),
        )

    def _take_in_transaction(self, connection: Any, operation_id: str, actor_id: str | None, handle: Handle) -> dict[str, Any]:
        stack_id = handle.payload["stack_id"]
        row = connection.execute("SELECT * FROM inventory_stacks WHERE id = ? AND owner_type = 'PARTY'", (stack_id,)).fetchone()
        if row is None:
            raise InventoryError("item stack no longer exists")
        mode = handle.payload["mode"]
        observed = int(handle.read_set_snapshot["quantity"])
        current = int(row["quantity"])
        if mode == "RELATIVE" and current != observed:
            raise SemanticStaleness(f"quantity changed from {observed} to {current}")
        amount = current if mode == "RELATIVE" else int(handle.payload["amount"])
        if current < amount:
            raise InventoryError(f"only {current} remain")
        remaining = current - amount
        now = iso_now()
        if remaining == 0:
            connection.execute("DELETE FROM inventory_stacks WHERE id = ?", (stack_id,))
        else:
            connection.execute("UPDATE inventory_stacks SET quantity = ?, version = version + 1, updated_at = ? WHERE id = ?", (remaining, now, stack_id))
        append_event(connection, operation_id=operation_id, actor_id=actor_id, event_type="ITEM_TAKEN", payload={"stack_id": stack_id, "item_name": row["item_name"], "quantity": amount, "remaining": remaining}, destination="session:active")
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {"status": "TAKEN", "stack_id": stack_id, "item_name": row["item_name"], "quantity": amount, "remaining": remaining}

    def browse(self) -> list[dict[str, Any]]:
        rows = self.store._require_connection().execute(
            "SELECT id, item_name, quantity, provenance, version, updated_at FROM inventory_stacks WHERE owner_type = 'PARTY' AND owner_id = 'party' ORDER BY last_acquired_at DESC, item_name"
        ).fetchall()
        return [dict(row) for row in rows]
