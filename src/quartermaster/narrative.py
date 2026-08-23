"""One narrative rendering of a domain event, for every surface that reads one.

Two surfaces put domain events in front of a person: the session log, delivered
from the outbox as it happens, and the export's history, read back afterwards or
during an outage. They were rendering the same events by different rules — the
session log through a table of sentences, the export by printing the stored JSON
payload — so the document the surfaces call "the full record" was the one place
internal UUIDs and Discord user IDs were read out. This module is the single
table both go through, for the same reason `credit_stack` is the single merge
rule: two copies of a rendering are two chances to disagree about what an event
means.

`render_event` is total. A renderer quotes payload keys, payloads are written by
whatever version of the code appended them, and the export renders every ledger
row a campaign has ever accumulated — so a key that moved between versions must
degrade to the raw payload rather than raise. On the delivery side the same
guarantee matters more: an event that raises here fails its delivery
identically every attempt, which costs it eight retries and a dead letter while
the per-destination FIFO gate holds every later event in that thread behind it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .combat import format_duration
from .currency import format_currency


def _item_consumed_line(payload: Mapping[str, Any]) -> str:
    """The one event that says something left the campaign.

    It reads differently at each end because it means differently: a player
    using up what they carry is play, and a DM taking something out of the
    shared stash is a correction the rest of the table should be able to see
    and query. The reason is said whenever there is one, which is most of the
    time for the second and almost never for the first.
    """
    if payload.get("owner_type") == "PARTY":
        sentence = (
            f"{payload['quantity']} {payload['item_name']} removed from the Party Stash. "
            f"{payload['remaining']} remain."
        )
    else:
        sentence = (
            f"{payload['owner_name']} used {payload['quantity']} {payload['item_name']}. "
            f"{payload['remaining']} left."
        )
    return sentence + (f" — {payload['reason']}" if payload.get("reason") else "")


def _combat_closed_line(payload: Mapping[str, Any]) -> str:
    ran = format_duration(payload.get("elapsed_seconds"))
    sentence = "Combat closed" + (f" after {ran}" if ran else "")
    if payload.get("reason") == "SESSION_CLOSED":
        sentence += " with the session"
    return sentence + (f": {payload['outcome']}." if payload.get("outcome") else ".")


def _dice_rolled_line(payload: Mapping[str, Any]) -> str:
    expression = payload.get("expression", "dice")
    label = payload.get("label") or expression
    mode = payload.get("mode")
    qualifier = f" ({mode})" if mode in {"advantage", "disadvantage"} else ""
    return f"{label}: {payload['total']} from {expression}{qualifier}."


# Every event that reaches a person renders through this table. An event type
# that is missing from it still delivers and still exports, as its raw JSON
# payload — which is how a Discord user ID or an internal UUID ends up read out
# at the table — so `test_every_domain_event_type_has_a_renderer` fails the
# build rather than letting a new event ship that way.
EVENT_RENDERERS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "ITEM_GRANTED": lambda payload: f"DM added {payload['quantity']} {payload['item_name']}.",
    "ITEM_TAKEN": lambda payload: (
        f"A player took {payload['quantity']} {payload['item_name']}. {payload['remaining']} remain."
    ),
    "ITEM_CONSUMED": _item_consumed_line,
    "ITEM_GIVEN": lambda payload: (
        f"{payload['character_name']} gave {payload['quantity']} {payload['item_name']} "
        f"to {payload['destination_name']}."
    ),
    "SESSION_STARTED": lambda payload: f"Session {payload['session_number']} started.",
    "SESSION_CLOSED": lambda payload: f"Session {payload['session_number']} closed.",
    "LOOT_DROP_CREATED": lambda payload: f"New Loot Drop created ({len(payload['items'])} item entries).",
    "LOOT_CLAIMED": lambda payload: (
        f"A player claimed {payload['quantity']} {payload['item_name']} from a Loot Drop."
    ),
    "LOOT_DROP_CLOSED": lambda payload: f"Loot Drop closed ({payload['reason']}).",
    "TREASURY_ADJUSTED": lambda payload: f"Treasury updated: {format_currency(payload['after'])}.",
    "TREASURY_SPLIT": lambda payload: (
        f"Treasury split among {len(payload['recipients'])} active characters."
    ),
    "CURRENCY_TRANSFERRED": lambda payload: f"Currency given to {payload['character_name']}.",
    "CURRENCY_GIVEN": lambda payload: (
        f"{payload['character_name']} gave {format_currency(payload['amount'])} "
        f"to {payload['destination_name']}."
    ),
    "BELONGINGS_RESOLVED": lambda payload: (
        f"Belongings resolved from {payload['source_character_name']} to {payload['destination_name']}."
    ),
    "CHARACTER_CREATED": lambda payload: f"{payload['name']} joined the roster.",
    "CHARACTER_LIFECYCLE_CHANGED": lambda payload: (
        f"{payload['name']} moved from {payload['from']} to {payload['to']}."
    ),
    "COMBAT_OPENED": lambda payload: f"Combat opened in <#{payload['channel_id']}>.",
    "COMBAT_CLOSED": _combat_closed_line,
    "DICE_ROLLED": _dice_rolled_line,
}


def render_event(event_type: str, payload: Mapping[str, Any]) -> str | None:
    """Render one event as a sentence, or None when nothing can render it.

    None means the caller must fall back to the payload itself: an event nobody
    can render is still an event that happened, and losing it is worse than
    printing it badly.
    """
    renderer = EVENT_RENDERERS.get(event_type)
    if renderer is None:
        return None
    try:
        return renderer(payload)
    except (KeyError, IndexError, TypeError, ValueError):
        # The payload does not carry what this renderer expects — an event
        # appended by an older build, or one whose shape moved underneath it.
        return None


def render_entry(event_type: str, payload: Any) -> str:
    """Render one stored ledger row, however little sense its payload makes.

    A ledger row holds its payload as JSON text, and every surface that reads
    history back — the export, the continuity recap — has to decode it, render
    it, and survive the row that predates the renderer. Doing that in each of
    them is how one of them ends up printing raw payloads at the table, which
    is the defect this module was written to close.
    """
    try:
        decoded = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        decoded = None
    sentence = render_event(event_type, decoded) if isinstance(decoded, dict) else None
    return sentence if sentence is not None else f"{event_type}: {payload}"
