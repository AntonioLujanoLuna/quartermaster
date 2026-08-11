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

