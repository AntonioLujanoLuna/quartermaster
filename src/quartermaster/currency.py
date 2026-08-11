"""Integer-only treasury currency operations."""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, mark_projection_dirty, session_event_destination
from .receipts import ReceiptRepository, ReceiptResult


CURRENCY_DENOMINATIONS = ("cp", "sp", "ep", "gp", "pp")
VISIBLE_DENOMINATIONS = ("cp", "sp", "gp", "pp")


class CurrencyError(RuntimeError):
    """Raised for invalid or impossible currency operations."""


def empty_currency() -> dict[str, int]:
    return {denomination: 0 for denomination in CURRENCY_DENOMINATIONS}


def currency_from_row(row: Any) -> dict[str, int]:
    return {denomination: int(row[denomination]) for denomination in CURRENCY_DENOMINATIONS}


def format_currency(balance: Mapping[str, int], *, include_electrum: bool = False) -> str:
    denominations = VISIBLE_DENOMINATIONS
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
    if not any(normalized.values()):
        raise CurrencyError("at least one currency denomination must change")
    return normalized


class CurrencyService:
    def __init__(
        self,
        store: SQLiteStore,
        receipts: ReceiptRepository,
        *,
        electrum_enabled: bool = False,
    ) -> None:
        self.store = store
        self.receipts = receipts
        self.electrum_enabled = electrum_enabled

    def view_treasury(self) -> dict[str, int]:
        with self.store.connection_lock:
            row = self.store._require_connection().execute(
                "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
            ).fetchone()
        if row is None:
            raise CurrencyError("treasury balance is missing")
        return currency_from_row(row)

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

    def split_treasury_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        amounts: Mapping[str, int],
    ) -> ReceiptResult:
        normalized = _validate_nonnegative_amounts(amounts, electrum_enabled=self.electrum_enabled)
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="treasury-split",
            mutation=lambda connection, operation_id: self._split_in_transaction(
                connection, operation_id, actor_id, normalized
            ),
        )

    def _split_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        amounts: Mapping[str, int],
    ) -> dict[str, Any]:
        treasury_row = connection.execute(
            "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
        ).fetchone()
        if treasury_row is None:
            raise CurrencyError("treasury balance is missing")
        characters = connection.execute(
            "SELECT id, name FROM characters WHERE lifecycle = 'ACTIVE' ORDER BY name, id"
        ).fetchall()
        if not characters:
            raise CurrencyError("at least one active character is required")
        before = currency_from_row(treasury_row)
        if any(amounts[denomination] > before[denomination] for denomination in CURRENCY_DENOMINATIONS):
            raise CurrencyError("treasury does not contain enough currency")
        after = {denomination: before[denomination] - amounts[denomination] for denomination in CURRENCY_DENOMINATIONS}
        per_recipient = {
            denomination: amounts[denomination] // len(characters) for denomination in CURRENCY_DENOMINATIONS
        }
        remainder = {
            denomination: amounts[denomination] % len(characters) for denomination in CURRENCY_DENOMINATIONS
        }
        now = iso_now()
        connection.execute(
            """UPDATE currency_balances
                  SET cp = ?, sp = ?, ep = ?, gp = ?, pp = ?, version = version + 1, updated_at = ?
                WHERE owner_type = 'PARTY' AND owner_id = 'party'""",
            (after["cp"], after["sp"], after["ep"], after["gp"], after["pp"], now),
        )
        for character in characters:
            existing = connection.execute(
                "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'CHARACTER' AND owner_id = ?",
                (character["id"],),
            ).fetchone()
            current = currency_from_row(existing) if existing else empty_currency()
            updated = {
                denomination: current[denomination] + per_recipient[denomination]
                for denomination in CURRENCY_DENOMINATIONS
            }
            connection.execute(
                """INSERT INTO currency_balances(owner_type, owner_id, cp, sp, ep, gp, pp, version, updated_at)
                   VALUES ('CHARACTER', ?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(owner_type, owner_id) DO UPDATE SET
                       cp = excluded.cp, sp = excluded.sp, ep = excluded.ep, gp = excluded.gp, pp = excluded.pp,
                       version = currency_balances.version + 1, updated_at = excluded.updated_at""",
                (character["id"], updated["cp"], updated["sp"], updated["ep"], updated["gp"], updated["pp"], now),
            )
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="TREASURY_SPLIT",
            payload={
                "before": before,
                "split": dict(amounts),
                "per_recipient": per_recipient,
                "remainder": remainder,
                "recipients": [{"id": character["id"], "name": character["name"]} for character in characters],
                "after": after,
            },
            destination=session_event_destination(connection),
        )
        mark_projection_dirty(connection, target_id="party-stash", target_type="STATE", destination="party-inventory")
        return {
            "status": "SPLIT",
            "before": before,
            "split": dict(amounts),
            "per_recipient": per_recipient,
            "remainder": remainder,
            "recipients": [{"id": character["id"], "name": character["name"]} for character in characters],
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
        character_row = connection.execute(
            "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'CHARACTER' AND owner_id = ?",
            (character_id,),
        ).fetchone()
        character_before = currency_from_row(character_row) if character_row else empty_currency()
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
        connection.execute(
            """INSERT INTO currency_balances(owner_type, owner_id, cp, sp, ep, gp, pp, version, updated_at)
               VALUES ('CHARACTER', ?, ?, ?, ?, ?, ?, 1, ?)
               ON CONFLICT(owner_type, owner_id) DO UPDATE SET
                   cp = excluded.cp, sp = excluded.sp, ep = excluded.ep, gp = excluded.gp, pp = excluded.pp,
                   version = currency_balances.version + 1, updated_at = excluded.updated_at""",
            (character_id, character_after["cp"], character_after["sp"], character_after["ep"], character_after["gp"], character_after["pp"], now),
        )
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
