"""Human-readable export of canonical state."""

from __future__ import annotations

from datetime import datetime, timezone

from .currency import currency_from_row, format_currency
from .db import SCHEMA_VERSION, SQLiteStore


def render_export(store: SQLiteStore) -> str:
    with store.connection_lock:
        return _render_export(store)


def _render_export(store: SQLiteStore) -> str:
    connection = store._require_connection()
    active = connection.execute(
        "SELECT session_number, started_at FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
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
    receipt_counts = connection.execute(
        "SELECT status, COUNT(*) AS count FROM interaction_receipts GROUP BY status ORDER BY status"
    ).fetchall()
    history = connection.execute(
        "SELECT event_type, payload, created_at FROM ledger_entries ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

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
    lines.extend(["", "## Session", ""])
    if active:
        lines.append(f"- Active session: {active['session_number']} (started {active['started_at']}).")
    else:
        lines.append("- No active session.")
    if previous:
        endpoint = f"; where ended: {previous['where_ended']}" if previous["where_ended"] else ""
        lines.append(f"- Previous session: {previous['session_number']} (ended {previous['ended_at']}{endpoint}).")
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
