"""Human-readable export of canonical state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .currency import currency_from_row, format_currency
from .db import SCHEMA_VERSION, SQLiteStore


def render_export(store: SQLiteStore) -> str:
    with store.read() as connection:
        return _render_export(connection)


def _render_export(connection: Any) -> str:
    active = connection.execute(
        "SELECT id, session_number, started_at FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    previous = connection.execute(
        "SELECT session_number, ended_at, where_ended FROM sessions WHERE status = 'CLOSED' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    stacks = connection.execute(
        "SELECT item_name, quantity, provenance, owner_type, owner_id FROM inventory_stacks ORDER BY item_name, id"
    ).fetchall()
    treasury = connection.execute(
        "SELECT cp, sp, ep, gp, pp FROM currency_balances WHERE owner_type = 'PARTY' AND owner_id = 'party'"
    ).fetchone()
    character_currency = connection.execute(
        """SELECT characters.name, characters.lifecycle, balances.cp, balances.sp, balances.ep, balances.gp, balances.pp
             FROM characters
             JOIN currency_balances AS balances
               ON balances.owner_type = 'CHARACTER' AND balances.owner_id = characters.id
            ORDER BY characters.name, characters.id"""
    ).fetchall()
    encounters = (
        connection.execute(
            """SELECT status, channel_id, opened_at, closed_at, closed_reason, outcome
                 FROM combat_encounters
                WHERE session_id = ?
             ORDER BY opened_at""",
            (active["id"],),
        ).fetchall()
        if active is not None
        else []
    )
    receipt_counts = connection.execute(
        "SELECT status, COUNT(*) AS count FROM interaction_receipts GROUP BY status ORDER BY status"
    ).fetchall()
    history = connection.execute(
        "SELECT event_type, payload, created_at FROM ledger_entries ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    generated = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    lines = [
        "# Quartermaster Export",
        "",
        f"Export timestamp: {generated}",
        f"Schema version: {SCHEMA_VERSION}",
        "",
        "## Party Stash",
        "",
    ]
    if stacks:
        lines.extend(
            f"- {row['item_name']} x{row['quantity']} ({row['owner_type']}:{row['owner_id']})"
            + (f" — {row['provenance']}" if row["provenance"] else "")
            for row in stacks
        )
    else:
        lines.append("- No inventory recorded yet.")
    lines.extend(["", "## Treasury", ""])
    lines.append(f"- {format_currency(currency_from_row(treasury), include_electrum=True)}" if treasury else "- Treasury is not initialized.")
    if character_currency:
        lines.extend(["", "### Character currency", ""])
        lines.extend(
            f"- {row['name']} [{row['lifecycle']}]: {format_currency(currency_from_row(row), include_electrum=True)}"
            for row in character_currency
        )
    lines.extend(["", "## Session", ""])
    if active:
        lines.append(f"- Active session: {active['session_number']} (started {active['started_at']}).")
    else:
        lines.append("- No active session.")
    if previous:
        endpoint = f"; where ended: {previous['where_ended']}" if previous["where_ended"] else ""
        lines.append(f"- Previous session: {previous['session_number']} (ended {previous['ended_at']}{endpoint}).")
    if encounters:
        # Combat encounters are continuity, not mechanics: when the table was in
        # a fight and how it resolved. Avrae keeps everything that happened
        # inside it.
        lines.extend(["", "### Combat encounters this session", ""])
        for row in encounters:
            if row["status"] == "OPEN":
                lines.append(f"- Open since {row['opened_at']} in channel {row['channel_id']}.")
                continue
            note = f"; outcome: {row['outcome']}" if row["outcome"] else ""
            lines.append(
                f"- {row['opened_at']} to {row['closed_at']} in channel {row['channel_id']}"
                f" ({row['closed_reason']}){note}."
            )
    lines.extend(["", "## Interaction receipts", ""])
    if receipt_counts:
        lines.extend(f"- {row['status']}: {row['count']}" for row in receipt_counts)
    else:
        lines.append("- No interaction receipts.")
    lines.extend(["", "## Recent relevant history", ""])
    if history:
        lines.extend(f"- {row['created_at']} — {row['event_type']}: {row['payload']}" for row in history)
    else:
        lines.append("- No ledger events.")
    lines.extend(["", "This export is generated from SQLite canonical state; Discord messages are disposable projections.", ""])
    return "\n".join(lines)
