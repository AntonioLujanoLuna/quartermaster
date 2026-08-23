"""Bounded, server-authoritative dice rolls for the Activity.

This is deliberately a utility layer, not a D&D rules engine. It can roll a
table expression and preserve an explainable result, while attacks, spells,
and their modifiers remain behind the Avrae authority boundary.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .db import SQLiteStore
from .events import append_event, session_event_destination
from .receipts import ReceiptRepository, ReceiptResult

__all__ = ["DiceRollError", "DiceService", "parse_expression", "roll_expression"]

RollMode = Literal["normal", "advantage", "disadvantage"]
Visibility = Literal["PUBLIC", "DM_ONLY"]

MAX_EXPRESSION_LENGTH = 40
MAX_DICE = 20
MAX_SIDES = 100
MAX_MODIFIER = 100

_EXPRESSION = re.compile(
    r"^(?P<count>\d+)?d(?P<sides>\d+)(?:\s*(?P<sign>[+-])\s*(?P<modifier>\d+))?$",
    re.IGNORECASE,
)


class DiceRollError(ValueError):
    """Raised when a requested expression is outside the supported grammar."""


@dataclass(frozen=True)
class DiceExpression:
    count: int
    sides: int
    modifier: int

    @property
    def normalized(self) -> str:
        sign = f"+{self.modifier}" if self.modifier > 0 else str(self.modifier) if self.modifier else ""
        count = "" if self.count == 1 else str(self.count)
        return f"{count}d{self.sides}{sign}"


def parse_expression(expression: str) -> DiceExpression:
    """Parse the intentionally small expression grammar without evaluation."""

    if not isinstance(expression, str):
        raise DiceRollError("a dice expression is required")
    raw = expression.strip()
    if not raw:
        raise DiceRollError("a dice expression is required")
    if len(raw) > MAX_EXPRESSION_LENGTH:
        raise DiceRollError(f"dice expressions are limited to {MAX_EXPRESSION_LENGTH} characters")
    match = _EXPRESSION.fullmatch(raw)
    if match is None:
        raise DiceRollError("use a bounded expression such as d20, 2d6+3, or 1d20-1")

    count = int(match.group("count") or 1)
    sides = int(match.group("sides"))
    modifier = int(match.group("modifier") or 0)
    if match.group("sign") == "-":
        modifier = -modifier
    if not 1 <= count <= MAX_DICE:
        raise DiceRollError(f"a roll may contain between 1 and {MAX_DICE} dice")
    if not 2 <= sides <= MAX_SIDES:
        raise DiceRollError(f"each die must have between 2 and {MAX_SIDES} sides")
    if abs(modifier) > MAX_MODIFIER:
        raise DiceRollError(f"the modifier must be between -{MAX_MODIFIER} and {MAX_MODIFIER}")
    return DiceExpression(count=count, sides=sides, modifier=modifier)


def _random_die(sides: int) -> int:
    return secrets.randbelow(sides) + 1


def roll_expression(
    expression: str,
    *,
    mode: RollMode = "normal",
    rng: Callable[[int], int] = _random_die,
) -> dict[str, Any]:
    """Roll an expression and return its full, explainable breakdown.

    Advantage and disadvantage are deliberately restricted to one d20. That
    prevents a syntax such as ``2d20`` from quietly acquiring two competing
    meanings and leaves future rules-specific behaviour to the provider layer.
    """

    parsed = parse_expression(expression)
    if mode not in {"normal", "advantage", "disadvantage"}:
        raise DiceRollError("mode must be normal, advantage, or disadvantage")
    if mode != "normal" and (parsed.count != 1 or parsed.sides != 20):
        raise DiceRollError("advantage and disadvantage apply to one d20 roll")

    attempts = 2 if mode != "normal" else 1
    dice: list[dict[str, Any]] = []
    totals: list[int] = []
    for _ in range(attempts):
        values = [rng(parsed.sides) for _ in range(parsed.count)]
        if any(not isinstance(value, int) or not 1 <= value <= parsed.sides for value in values):
            raise DiceRollError("the dice source returned an invalid die value")
        dice.append({"sides": parsed.sides, "values": values})
        totals.append(sum(values))

    selected = 0
    if mode == "disadvantage":
        selected = 0 if totals[0] <= totals[1] else 1
    elif mode == "advantage":
        selected = 0 if totals[0] >= totals[1] else 1

    natural = None
    if parsed.count == 1 and parsed.sides == 20:
        natural = dice[selected]["values"][0]

    return {
        "expression": parsed.normalized,
        "mode": mode,
        "dice": dice,
        "selected": selected if attempts == 2 else None,
        "modifier": parsed.modifier,
        "natural": natural,
        "total": totals[selected] + parsed.modifier,
    }


class DiceService:
    """Roll, record, and read public dice results over the shared store."""

    def __init__(self, store: SQLiteStore, receipts: ReceiptRepository) -> None:
        self.store = store
        self.receipts = receipts

    def roll_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str,
        expression: str,
        mode: RollMode,
        label: str | None,
        visibility: Visibility,
    ) -> ReceiptResult:
        def mutation(connection: Any, operation_id: str) -> dict[str, Any]:
            result = roll_expression(expression, mode=mode)
            normalized_label = label.strip() if isinstance(label, str) else ""
            payload = {
                **result,
                "label": normalized_label or None,
                "visibility": visibility,
            }
            recorded = False
            if visibility == "PUBLIC":
                session = connection.execute(
                    "SELECT id FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
                ).fetchone()
                if session is not None:
                    append_event(
                        connection,
                        operation_id=operation_id,
                        actor_id=actor_id,
                        event_type="DICE_ROLLED",
                        payload=payload,
                        destination=session_event_destination(connection, str(session["id"])),
                    )
                    recorded = True
            payload["recorded"] = recorded
            return payload

        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="dice_roll",
            mutation=mutation,
        )

    def public_rolls(self, *, limit: int = 20) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 100))
        with self.store.read() as connection:
            rows = connection.execute(
                """SELECT payload, created_at
                     FROM ledger_entries
                    WHERE event_type = 'DICE_ROLLED'
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?""",
                (bounded,),
            ).fetchall()
        rolls: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            rolls.append({**payload, "created_at": row["created_at"]})
        return {"rolls": rolls, "total": len(rolls)}
