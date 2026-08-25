"""The Quartermaster panel interface.

One slash command opens a panel; everything after that is pressed. The panels
here navigate — they read canonical state, render it, and hand the caller the
controls that fit what they are looking at. The controls themselves live in
`discord_views`, so a panel can pass a control the way back without the control
knowing anything about panels.

Two rules hold the surface together:

- **A panel renders only what the caller may press.** A player never sees a DM
  control, which is why the refusals here are rare rather than routine.
- **A control still checks.** A view outlives the render that built it, so
  every DM callback re-checks authorization; the render is ergonomics, the
  check is the boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import discord

from .avrae_handoff import AvraeHandoffError
from .characters import CharacterError
from .combat import CombatError
from .currency import CurrencyError, format_currency
from .discord_common import (
    Quartermaster,
    _actor_id,
    _endpoint_summary,
    _is_dm,
    _render_characters,
    _render_combat_closed,
    _render_combat_opened,
    _render_combat_status,
    _render_last_time,
    _render_loot,
    _render_stash,
    _require_dm,
    _require_guild,
    _rerender,
    _run_deferred,
    _run_fast,
    _send_deferred_backup,
    _send_deferred_export,
    _send_error,
    _send_execution,
    _send_panel,
)
from .discord_views import (
    MAX_VIEW_BUTTONS,
    PARTY_DESTINATION,
    CharacterAddModal,
    GiveCurrencyView,
    GiveItemView,
    GrantLootModal,
    LootClaimView,
    LootDropModal,
    Opener,
    QuartermasterModal,
    QuartermasterView,
    SessionEndModal,
    StashRemoveModal,
    TakeView,
    TreasuryAdjustModal,
    TreasuryGiveModal,
    TreasurySplitModal,
    render_give_coin,
    render_give_item,
)
from .export import render_export
from .inventory import InventoryError
from .loot import LootDropError
from .operations import create_scheduled_backup, health_report, render_health
from .rendering import fit_discord_lines
from .response import DeferredExecutionError
from .sessions import SessionError
from .snapshots import home_snapshot

PANEL_TIMEOUT = 600

#: The lifecycle states a DM can move a character to, in the order they read.
LIFECYCLE_CHOICES = (
    ("ACTIVE", "Playing at the table"),
    ("DEAD", "Died in the campaign"),
    ("RETIRED", "Retired from adventuring"),
    ("DEPARTED", "The player has left the table"),
)

SELECT_OPTION_LIMIT = 25


def _select_overflow(total: int, *, noun: str) -> str:
    """Say when a select menu is showing only part of what exists.

    A message that runs long says so; a select that runs long just stops, and
    the DM is left looking for a character the panel has quietly dropped.
    """
    if total <= SELECT_OPTION_LIMIT:
        return ""
    return f"\n\nShowing the first {SELECT_OPTION_LIMIT} of {total} {noun}."


Panel = Callable[[discord.Interaction, Quartermaster], Awaitable[None]]


def _open(panel: Panel, context: Quartermaster) -> Opener:
    """Bind a panel to its context so a control can call it with an interaction."""
    return partial(panel, context=context)


class PanelView(QuartermasterView):
    """A view that is somewhere the caller navigated to rather than a control.

    Every panel is reachable from home, so an expired one always has somewhere
    to send the caller: the way back out of an expiry is the same door they
    came in by, rendered against state as it stands rather than as it stood.
    """

    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context, timeout=PANEL_TIMEOUT, reopen=_open(open_home, context))

    def add_home(self, *, row: int | None = None) -> None:
        self.add_navigation(
            _open(open_home, self.context), label="◀ Home", custom_id="qm:nav:home", row=row
        )


# Home -----------------------------------------------------------------------


def _home_snapshot(context: Quartermaster, actor_id: str) -> dict[str, Any]:
    """The home composition, resolved out of the adapter's context.

    The composition itself lives in `snapshots` so the Activity API reads the
    same one; this is only the part that knows where a panel keeps its
    services.
    """
    return home_snapshot(
        inventory=context.inventory,
        loot=context.loot,
        characters=context.characters,
        currency=context.currency,
        sessions=context.sessions,
        actor_id=actor_id,
    )


def _render_home(snapshot: dict[str, Any], *, is_dm: bool) -> str:
    session = (
        f"Session {snapshot['active_session_number']} · in progress"
        if snapshot["active_session_number"] is not None
        else "No session in progress"
    )
    previous = snapshot["previous_session"]
    stacks = snapshot["stash_count"]
    drops = snapshot["drop_count"]
    lines = ["**QUARTERMASTER**", "", session]
    # Where the table stopped is what an evening opens with, so it sits under
    # the session line and only while it is still the last thing that happened:
    # once a session is running, the panel behind Last time is the place for it.
    if previous is not None and snapshot["active_session_number"] is None:
        lines.append(
            f"Last time · Session {previous['session_number']} · "
            f"{_endpoint_summary(previous['where_ended'])}"
        )
    lines.append(f"Party Stash · {stacks} {'stack' if stacks == 1 else 'stacks'}")
    if drops:
        lines.append(
            f"Open Loot · {drops} {'drop' if drops == 1 else 'drops'} · {snapshot['unclaimed']} unclaimed"
        )
    else:
        lines.append("Open Loot · nothing waiting")
    lines.append(f"Treasury · {format_currency(snapshot['treasury'])}")
    lines.append("")
    character = snapshot["character"]
    if character is None:
        lines.append(
            "You have no active character registered, so you cannot take or claim anything yet. "
            "Ask the DM to register one."
        )
    else:
        held = snapshot["held_stacks"]
        carrying = f"carrying {held} {'stack' if held == 1 else 'stacks'}" if held else "carrying nothing"
        lines.append(f"You are playing **{character['name']}**, {carrying}.")
        # A split moves coin out of the treasury line above and into a balance
        # the player owns. Saying so here is the only place they meet it before
        # they need it.
        purse = snapshot["purse"]
        if any(purse.values()):
            lines.append(f"Your coin · {format_currency(purse)}")
    if is_dm:
        estates = int(snapshot["unresolved_estates"] or 0)
        if estates:
            suffix = "estate" if estates == 1 else "estates"
            lines.append(f"{estates} unresolved character {suffix} waiting in DM Tools.")
    return "\n".join(lines)


class HomeView(PanelView):
    def __init__(self, context: Quartermaster, *, is_dm: bool, has_history: bool = False) -> None:
        super().__init__(context)
        self.add_navigation(
            _open(open_stash, context),
            label="Party Stash",
            style=discord.ButtonStyle.primary,
            custom_id="qm:home:stash",
            row=0,
        )
        self.add_navigation(
            _open(open_loot, context),
            label="Open Loot",
            style=discord.ButtonStyle.success,
            custom_id="qm:home:loot",
            row=0,
        )
        self.add_navigation(_open(open_my_items, context), label="My Items", custom_id="qm:home:items", row=0)
        self.add_navigation(_open(open_treasury, context), label="Treasury", custom_id="qm:home:treasury", row=1)
        self.add_navigation(_open(open_characters, context), label="Characters", custom_id="qm:home:characters", row=1)
        self.add_navigation(_open(open_combat, context), label="Combat", custom_id="qm:home:combat", row=1)
        if has_history:
            # Rendered only once there is a closed session to read back. A
            # control that opens a panel saying "nothing yet" is a control that
            # teaches the table to stop pressing it.
            self.add_navigation(
                _open(open_last_time, context), label="Last time", custom_id="qm:home:last", row=2
            )
        self.add_navigation(_open(open_home, context), label="Refresh", custom_id="qm:home:refresh", row=2)
        if is_dm:
            self.add_navigation(
                _open(open_dm_tools, context),
                label="DM Tools",
                style=discord.ButtonStyle.danger,
                custom_id="qm:home:dm",
                row=2,
            )


async def open_home(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    is_dm = await _is_dm(interaction, context.settings)
    execution = await _run_fast(
        interaction,
        context.settings,
        lambda: _home_snapshot(context, _actor_id(interaction)),
        ephemeral=True,
    )
    await _send_panel(
        interaction,
        _render_home(execution.value, is_dm=is_dm),
        HomeView(context, is_dm=is_dm, has_history=execution.value["previous_session"] is not None),
        execution=execution,
    )


# Last time ------------------------------------------------------------------


class LastTimeView(PanelView):
    """The continuity panel: read it, or go and read the log it came from."""

    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context)
        settings = context.settings
        if settings.session_log_channel_id:
            # The recap is the end of the ledger; the log is all of it, in the
            # channel the table watched it arrive in. A link is the only control
            # here that leads out of Quartermaster, which is why it is a link.
            self.add_item(
                discord.ui.Button(
                    label="Session log",
                    style=discord.ButtonStyle.link,
                    url=f"https://discord.com/channels/{settings.guild_id}/{settings.session_log_channel_id}",
                )
            )
        self.add_navigation(_open(open_last_time, context), label="Refresh", custom_id="qm:last:refresh")
        self.add_home()


async def open_last_time(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction, context.settings, context.sessions.continuity, ephemeral=True
    )
    await _send_panel(
        interaction, _render_last_time(execution.value), LastTimeView(context), execution=execution
    )


# Party Stash ----------------------------------------------------------------


class StashView(PanelView):
    def __init__(self, context: Quartermaster, *, has_items: bool) -> None:
        super().__init__(context)
        if has_items:
            self.add_navigation(
                _open(open_take, context),
                label="Take something…",
                style=discord.ButtonStyle.primary,
                custom_id="qm:stash:take",
            )
        self.add_navigation(_open(open_stash, context), label="Refresh", custom_id="qm:stash:refresh")
        self.add_home()


async def open_stash(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    try:
        execution = await _run_fast(interaction, context.settings, context.inventory.browse, ephemeral=True)
    except InventoryError as error:
        await _send_error(interaction, f"Party Stash could not be opened: {error}")
        return
    items = execution.value
    await _send_panel(
        interaction,
        _render_stash(items),
        StashView(context, has_items=bool(items)),
        execution=execution,
    )


async def open_take(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    try:
        execution = await _run_fast(
            interaction,
            context.settings,
            lambda: context.inventory.prepare_take_view(
                actor_id=_actor_id(interaction),
                # Refresh and the way back are controls too, and a view that
                # spends its whole budget on items leaves the player stranded
                # on a panel of consumed handles.
                control_budget=MAX_VIEW_BUTTONS - 2,
            ),
            ephemeral=True,
        )
    except InventoryError as error:
        await _send_error(interaction, f"Party Stash could not be opened: {error}")
        return
    prepared = execution.value
    await _send_panel(
        interaction,
        _render_stash(prepared["items"], total=prepared.get("total_items"), controls=prepared["handles"]),
        TakeView(
            context,
            prepared["items"],
            prepared["handles"],
            prepared["take_all_handles"],
            refresh=_open(open_take, context),
            back=_open(open_stash, context),
        ),
        execution=execution,
    )


# Loot Drops -----------------------------------------------------------------


async def open_loot(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    try:
        execution = await _run_fast(
            interaction,
            context.settings,
            lambda: context.loot.prepare_claim_view(
                actor_id=_actor_id(interaction), limit=MAX_VIEW_BUTTONS - 2
            ),
            ephemeral=True,
        )
    except LootDropError as error:
        await _send_error(interaction, f"Loot Drops could not be opened: {error}")
        return
    prepared = execution.value
    await _send_panel(
        interaction,
        _render_loot(prepared["drops"], prepared["handles"]),
        LootClaimView(
            context,
            prepared["drops"],
            prepared["handles"],
            refresh=_open(open_loot, context),
            back=_open(open_home, context),
        ),
        execution=execution,
    )


# My Items and giving --------------------------------------------------------


def _render_holdings(holdings: dict[str, Any]) -> str:
    character = holdings["character"]
    if character is None:
        return (
            "**MY ITEMS**\n\n"
            "You have no active character registered, so you are not holding anything. "
            "Ask the DM to register one."
        )
    lines = [f"**{str(character['name']).upper()}'S ITEMS**", ""]
    items = holdings["items"]
    if not items:
        lines.append("You are not holding anything. Take something from the Party Stash.")
        return "\n".join(lines)
    lines.extend(f"• {item['item_name']} x{item['quantity']}" for item in items)
    total = int(holdings["total_items"])
    if total > len(items):
        lines.append("")
        lines.append(f"Showing {len(items)} of {total} stacks.")
    lines.append("")
    lines.append("Choose one to hand it on, or to use it up.")
    return "\n".join(lines)


class MyItemsView(PanelView):
    def __init__(self, context: Quartermaster, items: list[dict]) -> None:
        super().__init__(context)
        if items:
            select = discord.ui.Select(
                placeholder="Pick something you are carrying…",
                options=[
                    discord.SelectOption(
                        label=str(item["item_name"])[:100],
                        value=str(item["id"]),
                        description=f"You hold {item['quantity']}",
                    )
                    for item in items[:SELECT_OPTION_LIMIT]
                ],
                custom_id="qm:items:pick",
                row=0,
            )
            select.callback = self._pick(select)
            self.add_item(select)
        self.add_navigation(_open(open_my_items, context), label="Refresh", custom_id="qm:items:refresh", row=1)
        self.add_home(row=1)

    def _pick(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            await open_give_item(interaction, self.context, stack_id=select.values[0])

        return callback


async def open_my_items(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction,
        context.settings,
        # The listing and the select have to agree about how much they show,
        # or the panel names a stack there is no option to choose.
        lambda: context.inventory.holdings(actor_id=_actor_id(interaction), limit=SELECT_OPTION_LIMIT),
        ephemeral=True,
    )
    holdings = execution.value
    await _send_panel(
        interaction,
        _render_holdings(holdings),
        MyItemsView(context, holdings["items"]),
        execution=execution,
    )


def _give_snapshot(context: Quartermaster, stack_id: str, actor_id: str) -> dict[str, Any]:
    prepared = context.inventory.create_give_handles(stack_id=stack_id, actor_id=actor_id)
    prepared["recipients"] = [
        {"id": str(row["id"]), "name": str(row["name"])}
        for row in context.characters.list_characters()
        if row["lifecycle"] == "ACTIVE" and str(row["id"]) != prepared["character"]["id"]
    ]
    return prepared


async def open_give_item(
    interaction: discord.Interaction,
    context: Quartermaster,
    *,
    stack_id: str,
) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    try:
        execution = await _run_fast(
            interaction,
            context.settings,
            lambda: _give_snapshot(context, stack_id, _actor_id(interaction)),
            ephemeral=True,
        )
    except InventoryError as error:
        await _send_error(interaction, f"That item could not be opened: {error}")
        return
    prepared = execution.value
    await _send_panel(
        interaction,
        render_give_item(prepared["item"], "the Party Stash"),
        GiveItemView(
            context,
            prepared["item"],
            prepared["handles"],
            prepared["recipients"],
            back=_open(open_my_items, context),
            destination=PARTY_DESTINATION,
        ),
        execution=execution,
    )


# Treasury -------------------------------------------------------------------


class TreasuryView(PanelView):
    def __init__(self, context: Quartermaster, *, is_dm: bool, has_coin: bool = False) -> None:
        super().__init__(context)
        if has_coin:
            self.add_navigation(
                _open(open_give_coin, context),
                label="My coin…",
                style=discord.ButtonStyle.primary,
                custom_id="qm:treasury:mycoin",
            )
        if is_dm:
            adjust = discord.ui.Button(
                label="Adjust…", style=discord.ButtonStyle.primary, custom_id="qm:treasury:adjust"
            )
            adjust.callback = self._adjust
            self.add_item(adjust)
            split = discord.ui.Button(
                label="Split…", style=discord.ButtonStyle.primary, custom_id="qm:treasury:split"
            )
            split.callback = self._split
            self.add_item(split)
            self.add_navigation(
                _open(open_treasury_give, context), label="Give to…", custom_id="qm:treasury:give"
            )
        self.add_navigation(_open(open_treasury, context), label="Refresh", custom_id="qm:treasury:refresh")
        self.add_home()

    async def _adjust(self, interaction: discord.Interaction) -> None:
        if await _require_dm(interaction, self.settings):
            await interaction.response.send_modal(TreasuryAdjustModal(self.context))

    async def _split(self, interaction: discord.Interaction) -> None:
        if await _require_dm(interaction, self.settings):
            await interaction.response.send_modal(TreasurySplitModal(self.context))


def _render_treasury(snapshot: dict[str, Any], *, is_dm: bool) -> str:
    """The party's money, and the caller's own.

    A player's coin used to appear nowhere on the surface — a split moved it
    somewhere only the DM's export could see. Showing both here is what makes
    the give control below legible: the number you are being offered a way to
    move is on the same screen as the control that moves it.
    """
    lines = ["**TREASURY**", "", format_currency(snapshot["treasury"])]
    character = snapshot["purse"]["character"]
    balance = snapshot["purse"]["balance"]
    # Said only when there is coin to say it about, which is the same condition
    # the give control appears under: a row of zeroes beside no control to press
    # is clutter on a panel every player opens.
    if character is not None and any(balance.values()):
        lines.extend(["", f"{character['name']} is carrying {format_currency(balance)}."])
    if is_dm:
        lines.extend(
            [
                "",
                "Split divides an amount evenly among every active character; the indivisible "
                "remainder stays here.",
            ]
        )
    return "\n".join(lines)


def _treasury_snapshot(context: Quartermaster, actor_id: str) -> dict[str, Any]:
    return {"treasury": context.currency.view_treasury(), "purse": context.currency.purse(actor_id=actor_id)}


async def open_treasury(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    is_dm = await _is_dm(interaction, context.settings)
    try:
        execution = await _run_fast(
            interaction,
            context.settings,
            lambda: _treasury_snapshot(context, _actor_id(interaction)),
            ephemeral=True,
        )
    except CurrencyError as error:
        await _send_error(interaction, f"Treasury could not be read: {error}")
        return
    snapshot = execution.value
    await _send_panel(
        interaction,
        _render_treasury(snapshot, is_dm=is_dm),
        TreasuryView(context, is_dm=is_dm, has_coin=any(snapshot["purse"]["balance"].values())),
        execution=execution,
    )


def _coin_snapshot(context: Quartermaster, actor_id: str) -> dict[str, Any]:
    purse = context.currency.purse(actor_id=actor_id)
    character = purse["character"]
    return {
        "purse": purse,
        "recipients": [
            {"id": str(row["id"]), "name": str(row["name"])}
            for row in context.characters.list_characters()
            if row["lifecycle"] == "ACTIVE"
            and (character is None or str(row["id"]) != character["id"])
        ],
    }


async def open_give_coin(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction,
        context.settings,
        lambda: _coin_snapshot(context, _actor_id(interaction)),
        ephemeral=True,
    )
    prepared = execution.value
    if prepared["purse"]["character"] is None:
        await _send_error(
            interaction,
            "You have no active character registered, so you are not carrying any coin.",
        )
        return
    await _send_panel(
        interaction,
        render_give_coin(prepared["purse"], "the treasury"),
        GiveCurrencyView(
            context,
            prepared["purse"],
            prepared["recipients"],
            back=_open(open_treasury, context),
            destination=PARTY_DESTINATION,
        ),
        execution=execution,
    )


class TreasuryGiveView(PanelView):
    """Pick who receives, then say how much: two presses, no character ID."""

    def __init__(self, context: Quartermaster, recipients: list[dict]) -> None:
        super().__init__(context)
        if recipients:
            select = discord.ui.Select(
                placeholder="Give treasury currency to…",
                options=[
                    discord.SelectOption(label=str(row["name"])[:100], value=str(row["id"]))
                    for row in recipients[:SELECT_OPTION_LIMIT]
                ],
                custom_id="qm:treasury:recipient",
                row=0,
            )
            select.callback = self._pick(select, recipients)
            self.add_item(select)
        self.add_navigation(_open(open_treasury, context), label="◀ Treasury", custom_id="qm:treasury:back", row=1)

    def _pick(self, select: discord.ui.Select, recipients: list[dict]) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            if not await _require_dm(interaction, self.settings):
                return
            character_id = select.values[0]
            name = next(
                (str(row["name"]) for row in recipients if str(row["id"]) == character_id),
                "that character",
            )
            await interaction.response.send_modal(TreasuryGiveModal(self.context, character_id, name))

        return callback


async def open_treasury_give(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction,
        context.settings,
        lambda: [row for row in context.characters.list_characters() if row["lifecycle"] == "ACTIVE"],
        ephemeral=True,
    )
    recipients = execution.value
    body = (
        "**GIVE TREASURY CURRENCY**\n\nChoose who receives it." + _select_overflow(len(recipients), noun="characters")
        if recipients
        else "**GIVE TREASURY CURRENCY**\n\nNo active character can receive currency yet."
    )
    await _send_panel(interaction, body, TreasuryGiveView(context, recipients), execution=execution)


# Characters -----------------------------------------------------------------


class CharactersView(PanelView):
    def __init__(self, context: Quartermaster, *, is_dm: bool) -> None:
        super().__init__(context)
        if is_dm:
            self.add_navigation(
                _open(open_register_character, context),
                label="Register…",
                style=discord.ButtonStyle.primary,
                custom_id="qm:characters:add",
            )
            self.add_navigation(
                _open(open_lifecycle, context), label="Lifecycle…", custom_id="qm:characters:lifecycle"
            )
            self.add_navigation(
                _open(open_estate, context), label="Resolve estate…", custom_id="qm:characters:estate"
            )
        self.add_navigation(_open(open_characters, context), label="Refresh", custom_id="qm:characters:refresh")
        self.add_home()


async def open_characters(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    is_dm = await _is_dm(interaction, context.settings)
    execution = await _run_fast(
        interaction, context.settings, context.characters.list_characters, ephemeral=True
    )
    await _send_panel(
        interaction,
        _render_characters(execution.value),
        CharactersView(context, is_dm=is_dm),
        execution=execution,
    )


class RegisterCharacterView(PanelView):
    """Pick the player from Discord itself rather than pasting a user ID."""

    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context)
        select = discord.ui.UserSelect(
            placeholder="Which player is this character for?",
            custom_id="qm:characters:player",
            row=0,
        )
        select.callback = self._pick(select)
        self.add_item(select)
        unassigned = discord.ui.Button(
            label="No Discord player", style=discord.ButtonStyle.secondary, custom_id="qm:characters:npc", row=1
        )
        unassigned.callback = self._unassigned
        self.add_item(unassigned)
        self.add_navigation(
            _open(open_characters, context), label="◀ Characters", custom_id="qm:characters:back", row=1
        )

    def _pick(self, select: discord.ui.UserSelect) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            if not await _require_dm(interaction, self.settings):
                return
            user = select.values[0]
            await interaction.response.send_modal(
                CharacterAddModal(self.context, str(user.id), f"<@{user.id}>")
            )

        return callback

    async def _unassigned(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        await interaction.response.send_modal(CharacterAddModal(self.context, None, "no Discord player"))


async def open_register_character(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    await _send_panel(
        interaction,
        "**REGISTER A CHARACTER**\n\nChoose the player, then name the character. "
        "A player may have only one active character at a time.",
        RegisterCharacterView(context),
    )


class LifecycleView(PanelView):
    """Choose a character and a state, then commit — nothing typed, nothing guessed."""

    def __init__(self, context: Quartermaster, roster: list[dict]) -> None:
        super().__init__(context)
        self.roster = roster
        self.character_id: str | None = None
        self.lifecycle: str | None = None
        if roster:
            characters = discord.ui.Select(
                placeholder="Which character?",
                options=[
                    discord.SelectOption(
                        label=str(row["name"])[:100],
                        value=str(row["id"]),
                        description=f"Currently {row['lifecycle']}",
                    )
                    for row in roster[:SELECT_OPTION_LIMIT]
                ],
                custom_id="qm:lifecycle:character",
                row=0,
            )
            characters.callback = self._pick_character(characters)
            self.add_item(characters)
            states = discord.ui.Select(
                placeholder="Move them to…",
                options=[
                    discord.SelectOption(label=state.title(), value=state, description=description)
                    for state, description in LIFECYCLE_CHOICES
                ],
                custom_id="qm:lifecycle:state",
                row=1,
            )
            states.callback = self._pick_state(states)
            self.add_item(states)
            apply = discord.ui.Button(
                label="Apply", style=discord.ButtonStyle.danger, custom_id="qm:lifecycle:apply", row=2
            )
            apply.callback = self._apply
            self.add_item(apply)
        self.add_navigation(
            _open(open_characters, context), label="◀ Characters", custom_id="qm:lifecycle:back", row=2
        )

    def _name(self) -> str:
        return next(
            (str(row["name"]) for row in self.roster if str(row["id"]) == self.character_id),
            "that character",
        )

    def _render(self) -> str:
        lines = ["**CHARACTER LIFECYCLE**", ""]
        if self.character_id is None:
            lines.append("Choose a character.")
        else:
            lines.append(f"{self._name()} → {self.lifecycle or 'choose a state'}")
        return "\n".join(lines)

    def _pick_character(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            self.character_id = select.values[0]
            for option in select.options:
                option.default = option.value == self.character_id
            await _rerender(interaction, self._render(), self)

        return callback

    def _pick_state(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            self.lifecycle = select.values[0]
            for option in select.options:
                option.default = option.value == self.lifecycle
            await _rerender(interaction, self._render(), self)

        return callback

    async def _apply(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        if self.character_id is None or self.lifecycle is None:
            await _send_error(interaction, "Choose a character and the state to move them to first.")
            return
        # Read once, here. The lambda below runs later, and reading through
        # `self` inside it would let a select changed in between hand the
        # transaction a character this guard never approved.
        character_id = self.character_id
        lifecycle = self.lifecycle
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.characters.transition_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    character_id=character_id,
                    lifecycle=lifecycle,
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"{response['name']} moved from {response['from']} to {response['to']}.",
                ephemeral=True,
            )
        except CharacterError as error:
            await _send_error(interaction, f"That lifecycle change was refused: {error}")


async def open_lifecycle(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction, context.settings, context.characters.list_characters, ephemeral=True
    )
    roster = execution.value
    body = (
        "**CHARACTER LIFECYCLE**\n\nChoose a character." + _select_overflow(len(roster), noun="characters")
        if roster
        else "**CHARACTER LIFECYCLE**\n\nNo characters are registered yet."
    )
    await _send_panel(interaction, body, LifecycleView(context, roster), execution=execution)


class EstateView(PanelView):
    """Where a dead, retired or departed character's belongings go."""

    def __init__(self, context: Quartermaster, roster: list[dict]) -> None:
        super().__init__(context)
        self.sources = [row for row in roster if row["lifecycle"] != "ACTIVE"]
        self.recipients = [row for row in roster if row["lifecycle"] == "ACTIVE"]
        self.character_id: str | None = None
        self.destination: str = PARTY_DESTINATION
        if self.sources:
            sources = discord.ui.Select(
                placeholder="Whose belongings?",
                options=[
                    discord.SelectOption(
                        label=str(row["name"])[:100],
                        value=str(row["id"]),
                        description=f"{row['lifecycle']}",
                    )
                    for row in self.sources[:SELECT_OPTION_LIMIT]
                ],
                custom_id="qm:estate:source",
                row=0,
            )
            sources.callback = self._pick_source(sources)
            self.add_item(sources)
            options = [
                discord.SelectOption(
                    label="The Party Stash",
                    value=PARTY_DESTINATION,
                    description="Everything goes back to the party",
                    default=True,
                )
            ]
            options.extend(
                discord.SelectOption(label=str(row["name"])[:100], value=str(row["id"]))
                for row in self.recipients[: SELECT_OPTION_LIMIT - 1]
            )
            destinations = discord.ui.Select(
                placeholder="Give it all to…", options=options, custom_id="qm:estate:destination", row=1
            )
            destinations.callback = self._pick_destination(destinations)
            self.add_item(destinations)
            apply = discord.ui.Button(
                label="Resolve", style=discord.ButtonStyle.danger, custom_id="qm:estate:apply", row=2
            )
            apply.callback = self._apply
            self.add_item(apply)
        self.add_navigation(
            _open(open_characters, context), label="◀ Characters", custom_id="qm:estate:back", row=2
        )

    def render(self) -> str:
        lines = ["**RESOLVE BELONGINGS**", ""]
        if not self.sources:
            lines.append("Every registered character is active, so there is no estate to resolve.")
            return "\n".join(lines)
        if self.character_id is None:
            lines.append("Choose whose belongings to move. Only non-active characters can be resolved.")
            return "\n".join(lines) + _select_overflow(len(self.sources), noun="non-active characters")
        source = next(
            (str(row["name"]) for row in self.sources if str(row["id"]) == self.character_id), "that character"
        )
        if self.destination == PARTY_DESTINATION:
            target = "the Party Stash"
        else:
            target = next(
                (str(row["name"]) for row in self.recipients if str(row["id"]) == self.destination),
                "the chosen character",
            )
        lines.append(f"Everything {source} was carrying, and their currency, goes to {target}.")
        return "\n".join(lines)

    def _pick_source(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            self.character_id = select.values[0]
            for option in select.options:
                option.default = option.value == self.character_id
            await _rerender(interaction, self.render(), self)

        return callback

    def _pick_destination(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            self.destination = select.values[0]
            for option in select.options:
                option.default = option.value == self.destination
            await _rerender(interaction, self.render(), self)

        return callback

    async def _apply(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        if self.character_id is None:
            await _send_error(interaction, "Choose whose belongings to resolve first.")
            return
        # Read once, for the same reason the lifecycle panel does.
        character_id = self.character_id
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.characters.resolve_belongings_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    character_id=character_id,
                    destination=self.destination,
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Resolved {response['items_moved']} item stacks and currency from "
                f"{response['source_character_name']} to {response['destination_name']}.",
                ephemeral=True,
            )
        except CharacterError as error:
            await _send_error(interaction, f"Those belongings could not be resolved: {error}")


async def open_estate(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction, context.settings, context.characters.list_characters, ephemeral=True
    )
    view = EstateView(context, execution.value)
    await _send_panel(interaction, view.render(), view, execution=execution)


# Combat ---------------------------------------------------------------------

#: The Avrae actions Quartermaster only ever hands off, and the label each reads as.
HANDOFF_ACTIONS = (
    ("join", "Join"),
    ("next", "Next turn"),
    ("attack", "Attack"),
    ("cast", "Cast"),
    ("check", "Check"),
    ("save", "Save"),
)


class CombatView(PanelView):
    def __init__(self, context: Quartermaster, *, is_dm: bool) -> None:
        super().__init__(context)
        # The rows are left to discord.py: a DM sees two more controls than a
        # player does, and pinning rows here is how one of them stops fitting.
        if is_dm:
            start = discord.ui.Button(
                label="Start combat", style=discord.ButtonStyle.danger, custom_id="qm:combat:start"
            )
            start.callback = self._start
            self.add_item(start)
            end = discord.ui.Button(
                label="End combat…", style=discord.ButtonStyle.danger, custom_id="qm:combat:end"
            )
            end.callback = self._end
            self.add_item(end)
        for action, label in HANDOFF_ACTIONS:
            button = discord.ui.Button(
                label=label, style=discord.ButtonStyle.secondary, custom_id=f"qm:combat:{action}"
            )
            button.callback = self._handoff(action)
            self.add_item(button)
        self.add_navigation(_open(open_combat, context), label="Refresh", custom_id="qm:combat:refresh")
        self.add_home()

    def _handoff(self, action: str) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            try:
                execution = await _run_fast(
                    interaction,
                    self.settings,
                    lambda: self.context.handoff.build(action, channel_id=str(interaction.channel_id)),
                    ephemeral=True,
                )
                await _send_execution(interaction, execution, execution.value.render(), ephemeral=True)
            except AvraeHandoffError as error:
                await _send_error(interaction, f"Avrae handoff could not be prepared: {error}")

        return callback

    async def _start(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.combat.open_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    channel_id=str(interaction.channel_id),
                ),
                ephemeral=True,
            )
            await _send_execution(
                interaction,
                execution,
                _render_combat_opened(execution.value.logical_response),
                ephemeral=True,
            )
        except CombatError as error:
            await _send_error(interaction, f"Combat could not be opened: {error}")

    async def _end(self, interaction: discord.Interaction) -> None:
        if await _require_dm(interaction, self.settings):
            await interaction.response.send_modal(CombatEndModal(self.context))


class CombatEndModal(QuartermasterModal, title="End combat"):
    """Close the record, and hand the DM the spoils controls in the same breath.

    This is the one modal that lives with the panels rather than with the other
    controls: what it produces is a panel, and the closeout controls are the
    whole reason ending a fight is not the end of the interaction.
    """

    outcome = discord.ui.TextInput(
        label="How did it resolve?", placeholder="The ogre fled", required=False, max_length=200
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        outcome = str(self.outcome.value).strip() or None
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.combat.close_interaction(
                    str(interaction.id), actor_id=_actor_id(interaction), outcome=outcome
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                _render_combat_closed(response),
                ephemeral=True,
                # The closeout controls only mean anything once a combat has
                # actually closed and there are spoils to record.
                view=CombatCloseoutView(self.context) if response["status"] == "CLOSED" else None,
            )
        except CombatError as error:
            await _send_error(interaction, f"Combat could not be closed: {error}")


async def open_combat(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_guild(interaction, context.settings):
        return
    is_dm = await _is_dm(interaction, context.settings)
    try:
        execution = await _run_fast(interaction, context.settings, context.combat.status, ephemeral=True)
    except CombatError as error:
        await _send_error(interaction, f"Combat status could not be read: {error}")
        return
    await _send_panel(
        interaction,
        _render_combat_status(execution.value),
        CombatView(context, is_dm=is_dm),
        execution=execution,
    )


class CombatCloseoutView(PanelView):
    """The end-of-combat controls: spoils into the stash, or a claimable drop.

    Combat ending is the moment loot exists, and it was also the moment the
    handoff used to stop — the DM read `!i end` and was left to remember where
    to record what they just won.
    """

    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context)
        grant = discord.ui.Button(
            label="Record spoils", style=discord.ButtonStyle.primary, custom_id="qm:closeout:grant"
        )
        grant.callback = self._grant
        self.add_item(grant)
        self.add_navigation(_open(open_loot, context), label="Open Loot", custom_id="qm:closeout:loot")
        self.add_home()

    async def _grant(self, interaction: discord.Interaction) -> None:
        if await _require_dm(interaction, self.settings):
            await interaction.response.send_modal(GrantLootModal(self.context))


# Sessions -------------------------------------------------------------------


class SessionView(PanelView):
    def __init__(self, context: Quartermaster, *, active: int | None) -> None:
        super().__init__(context)
        if active is None:
            start = discord.ui.Button(
                label="Start session", style=discord.ButtonStyle.primary, custom_id="qm:session:start"
            )
            start.callback = self._start
            self.add_item(start)
        else:
            end = discord.ui.Button(
                label="End session…", style=discord.ButtonStyle.danger, custom_id="qm:session:end"
            )
            end.callback = self._end
            self.add_item(end)
        self.add_navigation(_open(open_session, context), label="Refresh", custom_id="qm:session:refresh")
        self.add_navigation(_open(open_dm_tools, context), label="◀ DM Tools", custom_id="qm:session:back")

    async def _start(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.sessions.start_interaction(
                    str(interaction.id), actor_id=_actor_id(interaction)
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            if response["status"] == "ACTIVE_EXISTS":
                message = (
                    f"Session {response['active_session_number']} is still active. "
                    "End it explicitly before starting another."
                )
            else:
                message = f"Session {response['session_number']} started."
            await _send_execution(interaction, execution, message, ephemeral=True)
        except SessionError as error:
            await _send_error(interaction, f"The session could not be started: {error}")

    async def _end(self, interaction: discord.Interaction) -> None:
        if await _require_dm(interaction, self.settings):
            await interaction.response.send_modal(SessionEndModal(self.context))


def _active_session_number(context: Quartermaster) -> int | None:
    with context.store.read() as connection:
        row = connection.execute(
            "SELECT session_number FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
    return int(row["session_number"]) if row else None


async def open_session(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    execution = await _run_fast(
        interaction, context.settings, lambda: _active_session_number(context), ephemeral=True
    )
    active = execution.value
    body = (
        f"**SESSION**\n\nSession {active} is in progress. Ending it closes any open combat and "
        "any Loot Drop still waiting."
        if active is not None
        else "**SESSION**\n\nNo session is in progress."
    )
    await _send_panel(interaction, body, SessionView(context, active=active), execution=execution)


# DM tools -------------------------------------------------------------------


class DMToolsView(PanelView):
    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context)
        grant = discord.ui.Button(
            label="Grant loot…", style=discord.ButtonStyle.primary, custom_id="qm:dm:grant", row=0
        )
        grant.callback = self._grant
        self.add_item(grant)
        self.add_navigation(_open(open_loot_admin, context), label="Loot Drops", custom_id="qm:dm:loot", row=0)
        self.add_navigation(_open(open_session, context), label="Session", custom_id="qm:dm:session", row=0)
        self.add_navigation(
            _open(open_stash_correction, context), label="Correct stash…", custom_id="qm:dm:correct", row=1
        )
        self.add_navigation(
            _open(open_maintenance, context), label="Maintenance", custom_id="qm:dm:maintenance", row=1
        )
        self.add_home(row=1)

    async def _grant(self, interaction: discord.Interaction) -> None:
        if await _require_dm(interaction, self.settings):
            await interaction.response.send_modal(GrantLootModal(self.context))


async def open_dm_tools(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    await _send_panel(
        interaction,
        "**DM TOOLS**\n\nGrant loot straight into the Party Stash, open a claimable drop, "
        "run the session, or take a backup.",
        DMToolsView(context),
    )


class StashCorrectionView(PanelView):
    """Where the DM takes something out of the Party Stash for good.

    It lives in DM Tools rather than on the Party Stash panel every player
    opens, for the same reason granting does: the stash panel is what the
    table looks at, and a destructive control on it is a control somebody
    presses by accident.
    """

    def __init__(self, context: Quartermaster, items: list[dict]) -> None:
        super().__init__(context)
        if items:
            select = discord.ui.Select(
                placeholder="Remove some of…",
                options=[
                    discord.SelectOption(
                        label=str(item["item_name"])[:100],
                        value=str(item["id"]),
                        description=f"The party holds {item['quantity']}",
                    )
                    for item in items[:SELECT_OPTION_LIMIT]
                ],
                custom_id="qm:correct:pick",
                row=0,
            )
            select.callback = self._pick(select, items)
            self.add_item(select)
        self.add_navigation(
            _open(open_stash_correction, context), label="Refresh", custom_id="qm:correct:refresh", row=1
        )
        self.add_navigation(
            _open(open_dm_tools, context), label="◀ DM Tools", custom_id="qm:correct:back", row=1
        )

    def _pick(self, select: discord.ui.Select, items: list[dict]) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            if not await _require_dm(interaction, self.settings):
                return
            stack_id = select.values[0]
            item = next((row for row in items if str(row["id"]) == stack_id), None)
            if item is None:
                await _send_error(interaction, "That stack is no longer in the Party Stash.")
                return
            await interaction.response.send_modal(StashRemoveModal(self.context, item))

        return callback


async def open_stash_correction(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    try:
        execution = await _run_fast(interaction, context.settings, context.inventory.browse, ephemeral=True)
    except InventoryError as error:
        await _send_error(interaction, f"The Party Stash could not be read: {error}")
        return
    items = execution.value
    lines = ["**CORRECT THE PARTY STASH**", ""]
    if items:
        lines.extend(
            [
                "Removing takes items out of the campaign — a mistyped grant, or something the "
                "party used up.",
                "It hands nothing back to anyone: to move an item, grant it or let a player give it.",
                "",
            ]
        )
        # The select shows the first twenty-five stacks, so the listing shows
        # the same ones: a panel that names a stack with no option to choose it
        # reads as an item that cannot be corrected at all.
        lines.extend(
            f"• {item['item_name']} x{item['quantity']}" for item in items[:SELECT_OPTION_LIMIT]
        )
        overflow = _select_overflow(len(items), noun="stacks")
        if overflow:
            lines.extend(["", overflow.strip()])
    else:
        lines.append("The Party Stash is empty, so there is nothing to correct.")
    await _send_panel(
        interaction,
        fit_discord_lines(lines, label="Party Stash"),
        StashCorrectionView(context, items),
        execution=execution,
    )


class LootAdminView(PanelView):
    def __init__(self, context: Quartermaster, drops: list[dict]) -> None:
        super().__init__(context)
        new = discord.ui.Button(
            label="New drop…", style=discord.ButtonStyle.primary, custom_id="qm:lootadmin:new", row=0
        )
        new.callback = self._new
        self.add_item(new)
        if drops:
            select = discord.ui.Select(
                placeholder="Close a drop…",
                options=[
                    discord.SelectOption(
                        label=f"Drop {str(drop['drop_id'])[:8]}",
                        value=str(drop["drop_id"]),
                        description=_drop_summary(drop),
                    )
                    for drop in drops[:SELECT_OPTION_LIMIT]
                ],
                custom_id="qm:lootadmin:close",
                row=1,
            )
            select.callback = self._close(select)
            self.add_item(select)
        self.add_navigation(_open(open_loot_admin, context), label="Refresh", custom_id="qm:lootadmin:refresh", row=2)
        self.add_navigation(_open(open_dm_tools, context), label="◀ DM Tools", custom_id="qm:lootadmin:back", row=2)

    async def _new(self, interaction: discord.Interaction) -> None:
        if await _require_dm(interaction, self.settings):
            await interaction.response.send_modal(LootDropModal(self.context))

    def _close(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            if not await _require_dm(interaction, self.settings):
                return
            drop_id = select.values[0]
            try:
                execution = await _run_fast(
                    interaction,
                    self.settings,
                    lambda: self.context.loot.close_drop_interaction(
                        str(interaction.id), drop_id=drop_id, actor_id=_actor_id(interaction)
                    ),
                    ephemeral=True,
                )
                await _send_execution(
                    interaction,
                    execution,
                    f"Loot Drop `{drop_id[:8]}` closed. Anything unclaimed went back to the Party Stash.",
                    ephemeral=True,
                )
            except LootDropError as error:
                await _send_error(interaction, f"That Loot Drop could not be closed: {error}")

        return callback


def _drop_summary(drop: dict) -> str:
    remaining = sum(int(item["remaining_quantity"]) for item in drop["items"])
    entries = len(drop["items"])
    return f"{remaining} unclaimed across {entries} {'entry' if entries == 1 else 'entries'}"[:100]


async def open_loot_admin(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    try:
        execution = await _run_fast(interaction, context.settings, context.loot.list_open, ephemeral=True)
    except LootDropError as error:
        await _send_error(interaction, f"Loot Drops could not be read: {error}")
        return
    await _send_panel(
        interaction,
        _render_loot(execution.value),
        LootAdminView(context, execution.value),
        execution=execution,
    )


class MaintenanceView(PanelView):
    def __init__(self, context: Quartermaster) -> None:
        super().__init__(context)
        export = discord.ui.Button(
            label="Export", style=discord.ButtonStyle.primary, custom_id="qm:maintenance:export"
        )
        export.callback = self._export
        self.add_item(export)
        backup = discord.ui.Button(
            label="Backup", style=discord.ButtonStyle.primary, custom_id="qm:maintenance:backup"
        )
        backup.callback = self._backup
        self.add_item(backup)
        health = discord.ui.Button(
            label="Health", style=discord.ButtonStyle.secondary, custom_id="qm:maintenance:health"
        )
        health.callback = self._health
        self.add_item(health)
        self.add_navigation(_open(open_dm_tools, context), label="◀ DM Tools", custom_id="qm:maintenance:back")

    async def _export(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            execution = await _run_deferred(
                interaction,
                self.context.services,
                lambda: {"export": render_export(self.context.store)},
                settings=self.settings,
                response_kind="export",
                ephemeral=True,
            )
            await _send_deferred_export(interaction, execution)
        except DeferredExecutionError as error:
            await _send_error(interaction, str(error))

    async def _backup(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            execution = await _run_deferred(
                interaction,
                self.context.services,
                lambda: create_scheduled_backup(
                    self.context.store,
                    self.settings.backup_directory,
                    off_device_directory=self.settings.backup_off_device_directory,
                    retention_count=self.settings.backup_retention_count,
                ),
                settings=self.settings,
                response_kind="backup",
                ephemeral=True,
            )
            await _send_deferred_backup(interaction, execution)
        except DeferredExecutionError as error:
            await _send_error(interaction, str(error))

    async def _health(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        execution = await _run_fast(
            interaction,
            self.settings,
            lambda: render_health(health_report(self.context.store)),
            ephemeral=True,
        )
        await _send_execution(interaction, execution, execution.value, ephemeral=True)


async def open_maintenance(interaction: discord.Interaction, context: Quartermaster) -> None:
    if not await _require_dm(interaction, context.settings):
        return
    await _send_panel(
        interaction,
        "**MAINTENANCE**\n\nExport is the full record every truncated surface points at. "
        "Backup writes a validated snapshot. Health reports what the runtime can see.",
        MaintenanceView(context),
    )
