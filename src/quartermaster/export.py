"""Human-readable export of canonical state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .currency import currency_from_row, format_currency
from .db import SCHEMA_VERSION, SQLiteStore
from .narrative import render_entry

# What a reader wants from the history of a campaign with no session on record
# is the last thing that happened, not all of it.
_UNSCOPED_HISTORY_LIMIT = 25


def render_export(store: SQLiteStore) -> str:
    """The document, as the table reads it."""
    return _render(collect_export(store))


def export_document(store: SQLiteStore) -> dict[str, Any]:
    """The same document, as a machine reads it.

    The reason this is a second rendering of one collection rather than a
    second read: the export is what every truncated surface points at, and two
    queries answering the same question are two chances to disagree about what
    the campaign holds. `_collect` is the read; the Markdown below and the JSON
    the API serves are both views of what it returned.
    """
    return collect_export(store)


def collect_export(store: SQLiteStore) -> dict[str, Any]:
    with store.read() as connection:
        return _collect(connection)


def _owner_label(row: Any, character_names: dict[str, str]) -> str:
    """Name who holds a stack.

    Ownership of an item moves to a character on every take and every claim, so
    on a played campaign most stacks are character-held. Rendering that owner as
    a bare UUID makes the document that is supposed to be the readable record
    the one place the table cannot answer "who has the sword".
    """
    owner_id = str(row["owner_id"])
    if row["owner_type"] == "PARTY":
        return "Party Stash"
    name = character_names.get(owner_id)
    return f"held by {name}" if name else f"held by unknown character {owner_id}"


def _history(connection: Any, active: Any, previous: Any) -> tuple[list[Any], str]:
    """Read the history of the session the table is playing.

    "Recent" used to mean the last ten ledger rows in the campaign, which is a
    few minutes of a busy evening and says nothing about where those minutes
    sat. What a DM reads this document for — during an outage, or writing the
    session up afterwards — is what happened tonight, so the window is the
    played session: the active one, or the most recent closed one for as long
    as the next has not started. `ledger_entries` carries no session, but its
    timestamps and the session's are written by the same clock in the same
    format, so the session's own span is the window.
    """
    session = active if active is not None else previous
    if session is None:
        rows = connection.execute(
            "SELECT event_type, payload, created_at FROM ledger_entries ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (_UNSCOPED_HISTORY_LIMIT,),
        ).fetchall()
        return list(reversed(rows)), f"most recent {_UNSCOPED_HISTORY_LIMIT}"
    span = connection.execute(
        "SELECT session_number, started_at FROM sessions WHERE id = ?", (session["id"],)
    ).fetchone()
    # The window has no upper bound on purpose. The played session is the
    # newest one there is — an active session has nothing after it, and a
    # closed one is only "played" while no later session has started — so
    # everything from its start belongs to it. Closing a session writes
    # `ended_at` and appends SESSION_CLOSED from two consecutive clock reads,
    # and bounding the window by the first would drop the second: the export
    # of a session would be missing the line saying it ended.
    rows = connection.execute(
        "SELECT event_type, payload, created_at FROM ledger_entries WHERE created_at >= ? ORDER BY created_at, rowid",
        (span["started_at"],),
    ).fetchall()
    return list(rows), f"session {span['session_number']}"


def _history_line(row: Any) -> str:
    """Render one ledger entry the way the session log renders the same event.

    The export is what every truncated surface points at, so it cannot be the
    one place events are printed as their stored JSON: those payloads carry
    internal UUIDs and Discord user IDs, and a person reading the record of
    their own evening should not have to decode them. An entry nothing can
    render still appears, as its payload — losing it would be worse.
    """
    return render_entry(row["event_type"], row["payload"])


def _collect(connection: Any) -> dict[str, Any]:
    """Read the whole campaign once.

    Everything below this line is a view of what this returned. Nothing else in
    the export path touches the database.
    """
    active = connection.execute(
        "SELECT id, session_number, started_at FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    previous = connection.execute(
        "SELECT id, session_number, ended_at, where_ended, recording_url FROM sessions WHERE status = 'CLOSED' ORDER BY session_number DESC LIMIT 1"
    ).fetchone()
    stacks = connection.execute(
        "SELECT item_name, quantity, provenance, owner_type, owner_id FROM inventory_stacks ORDER BY item_name, id"
    ).fetchall()
    characters = connection.execute(
        "SELECT id, name, discord_user_id, lifecycle FROM characters ORDER BY name, id"
    ).fetchall()
    character_names = {str(row["id"]): str(row["name"]) for row in characters}
    open_drops = connection.execute(
        """SELECT loot.id, loot.expires_at, item.item_name, item.quantity, item.remaining_quantity
             FROM loot_drops AS loot
             LEFT JOIN loot_drop_items AS item ON item.drop_id = loot.id
            WHERE loot.status = 'OPEN'
         ORDER BY loot.created_at, item.created_at, item.id"""
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
    # Combat belongs to the session the table is playing — which is the closed
    # one for as long as the next has not started. Reading only the active
    # session meant the whole record vanished when the session ended, so the document
    # every truncated surface points at was empty of the fight it just held.
    encounter_session = active if active is not None else previous
    encounters = (
        connection.execute(
            """SELECT status, channel_id, opened_at, closed_at, closed_reason, outcome
                 FROM combat_encounters
                WHERE session_id = ?
             ORDER BY opened_at""",
            (encounter_session["id"],),
        ).fetchall()
        if encounter_session is not None
        else []
    )
    receipt_counts = connection.execute(
        "SELECT status, COUNT(*) AS count FROM interaction_receipts GROUP BY status ORDER BY status"
    ).fetchall()
    history, history_scope = _history(connection, active, previous)

    drops: list[dict[str, Any]] = []
    for row in open_drops:
        drop_id = str(row["id"])
        if not drops or drops[-1]["drop_id"] != drop_id:
            drops.append({"drop_id": drop_id, "expires_at": row["expires_at"], "items": []})
        if row["item_name"] is not None:
            drops[-1]["items"].append(
                {
                    "item_name": row["item_name"],
                    "quantity": row["quantity"],
                    "remaining_quantity": row["remaining_quantity"],
                }
            )

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "schema_version": SCHEMA_VERSION,
        "party_stash": [
            {
                "item_name": row["item_name"],
                "quantity": row["quantity"],
                "provenance": row["provenance"],
                "owner_type": row["owner_type"],
                "owner_id": str(row["owner_id"]),
                "holder": _owner_label(row, character_names),
            }
            for row in stacks
        ],
        "open_loot_drops": drops,
        "characters": [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "discord_user_id": row["discord_user_id"],
                "lifecycle": row["lifecycle"],
            }
            for row in characters
        ],
        "treasury": currency_from_row(treasury) if treasury else None,
        "character_currency": [
            {
                "name": row["name"],
                "lifecycle": row["lifecycle"],
                "balance": currency_from_row(row),
            }
            for row in character_currency
        ],
        "active_session": (
            {
                "session_number": active["session_number"],
                "started_at": active["started_at"],
            }
            if active
            else None
        ),
        "previous_session": (
            {
                "session_number": previous["session_number"],
                "ended_at": previous["ended_at"],
                "where_ended": previous["where_ended"],
                "recording_url": previous["recording_url"],
            }
            if previous
            else None
        ),
        "combat": (
            {
                "session_number": encounter_session["session_number"],
                "encounters": [
                    {
                        "status": row["status"],
                        "channel_id": row["channel_id"],
                        "opened_at": row["opened_at"],
                        "closed_at": row["closed_at"],
                        "closed_reason": row["closed_reason"],
                        "outcome": row["outcome"],
                    }
                    for row in encounters
                ],
            }
            if encounters
            else None
        ),
        "interaction_receipts": {row["status"]: row["count"] for row in receipt_counts},
        "history": {
            "scope": history_scope,
            "entries": [
                {
                    "created_at": row["created_at"],
                    "event_type": row["event_type"],
                    "line": _history_line(row),
                }
                for row in history
            ],
        },
    }


def _render(document: dict[str, Any]) -> str:
    stacks = document["party_stash"]
    drops = document["open_loot_drops"]
    characters = document["characters"]
    treasury = document["treasury"]
    character_currency = document["character_currency"]
    active = document["active_session"]
    previous = document["previous_session"]
    combat = document["combat"]
    receipt_counts = document["interaction_receipts"]
    history = document["history"]["entries"]

    lines = [
        "# Quartermaster Export",
        "",
        f"Export timestamp: {document['generated_at']}",
        f"Schema version: {document['schema_version']}",
        "",
        "## Party Stash",
        "",
    ]
    if stacks:
        lines.extend(
            f"- {row['item_name']} x{row['quantity']} ({row['holder']})"
            + (f" — {row['provenance']}" if row["provenance"] else "")
            for row in stacks
        )
    else:
        lines.append("- No inventory recorded yet.")
    # Every surface that has to drop entries tells the reader this document
    # holds the full record, so an open Loot Drop cannot be missing from it:
    # while a drop is open its items exist nowhere else, and the only reason
    # the Open Loot panel truncates is that there are enough of them to matter.
    lines.extend(["", "## Open Loot Drops", ""])
    if drops:
        for drop in drops:
            lines.append(f"- Drop {drop['drop_id']} (expires {drop['expires_at']})")
            if not drop["items"]:
                lines.append("  - No items recorded on this drop.")
                continue
            lines.extend(
                f"  - {item['item_name']}: {item['remaining_quantity']} unclaimed of {item['quantity']}"
                for item in drop["items"]
            )
    else:
        lines.append("- No open Loot Drops.")
    lines.extend(["", "## Characters", ""])
    if characters:
        lines.extend(
            f"- {row['name']} [{row['lifecycle']}] · `{row['id']}`"
            + (f" · Discord user {row['discord_user_id']}" if row["discord_user_id"] else "")
            for row in characters
        )
    else:
        lines.append("- No characters registered.")
    lines.extend(["", "## Treasury", ""])
    lines.append(f"- {format_currency(treasury, include_electrum=True)}" if treasury else "- Treasury is not initialized.")
    if character_currency:
        lines.extend(["", "### Character currency", ""])
        lines.extend(
            f"- {row['name']} [{row['lifecycle']}]: {format_currency(row['balance'], include_electrum=True)}"
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
        # The recording is the other half of "where we stopped", and the export
        # is the document somebody writing the evening up is already reading.
        if previous["recording_url"]:
            lines.append(f"- Recording: {previous['recording_url']}")
    if combat:
        # Combat encounters are continuity, not mechanics: when the table was in
        # a fight and how it resolved. Avrae keeps everything that happened
        # inside it.
        lines.extend(["", f"### Combat encounters in session {combat['session_number']}", ""])
        for row in combat["encounters"]:
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
        lines.extend(f"- {status}: {count}" for status, count in receipt_counts.items())
    else:
        lines.append("- No interaction receipts.")
    lines.extend(["", f"## Recent relevant history ({document['history']['scope']})", ""])
    if history:
        lines.extend(f"- {row['created_at']} — {row['line']}" for row in history)
    else:
        lines.append("- No ledger events.")
    lines.extend(["", "This export is generated from SQLite canonical state; Discord messages are disposable projections.", ""])
    return "\n".join(lines)
