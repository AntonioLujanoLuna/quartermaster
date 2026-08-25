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
from .rendering import DISCORD_VIEW_COMPONENT_LIMIT


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


def credit_stack(
    connection: Any,
    *,
    owner_type: str,
    owner_id: str,
    item_name: str,
    normalized_name: str,
    quantity: int,
    provenance: str | None,
    now: str,
) -> None:
    """Move a quantity into an owner's holdings, merging with any equal stack.

    Every path that puts an item somewhere goes through here — a take, a claim,
    a give, a closed Loot Drop returning what nobody wanted — so the merge rule
    is defined once. Splitting it per destination is how one path ends up
    creating a second stack of the same item beside the first.
    """
    connection.execute(
        """INSERT INTO inventory_stacks(
            id, item_name, normalized_name, variant_metadata, quantity,
            provenance, owner_type, owner_id, version, last_acquired_at, updated_at
        ) VALUES (?, ?, ?, '{}', ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(owner_type, owner_id, normalized_name, variant_metadata) DO UPDATE SET
            quantity = inventory_stacks.quantity + excluded.quantity,
            version = inventory_stacks.version + 1,
            provenance = COALESCE(excluded.provenance, inventory_stacks.provenance),
            last_acquired_at = excluded.last_acquired_at,
            updated_at = excluded.updated_at""",
        (
            str(uuid.uuid4()),
            item_name,
            normalized_name,
            quantity,
            provenance,
            owner_type,
            owner_id,
            now,
            now,
        ),
    )


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
    """Credit a character, for the paths that can only ever mean a character."""
    credit_stack(
        connection,
        owner_type="CHARACTER",
        owner_id=owner_id,
        item_name=item_name,
        normalized_name=normalized_name,
        quantity=quantity,
        provenance=provenance,
        now=now,
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
            action_amount: int | str
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

    def prepare_take_view(
        self,
        *,
        actor_id: str | None,
        limit: int = 25,
        control_budget: int = DISCORD_VIEW_COMPONENT_LIMIT,
    ) -> dict[str, Any]:
        """Snapshot the stash and mint the handles the browse controls will use.

        Take-all handles are RELATIVE and carry the quantity that was on screen,
        which is what lets a take of "everything" notice that the stash changed
        under the player and ask them to confirm the new amount instead.

        A stack above one needs two controls, so the snapshot limit is not the
        limit that matters: twenty-five stacks want fifty buttons and one view
        holds twenty-five. Handles are therefore minted against the budget the
        view can actually render, and only for a leading run of stacks, so the
        controls line up with the top of the list the player is reading. The
        caller is told which stacks got controls and can say so.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        if control_budget <= 0:
            raise ValueError("control budget must be positive")
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
            remaining_controls = control_budget
            for row in rows:
                controls_needed = 2 if int(row["quantity"]) > 1 else 1
                if controls_needed > remaining_controls:
                    break
                remaining_controls -= controls_needed
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

    def give_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        item_name: str,
        quantity: int,
        destination: str = "party",
    ) -> ReceiptResult:
        """Hand something the actor's character holds back, or on to someone else.

        Until this existed, possession moved one way only. A take or a claim
        transfers ownership to the taker's active character, and nothing —
        including the DM — could move it back: resolving belongings refuses
        active characters, and a grant mints a new item rather than returning
        one, so using it to undo a mistaken take quietly breaks conservation.
        A misread `Take all` was therefore permanent. That is also what made the
        take-all confirmation prompt load-bearing; it is a good prompt, but it
        was the only thing standing between the table and an unfixable stash.

        This is the path for a quantity someone names outright. The component
        path carries a handle instead — see `create_give_handles`.
        """
        if quantity <= 0:
            raise InventoryError("quantity must be positive")
        normalized_destination = destination.strip()
        if not normalized_destination:
            raise InventoryError("a destination is required")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="stash",
            mutation=lambda connection, operation_id: self._give_in_transaction(
                connection, operation_id, actor_id, item_name, quantity, normalized_destination
            ),
        )

    def _give_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        item_name: str,
        quantity: int,
        destination: str,
    ) -> dict[str, Any]:
        normalized = normalize_name(item_name)
        giver = active_claimant(connection, actor_id)
        if giver is None:
            raise InventoryError("an active registered character is required to give items")
        giver_id = str(giver["id"])
        source = connection.execute(
            """SELECT * FROM inventory_stacks
                WHERE owner_type = 'CHARACTER' AND owner_id = ?
                  AND normalized_name = ? AND variant_metadata = '{}'""",
            (giver_id, normalized),
        ).fetchone()
        if source is None:
            raise InventoryError(f"{giver['name']} is not holding {item_name.strip()}")
        return self._move_from_character(
            connection,
            operation_id,
            actor_id,
            giver=giver,
            source=source,
            quantity=quantity,
            destination=destination,
        )

    def _move_from_character(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        *,
        giver: Any,
        source: Any,
        quantity: int,
        destination: str,
    ) -> dict[str, Any]:
        """Move a quantity out of a character's stack, wherever the request came from.

        The typed path resolves the stack by name and the component path resolves
        it by handle; from here they are the same transfer, and keeping one body
        is what stops the two from disagreeing about who may receive what.
        """
        giver_id = str(giver["id"])
        held = int(source["quantity"])
        if held < quantity:
            raise InventoryError(f"{giver['name']} holds only {held}")

        if destination.casefold() == "party":
            destination_type, destination_id, destination_name = "PARTY", "party", "the Party Stash"
        else:
            recipient = connection.execute(
                "SELECT id, name, lifecycle FROM characters WHERE id = ?", (destination,)
            ).fetchone()
            if recipient is None:
                raise InventoryError("recipient character not found")
            if recipient["lifecycle"] != "ACTIVE":
                # Specification 32.1: a non-active character cannot ordinarily
                # receive transfers, and belongings resolution is the deliberate
                # exception the DM drives.
                raise InventoryError("only active characters can receive items")
            if str(recipient["id"]) == giver_id:
                raise InventoryError("the recipient must differ from the giver")
            destination_type, destination_id = "CHARACTER", str(recipient["id"])
            destination_name = str(recipient["name"])

        now = iso_now()
        remaining = held - quantity
        if remaining == 0:
            connection.execute("DELETE FROM inventory_stacks WHERE id = ?", (source["id"],))
        else:
            connection.execute(
                "UPDATE inventory_stacks SET quantity = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (remaining, now, source["id"]),
            )
        credit_stack(
            connection,
            owner_type=destination_type,
            owner_id=destination_id,
            item_name=source["item_name"],
            normalized_name=source["normalized_name"],
            quantity=quantity,
            provenance=source["provenance"],
            now=now,
        )
        result = {
            "status": "GIVEN",
            "item_name": source["item_name"],
            "quantity": quantity,
            "remaining": remaining,
            "character_id": giver_id,
            "character_name": giver["name"],
            "destination_type": destination_type,
            "destination_id": destination_id,
            "destination_name": destination_name,
        }
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="ITEM_GIVEN",
            payload=result,
            destination=session_event_destination(connection),
        )
        # The pinned surface renders party holdings, so it only changes when the
        # Party Stash is one end of the transfer.
        if destination_type == "PARTY":
            mark_projection_dirty(
                connection, target_id="party-stash", target_type="STATE", destination="party-inventory"
            )
        return result

    def holdings(self, *, actor_id: str | None, limit: int = 25) -> dict[str, Any]:
        """What the actor's active character is carrying, for the give controls.

        Returns the character as well as the stacks, because "you have no
        registered character" and "your character is carrying nothing" are
        different answers and a panel that conflates them tells a player to go
        and take something they will then be refused.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.store.read() as connection:
            holder = active_claimant(connection, actor_id)
            if holder is None:
                return {"character": None, "items": [], "total_items": 0}
            rows = connection.execute(
                """SELECT id, item_name, quantity, provenance, version, updated_at
                    FROM inventory_stacks
                    WHERE owner_type = 'CHARACTER' AND owner_id = ?
                    ORDER BY last_acquired_at DESC, item_name LIMIT ?""",
                (str(holder["id"]), limit),
            ).fetchall()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM inventory_stacks WHERE owner_type = 'CHARACTER' AND owner_id = ?",
                    (str(holder["id"]),),
                ).fetchone()[0]
            )
        return {
            "character": {"id": str(holder["id"]), "name": str(holder["name"])},
            "items": [dict(row) for row in rows],
            "total_items": total,
        }

    def held_by_character(self) -> dict[str, Any]:
        """What every character is carrying, grouped by the character carrying it.

        `browse` answers what the party shares and `holdings` answers what one
        player carries, so the question the table actually asks between them —
        who has the rope — could only be answered by reading the export, which
        is a DM-only document and a wall of prose. This is that column of the
        export as a read.

        Characters holding nothing are omitted rather than listed empty: the
        roster already says who exists, and a list of names with no items under
        them is a longer way of not answering. Lifecycle travels with the name
        because a stack still held by a character who has stopped playing is
        exactly what estate resolution is for, and seeing it is how a DM knows
        to resolve one.
        """
        with self.store.read() as connection:
            rows = connection.execute(
                """SELECT stack.owner_id AS character_id,
                          character.name AS character_name,
                          character.lifecycle AS lifecycle,
                          stack.item_name AS item_name,
                          stack.quantity AS quantity,
                          stack.provenance AS provenance
                     FROM inventory_stacks AS stack
                     JOIN characters AS character ON character.id = stack.owner_id
                    WHERE stack.owner_type = 'CHARACTER' AND stack.quantity > 0
                 ORDER BY character.lifecycle, character.name, stack.item_name"""
            ).fetchall()
        holders: dict[str, dict[str, Any]] = {}
        for row in rows:
            character_id = str(row["character_id"])
            holder = holders.setdefault(
                character_id,
                {
                    "character_id": character_id,
                    "character_name": str(row["character_name"]),
                    "lifecycle": str(row["lifecycle"]),
                    "items": [],
                },
            )
            holder["items"].append(
                {
                    "item_name": row["item_name"],
                    "quantity": row["quantity"],
                    "provenance": row["provenance"],
                }
            )
        characters = list(holders.values())
        return {
            "characters": characters,
            "total_stacks": sum(len(holder["items"]) for holder in characters),
        }

    def create_give_handles(self, *, stack_id: str, actor_id: str | None) -> dict[str, Any]:
        """Mint the give controls for one held stack against what it holds now.

        A typed give names its own quantity, so there is nothing on screen to go
        stale. A button does not: "Give all" means the number the player was
        looking at when the panel rendered, and between rendering and pressing
        another character can hand them more of the same item. The relative
        handle is what makes that difference visible rather than silent, exactly
        as it already is for Take all.
        """
        with self.store.transaction() as connection:
            giver = active_claimant(connection, actor_id)
            if giver is None:
                raise InventoryError("an active registered character is required to give items")
            row = connection.execute(
                "SELECT * FROM inventory_stacks WHERE id = ? AND owner_type = 'CHARACTER' AND owner_id = ?",
                (stack_id, str(giver["id"])),
            ).fetchone()
            if row is None:
                raise InventoryError(f"{giver['name']} is no longer holding that item")
            snapshot = {"quantity": row["quantity"], "version": row["version"]}
            handles = {
                "one": self.handles.create_in_transaction(
                    connection,
                    workflow_type="stash",
                    action="give",
                    actor_id=actor_id,
                    payload={
                        "stack_id": stack_id,
                        "item_name": row["item_name"],
                        "amount": 1,
                        "mode": "ABSOLUTE",
                    },
                    read_set_snapshot=snapshot,
                    single_use=True,
                    ttl_seconds=300,
                )
            }
            if int(row["quantity"]) > 1:
                handles["all"] = self.handles.create_in_transaction(
                    connection,
                    workflow_type="stash",
                    action="give",
                    actor_id=actor_id,
                    payload={
                        "stack_id": stack_id,
                        "item_name": row["item_name"],
                        "amount": "all",
                        "mode": "RELATIVE",
                    },
                    read_set_snapshot=snapshot,
                    single_use=True,
                    ttl_seconds=300,
                )
            return {
                "item": dict(row),
                "character": {"id": str(giver["id"]), "name": str(giver["name"])},
                "handles": handles,
            }

    def give_with_handle_interaction(
        self,
        interaction_id: str,
        *,
        handle_id: str,
        actor_id: str | None,
        destination: str,
    ) -> ReceiptResult:
        return self._give_with_handle(interaction_id, handle_id=handle_id, actor_id=actor_id, destination=destination)

    def confirm_give_with_handle_interaction(
        self,
        interaction_id: str,
        *,
        handle_id: str,
        actor_id: str | None,
        destination: str,
    ) -> ReceiptResult:
        return self._give_with_handle(
            interaction_id,
            handle_id=handle_id,
            actor_id=actor_id,
            destination=destination,
            allow_relative_stale=True,
        )

    def _give_with_handle(
        self,
        interaction_id: str,
        *,
        handle_id: str,
        actor_id: str | None,
        destination: str,
        allow_relative_stale: bool = False,
    ) -> ReceiptResult:
        normalized_destination = destination.strip()
        if not normalized_destination:
            raise InventoryError("a destination is required")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="stash",
            mutation=lambda connection, operation_id: self.handles.consume_and_mutate_in_transaction(
                connection,
                handle_id,
                actor_id=actor_id,
                mutation=lambda transaction, handle: self._give_handle_in_transaction(
                    transaction,
                    operation_id,
                    actor_id,
                    handle,
                    normalized_destination,
                    allow_relative_stale=allow_relative_stale,
                ),
            ),
        )

    def _give_handle_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        handle: Handle,
        destination: str,
        *,
        allow_relative_stale: bool = False,
    ) -> dict[str, Any]:
        giver = active_claimant(connection, actor_id)
        if giver is None:
            raise InventoryError("an active registered character is required to give items")
        stack_id = handle.payload["stack_id"]
        source = connection.execute(
            "SELECT * FROM inventory_stacks WHERE id = ? AND owner_type = 'CHARACTER' AND owner_id = ?",
            (stack_id, str(giver["id"])),
        ).fetchone()
        if source is None:
            raise InventoryError(f"{giver['name']} is no longer holding that item")
        observed = int(handle.read_set_snapshot["quantity"])
        current = int(source["quantity"])
        if handle.payload["mode"] == "RELATIVE":
            if current != observed and not allow_relative_stale:
                raise SemanticStaleness(f"quantity changed from {observed} to {current}")
            quantity = current
        else:
            quantity = int(handle.payload["amount"])
        return self._move_from_character(
            connection,
            operation_id,
            actor_id,
            giver=giver,
            source=source,
            quantity=quantity,
            destination=destination,
        )

    def consume_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        stack_id: str,
        quantity: int,
        reason: str | None = None,
        party_authorized: bool = False,
    ) -> ReceiptResult:
        """Take a quantity out of the campaign, deliberately and on the record.

        Every other item path is a mint or a transfer. A grant mints into the
        Party Stash, a Loot Drop mints into the drop and returns what nobody
        wanted, and take, claim, give and belongings resolution move what
        already exists between owners. A stack row is only ever deleted because
        its quantity reached zero on the way somewhere else, so the campaign's
        item total could rise and never fall.

        Two ordinary things at the table had nowhere to go. A potion drunk, a
        rope burned, twenty arrows fired: the stash keeps saying the party has
        them, and it drifts fastest for exactly the items that get used most.
        And a mistyped grant — 50 potions where the DM meant 5 — was permanent,
        because the repair everyone reaches for is the one that makes it worse.
        Granting again cannot subtract, taking only moves the mistake onto a
        character, and the treasury's own **Adjust…** has taken signed deltas
        since the beginning: coin has had this exit all along and items did not.

        Who may do it follows possession, which is the same rule the give paths
        already hold. You may use up what you are carrying, because it is
        yours and handing it back is already yours to do. Only a DM may remove
        from the Party Stash, because it is shared. `party_authorized` is what
        the Discord gate passes through, and it is checked here rather than
        only at the control, because the control is ergonomics and this is the
        boundary.

        Nothing here is relative. The quantity is always named by the person
        removing it, so there is no handle and no staleness prompt: the one
        operation with no way back should never be a single press whose meaning
        was fixed by a render that has since gone stale.
        """
        if quantity <= 0:
            raise InventoryError("quantity must be positive")
        normalized_reason = (reason or "").strip() or None
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="stash",
            mutation=lambda connection, operation_id: self._consume_in_transaction(
                connection,
                operation_id,
                actor_id,
                stack_id,
                quantity,
                normalized_reason,
                party_authorized,
            ),
        )

    def _consume_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        stack_id: str,
        quantity: int,
        reason: str | None,
        party_authorized: bool,
    ) -> dict[str, Any]:
        source = connection.execute("SELECT * FROM inventory_stacks WHERE id = ?", (stack_id,)).fetchone()
        if source is None:
            raise InventoryError("that item is no longer there")
        if source["owner_type"] == "PARTY":
            if not party_authorized:
                raise InventoryError(
                    "only a DM administrator can remove something from the Party Stash"
                )
            owner_name = "the Party Stash"
        else:
            holder = active_claimant(connection, actor_id)
            if holder is None or str(holder["id"]) != str(source["owner_id"]):
                raise InventoryError("you can only use up what your own active character is holding")
            owner_name = str(holder["name"])
        held = int(source["quantity"])
        if held < quantity:
            raise InventoryError(f"{owner_name} holds only {held}")
        now = iso_now()
        remaining = held - quantity
        if remaining == 0:
            connection.execute("DELETE FROM inventory_stacks WHERE id = ?", (stack_id,))
        else:
            connection.execute(
                "UPDATE inventory_stacks SET quantity = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (remaining, now, stack_id),
            )
        result = {
            "status": "CONSUMED",
            "stack_id": stack_id,
            "item_name": source["item_name"],
            "quantity": quantity,
            "remaining": remaining,
            "owner_type": source["owner_type"],
            "owner_id": str(source["owner_id"]),
            "owner_name": owner_name,
            "reason": reason,
        }
        # The ledger is the whole justification for allowing this at all: an
        # item that leaves the campaign leaves a line saying who removed it,
        # how many, and why.
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="ITEM_CONSUMED",
            payload=result,
            destination=session_event_destination(connection),
        )
        if source["owner_type"] == "PARTY":
            mark_projection_dirty(
                connection, target_id="party-stash", target_type="STATE", destination="party-inventory"
            )
        return result

    def browse(self) -> list[dict[str, Any]]:
        with self.store.read() as connection:
            rows = connection.execute(
                "SELECT id, item_name, quantity, provenance, version, updated_at FROM inventory_stacks WHERE owner_type = 'PARTY' AND owner_id = 'party' ORDER BY last_acquired_at DESC, item_name"
            ).fetchall()
        return [dict(row) for row in rows]
