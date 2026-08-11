"""Transient Loot Drop lifecycle and claim operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty
from .handles import Handle, HandleRepository
from .inventory import normalize_name
from .receipts import ReceiptRepository, ReceiptResult


class LootDropError(RuntimeError):
    """Raised when a Loot Drop operation cannot be completed."""


def _expiry_after(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event_destination(session_id: str | None) -> str:
    return f"session:{session_id}" if session_id else "session:unassigned"


class LootDropService:
    def __init__(self, store: SQLiteStore, receipts: ReceiptRepository, handles: HandleRepository) -> None:
        self.store = store
        self.receipts = receipts
        self.handles = handles

    def create_drop_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        items: list[tuple[str, int, str | None]],
        session_id: str | None = None,
        expiry_hours: int = 72,
    ) -> ReceiptResult:
        if not items:
            raise LootDropError("at least one item is required")
        if expiry_hours <= 0:
            raise LootDropError("expiry must be positive")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="loot-drop",
            mutation=lambda connection, operation_id: self._create_in_transaction(
                connection, operation_id, actor_id, items, session_id, expiry_hours
            ),
        )

    def _create_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        items: list[tuple[str, int, str | None]],
        session_id: str | None,
        expiry_hours: int,
    ) -> dict[str, Any]:
        if session_id is None:
            active = connection.execute("SELECT id FROM sessions WHERE status = 'ACTIVE'").fetchone()
            session_id = active["id"] if active else None
        elif connection.execute("SELECT 1 FROM sessions WHERE id = ? AND status = 'ACTIVE'", (session_id,)).fetchone() is None:
            raise LootDropError("active session not found")

        prepared: list[tuple[str, str, int, str | None]] = []
        for item_name, quantity, provenance in items:
            if quantity <= 0:
                raise LootDropError("quantity must be positive")
            prepared.append((item_name.strip(), normalize_name(item_name), quantity, provenance))
        drop_id = str(uuid.uuid4())
        now = iso_now()
        expires_at = _expiry_after(expiry_hours)
        connection.execute(
            "INSERT INTO loot_drops(id, session_id, status, expires_at, created_at) VALUES (?, ?, 'OPEN', ?, ?)",
            (drop_id, session_id, expires_at, now),
        )
        item_results: list[dict[str, Any]] = []
        for item_name, normalized, quantity, provenance in prepared:
            item_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO loot_drop_items(
                    id, drop_id, item_name, normalized_name, quantity,
                    remaining_quantity, provenance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, drop_id, item_name, normalized, quantity, quantity, provenance, now, now),
            )
            item_results.append({"id": item_id, "item_name": item_name, "quantity": quantity, "remaining": quantity})
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="LOOT_DROP_CREATED",
            payload={"drop_id": drop_id, "session_id": session_id, "expires_at": expires_at, "items": item_results},
            destination=_event_destination(session_id),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {"status": "OPEN", "drop_id": drop_id, "session_id": session_id, "expires_at": expires_at, "items": item_results}

    def list_open(self) -> list[dict[str, Any]]:
        self.expire_due_drops()
        with self.store.connection_lock:
            connection = self.store._require_connection()
            drops = connection.execute(
                "SELECT id, session_id, expires_at, created_at FROM loot_drops WHERE status = 'OPEN' ORDER BY created_at"
            ).fetchall()
            result: list[dict[str, Any]] = []
            for drop in drops:
                items = connection.execute(
                    "SELECT id, item_name, quantity, remaining_quantity, provenance FROM loot_drop_items WHERE drop_id = ? AND remaining_quantity > 0 ORDER BY created_at",
                    (drop["id"],),
                ).fetchall()
                result.append({"drop_id": drop["id"], "session_id": drop["session_id"], "expires_at": drop["expires_at"], "items": [dict(item) for item in items]})
        return result

    def prepare_claim_view(self, *, actor_id: str | None, limit: int = 25) -> dict[str, Any]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.expire_due_drops()
        with self.store.transaction() as connection:
            drops = connection.execute(
                "SELECT id, session_id, expires_at, created_at FROM loot_drops WHERE status = 'OPEN' ORDER BY created_at"
            ).fetchall()
            result: list[dict[str, Any]] = []
            handle_ids: dict[str, str] = {}
            created_handles = 0
            for drop in drops:
                items = connection.execute(
                    "SELECT id, item_name, quantity, remaining_quantity, provenance FROM loot_drop_items WHERE drop_id = ? AND remaining_quantity > 0 ORDER BY created_at",
                    (drop["id"],),
                ).fetchall()
                item_payload = [dict(item) for item in items]
                result.append({"drop_id": drop["id"], "session_id": drop["session_id"], "expires_at": drop["expires_at"], "items": item_payload})
                for item in items:
                    if created_handles >= limit:
                        break
                    handle_ids[item["id"]] = self.handles.create_in_transaction(
                        connection,
                        workflow_type="loot-drop",
                        action="claim",
                        actor_id=actor_id,
                        payload={"drop_item_id": item["id"], "drop_id": drop["id"], "amount": 1},
                        read_set_snapshot={"remaining_quantity": item["remaining_quantity"]},
                        single_use=True,
                        ttl_seconds=300,
                    )
                    created_handles += 1
            return {"drops": result, "handles": handle_ids}

    def create_claim_handle(self, *, drop_item_id: str, actor_id: str | None, amount: int = 1) -> str:
        if amount <= 0:
            raise LootDropError("amount must be positive")
        self.expire_due_drops()
        with self.store.transaction() as connection:
            row = connection.execute(
                """SELECT item.id, item.drop_id, item.item_name, item.remaining_quantity, loot.status, loot.expires_at
                     FROM loot_drop_items AS item JOIN loot_drops AS loot ON loot.id = item.drop_id
                    WHERE item.id = ?""",
                (drop_item_id,),
            ).fetchone()
            if row is None:
                raise LootDropError("loot item not found")
            if row["status"] != "OPEN":
                raise LootDropError("LOOT_DROP_CLOSED")
            if row["expires_at"] <= iso_now():
                raise LootDropError("LOOT_DROP_EXPIRED")
            if int(row["remaining_quantity"]) < amount:
                raise LootDropError(f"only {row['remaining_quantity']} remain")
            return self.handles.create_in_transaction(
                connection,
                workflow_type="loot-drop",
                action="claim",
                actor_id=actor_id,
                payload={"drop_item_id": drop_item_id, "drop_id": row["drop_id"], "amount": amount},
                read_set_snapshot={"remaining_quantity": row["remaining_quantity"]},
                single_use=True,
                ttl_seconds=300,
            )

    def claim_interaction(self, interaction_id: str, *, handle_id: str, actor_id: str | None) -> ReceiptResult:
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="loot-drop",
            mutation=lambda connection, operation_id: self.handles.consume_and_mutate_in_transaction(
                connection,
                handle_id,
                actor_id=actor_id,
                mutation=lambda transaction, handle: self._claim_in_transaction(transaction, operation_id, actor_id, handle),
            ),
        )

    def _claim_in_transaction(self, connection: Any, operation_id: str, actor_id: str | None, handle: Handle) -> dict[str, Any]:
        drop_item_id = handle.payload["drop_item_id"]
        row = connection.execute(
            """SELECT item.*, loot.session_id, loot.status, loot.expires_at
                 FROM loot_drop_items AS item JOIN loot_drops AS loot ON loot.id = item.drop_id
                WHERE item.id = ?""",
            (drop_item_id,),
        ).fetchone()
        if row is None:
            raise LootDropError("loot item not found")
        if row["status"] != "OPEN":
            return {"status": "CLOSED", "drop_id": row["drop_id"]}
        if row["expires_at"] <= iso_now():
            self._close_drop_in_transaction(connection, operation_id, row["drop_id"], "EXPIRED")
            return {"status": "EXPIRED", "drop_id": row["drop_id"]}
        claimant = connection.execute(
            "SELECT id, name FROM characters WHERE discord_user_id = ? AND lifecycle = 'ACTIVE'",
            (actor_id,),
        ).fetchone()
        if claimant is None:
            raise LootDropError("an active registered character is required to claim Loot Drops")
        amount = int(handle.payload["amount"])
        remaining = int(row["remaining_quantity"])
        if remaining < amount:
            raise LootDropError(f"only {remaining} remain")
        now = iso_now()
        new_remaining = remaining - amount
        connection.execute(
            "UPDATE loot_drop_items SET remaining_quantity = ?, updated_at = ? WHERE id = ?",
            (new_remaining, now, drop_item_id),
        )
        owner_id = str(claimant["id"])
        character = connection.execute(
            "SELECT * FROM inventory_stacks WHERE owner_type = 'CHARACTER' AND owner_id = ? AND normalized_name = ? AND variant_metadata = '{}'",
            (owner_id, row["normalized_name"]),
        ).fetchone()
        if character is None:
            connection.execute(
                """INSERT INTO inventory_stacks(
                    id, item_name, normalized_name, variant_metadata, quantity,
                    provenance, owner_type, owner_id, version, last_acquired_at, updated_at
                ) VALUES (?, ?, ?, '{}', ?, ?, 'CHARACTER', ?, 1, ?, ?)""",
                (str(uuid.uuid4()), row["item_name"], row["normalized_name"], amount, row["provenance"], owner_id, now, now),
            )
        else:
            connection.execute(
                "UPDATE inventory_stacks SET quantity = quantity + ?, version = version + 1, last_acquired_at = ?, updated_at = ? WHERE id = ?",
                (amount, now, now, character["id"]),
            )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="LOOT_CLAIMED",
            payload={"drop_id": row["drop_id"], "drop_item_id": drop_item_id, "item_name": row["item_name"], "quantity": amount, "remaining": new_remaining, "claimant_id": owner_id},
            destination=_event_destination(row["session_id"]),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {"status": "CLAIMED", "drop_id": row["drop_id"], "item_name": row["item_name"], "quantity": amount, "remaining": new_remaining}

    def close_drop_interaction(self, interaction_id: str, *, drop_id: str, actor_id: str | None, reason: str = "MANUAL") -> ReceiptResult:
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="loot-drop",
            mutation=lambda connection, operation_id: self._close_drop_in_transaction(connection, operation_id, drop_id, reason),
        )

    def _close_drop_in_transaction(self, connection: Any, operation_id: str, drop_id: str, reason: str) -> dict[str, Any]:
        drop = connection.execute("SELECT * FROM loot_drops WHERE id = ?", (drop_id,)).fetchone()
        if drop is None:
            raise LootDropError("loot drop not found")
        if drop["status"] == "CLOSED":
            return {"status": "CLOSED", "drop_id": drop_id}
        rows = connection.execute(
            "SELECT item_name, normalized_name, remaining_quantity, provenance FROM loot_drop_items WHERE drop_id = ? AND remaining_quantity > 0",
            (drop_id,),
        ).fetchall()
        for row in rows:
            self._return_to_party(connection, row["item_name"], row["normalized_name"], int(row["remaining_quantity"]), row["provenance"])
        now = iso_now()
        connection.execute("UPDATE loot_drops SET status = 'CLOSED', closed_at = ? WHERE id = ? AND status = 'OPEN'", (now, drop_id))
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=None,
            event_type="LOOT_DROP_CLOSED",
            payload={"drop_id": drop_id, "reason": reason, "returned_item_count": sum(int(row["remaining_quantity"]) for row in rows)},
            destination=_event_destination(drop["session_id"]),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {"status": "CLOSED", "drop_id": drop_id, "reason": reason, "returned_item_count": sum(int(row["remaining_quantity"]) for row in rows)}

    def _return_to_party(self, connection: Any, item_name: str, normalized_name: str, quantity: int, provenance: str | None) -> None:
        now = iso_now()
        row = connection.execute(
            "SELECT id FROM inventory_stacks WHERE owner_type = 'PARTY' AND owner_id = 'party' AND normalized_name = ? AND variant_metadata = '{}'",
            (normalized_name,),
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO inventory_stacks(
                    id, item_name, normalized_name, variant_metadata, quantity,
                    provenance, owner_type, owner_id, version, last_acquired_at, updated_at
                ) VALUES (?, ?, ?, '{}', ?, ?, 'PARTY', 'party', 1, ?, ?)""",
                (str(uuid.uuid4()), item_name, normalized_name, quantity, provenance, now, now),
            )
        else:
            connection.execute(
                "UPDATE inventory_stacks SET quantity = quantity + ?, version = version + 1, provenance = COALESCE(?, provenance), last_acquired_at = ?, updated_at = ? WHERE id = ?",
                (quantity, provenance, now, now, row["id"]),
            )

    def close_session_drops(self, connection: Any, *, session_id: str, operation_id: str) -> int:
        drops = connection.execute("SELECT id FROM loot_drops WHERE session_id = ? AND status = 'OPEN'", (session_id,)).fetchall()
        for drop in drops:
            self._close_drop_in_transaction(connection, operation_id, drop["id"], "SESSION_CLOSED")
        return len(drops)

    def expire_due_drops(self) -> int:
        now = iso_now()
        with self.store.transaction() as connection:
            drops = connection.execute("SELECT id FROM loot_drops WHERE status = 'OPEN' AND expires_at <= ?", (now,)).fetchall()
            for drop in drops:
                self._close_drop_in_transaction(connection, str(uuid.uuid4()), drop["id"], "EXPIRED")
            return len(drops)


def expire_due_drops(store: SQLiteStore) -> int:
    """Close due drops for background maintenance without needing receipts."""
    service = LootDropService(store, ReceiptRepository(store), HandleRepository(store))
    return service.expire_due_drops()
