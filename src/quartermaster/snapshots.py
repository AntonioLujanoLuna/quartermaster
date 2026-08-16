"""Cross-service read compositions, belonging to no particular surface.

Most reads a surface needs are one service call. A few — what the home screen
states, and nothing else so far — are six, composed into one answer. Those have
to live somewhere both the panels and the Activity API can reach, because the
alternative is two compositions that agree until one of them is edited.

Nothing here takes a Discord type or an adapter context. A composition that
knows what a panel is cannot be read by anything that is not one.
"""

from __future__ import annotations

from typing import Any

from .characters import CharacterService
from .currency import CurrencyService
from .inventory import InventoryService
from .loot import LootDropService
from .sessions import SessionService

__all__ = ["home_snapshot"]


def home_snapshot(
    *,
    inventory: InventoryService,
    loot: LootDropService,
    characters: CharacterService,
    currency: CurrencyService,
    sessions: SessionService,
    actor_id: str,
) -> dict[str, Any]:
    """Everything the home surface states, read in one worker-thread pass."""
    items = inventory.browse()
    drops = loot.list_open()
    roster = characters.list_characters()
    holdings = inventory.holdings(actor_id=actor_id)
    treasury = currency.view_treasury()
    purse = currency.purse(actor_id=actor_id)
    # One read of continuity rather than one of the active session: home has to
    # say whether the table is mid-session either way, and the endpoint of the
    # last one is the first thing anybody wants at the start of an evening. The
    # recap belongs to the Last time surface, so nothing here asks for one.
    continuity = sessions.continuity(limit=1)
    return {
        "stash_count": len(items),
        "drop_count": len(drops),
        "unclaimed": sum(int(item["remaining_quantity"]) for drop in drops for item in drop["items"]),
        "active_session_number": continuity["active_session_number"],
        "previous_session": continuity["previous"],
        "unresolved_estates": sum(1 for row in roster if row["lifecycle"] != "ACTIVE"),
        "treasury": treasury,
        "character": holdings["character"],
        "held_stacks": holdings["total_items"],
        "purse": purse["balance"],
    }
