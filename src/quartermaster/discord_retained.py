"""The small Discord surface retained after the Activity migration.

The Activity is the table surface.  The bot remains useful between sessions,
but its entry point should not keep presenting a second copy of every workflow
the Activity already owns.  This module deliberately keeps only asynchronous
browse views plus the DM's useful grant shortcut.

The former full panel implementation remains available behind the explicit
``legacy`` surface setting while the table moves over.  Nothing in this module
changes canonical domain behaviour; it only narrows the bot's active renderer.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import discord

from .discord_common import (
    Quartermaster,
    _actor_id,
    _is_dm,
    _render_stash,
    _require_guild,
    _run_fast,
    _send_panel,
)
from .discord_views import GrantLootModal, Opener, QuartermasterView
from .rendering import fit_discord_lines
from .snapshots import home_snapshot

RETAINED_PANEL_TIMEOUT = 600


def _open(panel, context: Quartermaster) -> Opener:
    return partial(panel, context=context)


class RetainedView(QuartermasterView):
    """A retained view with only a refresh and a route back to the home view."""

    def __init__(self, context: Quartermaster) -> None:
        super().__init__(
            context,
            timeout=RETAINED_PANEL_TIMEOUT,
            reopen=_open(open_home, context),
        )

    def add_home(self) -> None:
        self.add_navigation(
            _open(open_home, self.context),
            label="◀ Home",
            custom_id="qm:retained:home",
        )


def _render_home(snapshot: dict[str, Any], *, is_dm: bool) -> str:
    session = (
        f"Session {snapshot['active_session_number']} · in progress"
        if snapshot["active_session_number"] is not None
        else "No session in progress"
    )
    lines = ["**QUARTERMASTER**", "", session]
    previous = snapshot["previous_session"]
    if previous is not None and snapshot["active_session_number"] is None:
        lines.append(f"Last time · Session {previous['session_number']} · {previous['where_ended']}")
    lines.extend(
        [
            f"Party Stash · {snapshot['stash_count']} {'stack' if snapshot['stash_count'] == 1 else 'stacks'}",
            f"My Items · {snapshot['held_stacks']} {'stack' if snapshot['held_stacks'] == 1 else 'stacks'}",
            "",
            "The Activity is the table surface. This bot view keeps the asynchronous reads available.",
        ]
    )
    if is_dm:
        lines.append("Grant loot here when you are between sessions.")
    return fit_discord_lines(lines, label="Quartermaster")


class HomeView(RetainedView):
    def __init__(self, context: Quartermaster, *, is_dm: bool) -> None:
        super().__init__(context)
        self.add_navigation(
            _open(open_stash, context),
            label="Party Stash",
            style=discord.ButtonStyle.primary,
            custom_id="qm:retained:stash",
        )
        self.add_navigation(
            _open(open_my_items, context),
            label="My Items",
            custom_id="qm:retained:items",
        )
        self.add_navigation(
            _open(open_home, context),
            label="Refresh",
            custom_id="qm:retained:refresh",
        )
        if is_dm:
            grant = discord.ui.Button(
                label="Grant loot…",
                style=discord.ButtonStyle.primary,
                custom_id="qm:retained:grant",
            )

            async def on_grant(interaction: discord.Interaction) -> None:
                if await _is_dm(interaction, self.settings):
                    await interaction.response.send_modal(GrantLootModal(self.context))

            grant.callback = on_grant
            self.add_item(grant)


async def open_home(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction,
        context.settings,
        lambda: home_snapshot(
            inventory=context.inventory,
            loot=context.loot,
            characters=context.characters,
            currency=context.currency,
            sessions=context.sessions,
            actor_id=_actor_id(interaction),
        ),
        ephemeral=True,
    )
    await _send_panel(
        interaction,
        _render_home(execution.value, is_dm=await _is_dm(interaction, context.settings)),
        HomeView(context, is_dm=await _is_dm(interaction, context.settings)),
        execution=execution,
    )


class StashView(RetainedView):
    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context)
        self.add_navigation(
            _open(open_stash, context),
            label="Refresh",
            custom_id="qm:retained:stash:refresh",
        )
        self.add_home()


async def open_stash(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction,
        context.settings,
        context.inventory.browse,
        ephemeral=True,
    )
    await _send_panel(
        interaction,
        _render_stash(execution.value),
        StashView(context),
        execution=execution,
    )


def _render_holdings(holdings: dict[str, Any]) -> str:
    character = holdings["character"]
    if character is None:
        return (
            "**MY ITEMS**\n\n"
            "You have no active character registered, so there is no personal pack to read. "
            "Ask the DM to register one."
        )
    lines = [f"**{str(character['name']).upper()}'S ITEMS**", ""]
    items = holdings["items"]
    if not items:
        lines.append("You are not holding anything.")
    else:
        lines.extend(f"• {item['item_name']} x{item['quantity']}" for item in items)
        total = int(holdings["total_items"])
        if total > len(items):
            lines.extend(["", f"Showing {len(items)} of {total} stacks."])
    return fit_discord_lines(lines, label="My Items")


class MyItemsView(RetainedView):
    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context)
        self.add_navigation(
            _open(open_my_items, context),
            label="Refresh",
            custom_id="qm:retained:items:refresh",
        )
        self.add_home()


async def open_my_items(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction,
        context.settings,
        lambda: context.inventory.holdings(actor_id=_actor_id(interaction), limit=200),
        ephemeral=True,
    )
    await _send_panel(
        interaction,
        _render_holdings(execution.value),
        MyItemsView(context),
        execution=execution,
    )


__all__ = ["open_home"]
