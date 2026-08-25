"""Integer-only treasury currency operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty, session_event_destination
from .handles import HandleRepository
from .inventory import active_claimant
from .receipts import ReceiptRepository, ReceiptResult

CURRENCY_DENOMINATIONS = ("cp", "sp", "ep", "gp", "pp")
VISIBLE_DENOMINATIONS = ("cp", "sp", "gp", "pp")


class CurrencyError(RuntimeError):
    """Raised for invalid or impossible currency operations."""


class CurrencySemanticStaleness(CurrencyError):
    """Raised when a relative split no longer has the meaning the user observed."""


def empty_currency() -> dict[str, int]:
    return {denomination: 0 for denomination in CURRENCY_DENOMINATIONS}


def currency_from_row(row: Any) -> dict[str, int]:
    return {denomination: int(row[denomination]) for denomination in CURRENCY_DENOMINATIONS}


def format_currency(balance: Mapping[str, int], *, include_electrum: bool = False) -> str:
    denominations: tuple[str, ...] = VISIBLE_DENOMINATIONS
    if include_electrum or int(balance.get("ep", 0)) != 0:
        denominations = ("cp", "sp", "ep", "gp", "pp")
    return " · ".join(f"{int(balance.get(denomination, 0))} {denomination}" for denomination in denominations)


def _validate_deltas(deltas: Mapping[str, int], *, electrum_enabled: bool) -> dict[str, int]:
    normalized = empty_currency()
    unknown = set(deltas) - set(CURRENCY_DENOMINATIONS)
    if unknown:
        raise CurrencyError(f"unknown currency denominations: {sorted(unknown)}")
    for denomination in CURRENCY_DENOMINATIONS:
        value = deltas.get(denomination, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CurrencyError(f"{denomination} must be an integer")
        normalized[denomination] = value
    if not electrum_enabled and normalized["ep"] != 0:
        raise CurrencyError("electrum is disabled")
    if not any(normalized.values()):
        raise CurrencyError("at least one currency denomination must change")
    return normalized


def _validate_nonnegative_amounts(amounts: Mapping[str, int], *, electrum_enabled: bool) -> dict[str, int]:
    normalized = _validate_deltas(amounts, electrum_enabled=electrum_enabled)
    if any(value < 0 for value in normalized.values()):
        raise CurrencyError("currency amounts must be non-negative")
    return normalized


def read_balance(connection: Any, owner_type: str, owner_id: str) -> dict[str, int]:
    """What an owner is carrying, treating an absent row as nothing.

    Only the party has a balance row from the start; a character gets one the
    first time coin reaches them. Every caller that reads a character has to
    handle both, so the fallback lives here rather than at each call site.
    """
    row = connection.execute(
        "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = ? AND owner_id = ?",
        (owner_type, owner_id),
    ).fetchone()
    return currency_from_row(row) if row is not None else empty_currency()


def write_balance(
    connection: Any, owner_type: str, owner_id: str, balance: Mapping[str, int], now: str
) -> None:
    """Set an owner's balance, creating the row if this is their first coin."""
    connection.execute(
        """INSERT INTO currency_balances(owner_type, owner_id, cp, sp, ep, gp, pp, version, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(owner_type, owner_id) DO UPDATE SET
               cp = excluded.cp, sp = excluded.sp, ep = excluded.ep, gp = excluded.gp, pp = excluded.pp,
               version = currency_balances.version + 1, updated_at = excluded.updated_at""",
        (
            owner_type,
            owner_id,
            int(balance["cp"]),
            int(balance["sp"]),
            int(balance["ep"]),
            int(balance["gp"]),
            int(balance["pp"]),
            now,
        ),
    )


class CurrencyService:
    def __init__(
        self,
        store: SQLiteStore,
        receipts: ReceiptRepository,
        *,
        electrum_enabled: bool = False,
        handles: HandleRepository | None = None,
    ) -> None:
        self.store = store
        self.receipts = receipts
        self.electrum_enabled = electrum_enabled
        self.handles = handles or HandleRepository(store)

    def view_treasury(self) -> dict[str, int]:
        with self.store.read() as connection:
            row = connection.execute(
                "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
            ).fetchone()
        if row is None:
            raise CurrencyError("treasury balance is missing")
        return currency_from_row(row)

    def purse(self, *, actor_id: str | None) -> dict[str, Any]:
        """What the actor's active character is carrying, for the give controls.

        Returns the character as well as the coin, for the same reason
        `InventoryService.holdings` does: "you have no registered character" and
        "your character has no coin" are different answers, and a surface that
        conflates them tells a player to go and spend money they do not have.

        Until this existed a player's own coin appeared in exactly one place —
        the DM's export — so the money a split handed them was invisible to the
        person it belonged to.
        """
        with self.store.read() as connection:
            holder = active_claimant(connection, actor_id)
            if holder is None:
                return {"character": None, "balance": empty_currency()}
            return {
                "character": {"id": str(holder["id"]), "name": str(holder["name"])},
                "balance": read_balance(connection, "CHARACTER", str(holder["id"])),
            }

    def adjust_treasury_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        deltas: Mapping[str, int],
        reason: str | None = None,
    ) -> ReceiptResult:
        normalized = _validate_deltas(deltas, electrum_enabled=self.electrum_enabled)
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="treasury",
            mutation=lambda connection, operation_id: self._adjust_in_transaction(
                connection, operation_id, actor_id, normalized, reason
            ),
        )

    def _adjust_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        deltas: Mapping[str, int],
        reason: str | None,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT cp, sp, ep, gp, pp, version FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
        ).fetchone()
        if row is None:
            raise CurrencyError("treasury balance is missing")
        before = currency_from_row(row)
        after = {denomination: before[denomination] + int(deltas[denomination]) for denomination in CURRENCY_DENOMINATIONS}
        if any(value < 0 for value in after.values()):
            raise CurrencyError("treasury balances cannot become negative")
        now = iso_now()
        connection.execute(
            """UPDATE currency_balances
                  SET cp = ?, sp = ?, ep = ?, gp = ?, pp = ?, version = version + 1, updated_at = ?
                WHERE owner_type = 'PARTY' AND owner_id = 'party'""",
            (after["cp"], after["sp"], after["ep"], after["gp"], after["pp"], now),
        )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="TREASURY_ADJUSTED",
            payload={"before": before, "delta": dict(deltas), "after": after, "reason": reason},
            destination=session_event_destination(connection),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {"status": "ADJUSTED", "before": before, "delta": dict(deltas), "after": after, "reason": reason}

    def preview_split(self, *, amounts: Mapping[str, int]) -> dict[str, Any]:
        """What splitting `amounts` would do against the roster as it stands now.

        Nothing is minted and nothing moves. This is what the confirmation asks
        again with when the roster it was prepared against has since changed:
        the share is a function of how many characters are alive, so the second
        question has to carry the second answer, not repeat the first.
        """
        normalized = _validate_nonnegative_amounts(amounts, electrum_enabled=self.electrum_enabled)
        with self.store.read() as connection:
            return self._describe_split(connection, normalized)

    def prepare_split(self, *, actor_id: str | None, amounts: Mapping[str, int]) -> dict[str, Any]:
        """Mint the handle a split commits through, and say what it would do.

        A split reads the roster twice: once to show the DM who is being paid
        and how much each of them gets, and again when the coin actually moves.
        A character dying in between changes every share, and the DM pressed a
        button that promised the old ones. The handle therefore carries the
        roster and treasury version that were on screen, and
        `split_relative_interaction` refuses to commit against a different one
        without a second, explicit confirmation.
        """
        normalized = _validate_nonnegative_amounts(amounts, electrum_enabled=self.electrum_enabled)
        with self.store.transaction() as connection:
            preview = self._describe_split(connection, normalized)
            handle_id = self.handles.create_in_transaction(
                connection,
                workflow_type="treasury",
                action="split-relative",
                actor_id=actor_id,
                payload={"amounts": normalized},
                read_set_snapshot={
                    "treasury_version": preview["treasury_version"],
                    "recipients": [recipient["id"] for recipient in preview["recipients"]],
                },
                single_use=True,
                ttl_seconds=300,
            )
        return {"handle_id": handle_id, **preview}

    def _describe_split(self, connection: Any, amounts: Mapping[str, int]) -> dict[str, Any]:
        """The arithmetic of a split, shared by the preview and the commit.

        Both go through here so a preview cannot promise a share the commit
        would not pay.
        """
        treasury_row = connection.execute(
            "SELECT cp, sp, ep, gp, pp, version FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
        ).fetchone()
        if treasury_row is None:
            raise CurrencyError("treasury balance is missing")
        recipients = [
            {"id": str(row["id"]), "name": row["name"]}
            for row in connection.execute(
                "SELECT id, name FROM characters WHERE lifecycle = 'ACTIVE' ORDER BY name, id"
            ).fetchall()
        ]
        if not recipients:
            raise CurrencyError("at least one active character is required")
        treasury = currency_from_row(treasury_row)
        if any(amounts[denomination] > treasury[denomination] for denomination in CURRENCY_DENOMINATIONS):
            raise CurrencyError("treasury does not contain enough currency")
        share = len(recipients)
        per_recipient = {denomination: amounts[denomination] // share for denomination in CURRENCY_DENOMINATIONS}
        # Specification 33.1: each denomination splits independently and the
        # indivisible remainder stays with the source rather than being debited.
        remainder = {denomination: amounts[denomination] % share for denomination in CURRENCY_DENOMINATIONS}
        distributed = {
            denomination: per_recipient[denomination] * share for denomination in CURRENCY_DENOMINATIONS
        }
        return {
            "treasury": treasury,
            "treasury_version": int(treasury_row["version"]),
            "amounts": dict(amounts),
            "recipients": recipients,
            "per_recipient": per_recipient,
            "remainder": remainder,
            "distributed": distributed,
        }

    def split_relative_interaction(
        self,
        interaction_id: str,
        *,
        handle_id: str,
        actor_id: str | None,
        confirm_current: bool = False,
    ) -> ReceiptResult:
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="treasury-split",
            mutation=lambda connection, operation_id: self.handles.consume_and_mutate_in_transaction(
                connection,
                handle_id,
                actor_id=actor_id,
                mutation=lambda transaction, handle: self._split_relative_in_transaction(
                    transaction, operation_id, actor_id, handle, confirm_current
                ),
            ),
        )

    def _split_relative_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        handle: Any,
        confirm_current: bool,
    ) -> dict[str, Any]:
        if handle.workflow_type != "treasury" or handle.action != "split-relative":
            raise CurrencyError("handle is not a treasury split handle")
        treasury = connection.execute(
            "SELECT version FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
        ).fetchone()
        recipients = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM characters WHERE lifecycle = 'ACTIVE' ORDER BY name, id"
            ).fetchall()
        ]
        current_snapshot = {
            "treasury_version": int(treasury["version"]) if treasury else None,
            "recipients": recipients,
        }
        if not confirm_current and current_snapshot != handle.read_set_snapshot:
            raise CurrencySemanticStaleness(
                "treasury or active recipient set changed; confirm the split against current state"
            )
        return self._split_in_transaction(
            connection,
            operation_id,
            actor_id,
            handle.payload["amounts"],
        )

    def _split_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        amounts: Mapping[str, int],
    ) -> dict[str, Any]:
        described = self._describe_split(connection, amounts)
        before = described["treasury"]
        characters = described["recipients"]
        per_recipient = described["per_recipient"]
        remainder = described["remainder"]
        distributed = described["distributed"]
        after = {
            denomination: before[denomination] - distributed[denomination]
            for denomination in CURRENCY_DENOMINATIONS
        }
        now = iso_now()
        connection.execute(
            """UPDATE currency_balances
                  SET cp = ?, sp = ?, ep = ?, gp = ?, pp = ?, version = version + 1, updated_at = ?
                WHERE owner_type = 'PARTY' AND owner_id = 'party'""",
            (after["cp"], after["sp"], after["ep"], after["gp"], after["pp"], now),
        )
        for character in characters:
            current = read_balance(connection, "CHARACTER", str(character["id"]))
            updated = {
                denomination: current[denomination] + per_recipient[denomination]
                for denomination in CURRENCY_DENOMINATIONS
            }
            write_balance(connection, "CHARACTER", str(character["id"]), updated, now)
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="TREASURY_SPLIT",
            payload={
                "before": before,
                "split": dict(amounts),
                "distributed": distributed,
                "per_recipient": per_recipient,
                "remainder": remainder,
                "recipients": characters,
                "after": after,
            },
            destination=session_event_destination(connection),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {
            "status": "SPLIT",
            "before": before,
            "split": dict(amounts),
            "distributed": distributed,
            "per_recipient": per_recipient,
            "remainder": remainder,
            "recipients": characters,
            "after": after,
        }

    def give_to_character_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        character_id: str,
        amounts: Mapping[str, int],
    ) -> ReceiptResult:
        normalized = _validate_nonnegative_amounts(amounts, electrum_enabled=self.electrum_enabled)
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="currency-transfer",
            mutation=lambda connection, operation_id: self._give_in_transaction(
                connection, operation_id, actor_id, character_id, normalized
            ),
        )

    def _give_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        character_id: str,
        amounts: Mapping[str, int],
    ) -> dict[str, Any]:
        character = connection.execute(
            "SELECT id, name, lifecycle FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        if character is None:
            raise CurrencyError("character not found")
        if character["lifecycle"] != "ACTIVE":
            raise CurrencyError("only active characters can receive currency")
        treasury_row = connection.execute(
            "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
        ).fetchone()
        if treasury_row is None:
            raise CurrencyError("treasury balance is missing")
        before = currency_from_row(treasury_row)
        if any(amounts[denomination] > before[denomination] for denomination in CURRENCY_DENOMINATIONS):
            raise CurrencyError("treasury does not contain enough currency")
        character_before = read_balance(connection, "CHARACTER", character_id)
        after = {denomination: before[denomination] - amounts[denomination] for denomination in CURRENCY_DENOMINATIONS}
        character_after = {
            denomination: character_before[denomination] + amounts[denomination]
            for denomination in CURRENCY_DENOMINATIONS
        }
        now = iso_now()
        connection.execute(
            """UPDATE currency_balances
                  SET cp = ?, sp = ?, ep = ?, gp = ?, pp = ?, version = version + 1, updated_at = ?
                WHERE owner_type = 'PARTY' AND owner_id = 'party'""",
            (after["cp"], after["sp"], after["ep"], after["gp"], after["pp"], now),
        )
        write_balance(connection, "CHARACTER", character_id, character_after, now)
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="CURRENCY_TRANSFERRED",
            payload={
                "character_id": character_id,
                "character_name": character["name"],
                "amount": dict(amounts),
                "treasury_after": after,
                "character_after": character_after,
            },
            destination=session_event_destination(connection),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {
            "status": "TRANSFERRED",
            "character_id": character_id,
            "character_name": character["name"],
            "amount": dict(amounts),
            "treasury_after": after,
            "character_after": character_after,
        }

    def give_from_character_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        amounts: Mapping[str, int],
        destination: str = "party",
    ) -> ReceiptResult:
        """Move coin out of the actor's active character, back or on.

        Until this existed currency only travelled towards a living character.
        A split credits every active character, **Give to…** credits one, and
        the single debit in the product — belongings resolution — refuses an
        active character on purpose. So an active character's balance could
        only ever rise, and a mistyped give, 90 gp where the DM meant 9, was
        permanent.

        What made that worse than the equivalent item mistake is the repair. A
        DM reaching for **Adjust…** to put the 81 gp back does not return it,
        because adjust only touches the party row: the character keeps the coin
        and the campaign ends the evening 81 gp richer than it started. This is
        the coin counterpart of My Items, and it exists for the same reason —
        a table needs conservation, not a second way to mint.
        """
        normalized = _validate_nonnegative_amounts(amounts, electrum_enabled=self.electrum_enabled)
        normalized_destination = destination.strip()
        if not normalized_destination:
            raise CurrencyError("a destination is required")
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="currency-transfer",
            mutation=lambda connection, operation_id: self._give_from_character_in_transaction(
                connection, operation_id, actor_id, normalized, normalized_destination
            ),
        )

    def _give_from_character_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        amounts: Mapping[str, int],
        destination: str,
    ) -> dict[str, Any]:
        giver = active_claimant(connection, actor_id)
        if giver is None:
            raise CurrencyError("an active registered character is required to give currency")
        giver_id = str(giver["id"])
        held = read_balance(connection, "CHARACTER", giver_id)
        if any(amounts[denomination] > held[denomination] for denomination in CURRENCY_DENOMINATIONS):
            raise CurrencyError(f"{giver['name']} is carrying only {format_currency(held)}")

        if destination.casefold() == "party":
            destination_type, destination_id, destination_name = "PARTY", "party", "the treasury"
        else:
            recipient = connection.execute(
                "SELECT id, name, lifecycle FROM characters WHERE id = ?", (destination,)
            ).fetchone()
            if recipient is None:
                raise CurrencyError("recipient character not found")
            if recipient["lifecycle"] != "ACTIVE":
                raise CurrencyError("only active characters can receive currency")
            if str(recipient["id"]) == giver_id:
                raise CurrencyError("the recipient must differ from the giver")
            destination_type, destination_id = "CHARACTER", str(recipient["id"])
            destination_name = str(recipient["name"])

        now = iso_now()
        giver_after = {
            denomination: held[denomination] - amounts[denomination]
            for denomination in CURRENCY_DENOMINATIONS
        }
        destination_before = read_balance(connection, destination_type, destination_id)
        destination_after = {
            denomination: destination_before[denomination] + amounts[denomination]
            for denomination in CURRENCY_DENOMINATIONS
        }
        write_balance(connection, "CHARACTER", giver_id, giver_after, now)
        write_balance(connection, destination_type, destination_id, destination_after, now)
        result = {
            "status": "GIVEN",
            "character_id": giver_id,
            "character_name": str(giver["name"]),
            "amount": dict(amounts),
            "character_after": giver_after,
            "destination_type": destination_type,
            "destination_id": destination_id,
            "destination_name": destination_name,
            "destination_after": destination_after,
        }
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="CURRENCY_GIVEN",
            payload=result,
            destination=session_event_destination(connection),
        )
        # The pinned surface renders the treasury, not a character's purse, so
        # it only changes when the party is one end of the transfer.
        if destination_type == "PARTY":
            mark_projection_dirty(
                connection, target_id="party-stash", target_type="STATE", destination="party-inventory"
            )
        return result
