"""Leaf controls: the components that act, and the modals they open.

The panels in `discord_panels` navigate; everything here changes canonical
state or collects what a change needs. Keeping the two apart is what lets a
panel hand a control the way back to itself without the control having to know
what a panel is.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from .characters import CharacterError
from .currency import CurrencyError, CurrencySemanticStaleness, format_currency
from .discord_common import (
    Quartermaster,
    _actor_id,
    _bind_view,
    _message_id,
    _require_dm,
    _rerender,
    _run_fast,
    _send_error,
    _send_execution,
)
from .handles import HandleError
from .inventory import InventoryError, SemanticStaleness
from .loot import LootDropError
from .rendering import DISCORD_VIEW_COMPONENT_LIMIT
from .sessions import RECORDING_URL_LIMIT, SessionError

logger = logging.getLogger(__name__)

MAX_VIEW_BUTTONS = DISCORD_VIEW_COMPONENT_LIMIT

#: Handles live for five minutes, so a view that carries them should not outlive
#: them: a button that has quietly expired is worse than one that is gone.
CONTROL_TIMEOUT = 300

Opener = Callable[[discord.Interaction], Awaitable[None]]

#: How a view reaches the message it was sent on after the fact. The two forms
#: Discord offers — an interaction's original response and a followup webhook
#: message — both edit an ephemeral message, and both take `content` and `view`.
Editor = Callable[..., Awaitable[Any]]

PARTY_DESTINATION = "party"

#: The message a view has expired on, and who is showing on it now. Navigation
#: replaces a panel in place, so several views can share one message over an
#: evening and only the last of them is what the player is looking at.
_ON_SCREEN: weakref.WeakValueDictionary[int, QuartermasterView] = weakref.WeakValueDictionary()

EXPIRED_NOTICE = "This view has expired."
EXPIRED_NOTICE_WITHOUT_CONTROL = "This view has expired. Run `/quartermaster` to open it again."


class QuartermasterView(discord.ui.View):
    """A view whose controls answer even when something unforeseen breaks.

    Slash commands route an unexpected exception to `bot.tree.error`, which
    replies and logs. Component callbacks have no equivalent: each one catches
    the domain errors it names, and anything else — a `sqlite3.OperationalError`
    from a contended write, a bug in a renderer — reaches discord.py's default
    `View.on_error`, which logs and leaves the player looking at Discord's bare
    "This interaction failed" with no idea whether their take committed. Every
    view inherits this so the answer is the same wherever it is pressed.

    A timeout is the same failure by a different route, and a more likely one:
    once a view stops listening, discord.py never sees the press at all, so the
    player reads the same bare failure with no way to tell it from a mutation
    that was refused. `on_timeout` retires the controls and says so instead.
    """

    def __init__(
        self,
        context: Quartermaster,
        *,
        timeout: float | None = CONTROL_TIMEOUT,
        reopen: Opener | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.context = context
        self.settings = context.settings
        self.reopen = reopen
        self._editor: Editor | None = None
        self._screen: int | None = None

    def bind(self, editor: Editor, *, screen: int | None = None) -> None:
        """Remember how to reach the message this view was sent on.

        Ephemeral messages can only be edited through the interaction that
        produced them, so a view that wants to retire its own controls has to
        be handed that route at the moment it is sent. `screen` is the message
        it landed on when there is one; a view sent as a fresh message learns
        its message the first time something on it is pressed.
        """
        self._editor = editor
        if screen is not None:
            self._claim(screen)

    def _claim(self, screen: int) -> None:
        self._screen = screen
        _ON_SCREEN[screen] = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Record which message this view is on, and that it is still on it."""
        message = getattr(interaction, "message", None)
        if message is not None:
            self._claim(int(message.id))
        return True

    async def on_timeout(self) -> None:
        """Take the dead controls off the screen rather than leave them to fail.

        A view that has timed out is no longer listening: discord.py never
        dispatches the press, nothing acknowledges the interaction, and Discord
        tells the player "This interaction failed" — the one sentence that must
        never be ambiguous, because it is also what a crash looks like. The
        controls are replaced with the reason they are gone and, where the view
        knows the way back, one control that renders it again.
        """
        if self._editor is None:
            return
        if self._screen is not None and _ON_SCREEN.get(self._screen) is not self:
            # Another panel replaced this one on the same message. Editing now
            # would wipe whatever the player is actually looking at, which is a
            # worse failure than the one this is here to prevent.
            return
        expired = ExpiredView(self.context, self.reopen) if self.reopen is not None else None
        notice = EXPIRED_NOTICE if expired is not None else EXPIRED_NOTICE_WITHOUT_CONTROL
        try:
            await self._editor(content=notice, view=expired)
        except discord.HTTPException as error:
            # A rendered view is bound to the interaction that rendered it and
            # times out well inside that token's life, but pressing a control
            # restarts the clock without re-rendering — so a long enough run of
            # presses can outlive the token. That is the dead end this exists to
            # prevent, reached anyway; there is nobody left to tell but the log.
            logger.info("an expired view could not be retired: %s", error)

    def add_navigation(
        self,
        opener: Opener,
        *,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        custom_id: str,
        row: int | None = None,
    ) -> discord.ui.Button:
        """Add a button that moves the caller to another panel."""
        button = discord.ui.Button(label=label, style=style, custom_id=custom_id, row=row)

        async def callback(interaction: discord.Interaction) -> None:
            await opener(interaction)

        button.callback = callback
        self.add_item(button)
        return button

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception(
            "Quartermaster component %s failed for interaction %s",
            getattr(item, "custom_id", type(item).__name__),
            interaction.id,
            exc_info=error,
        )
        await _send_error(
            interaction,
            "Quartermaster could not complete that action. Nothing was changed unless you "
            "were told otherwise; open the surface again to see current state.",
        )


class ExpiredView(QuartermasterView):
    """What is left on the screen once a view has expired: the way back.

    This one never expires. Everything it can do is render a panel out of
    current state, so there is nothing on it to go stale — and a view whose
    whole purpose is to answer an expiry cannot be the next thing that quietly
    stops answering.
    """

    def __init__(self, context: Quartermaster, reopen: Opener) -> None:
        super().__init__(context, timeout=None)
        self.add_navigation(reopen, label="Open again", custom_id="qm:expired:open")


class QuartermasterModal(discord.ui.Modal):
    """A modal with the same promise as a button: it always answers."""

    def __init__(self, context: Quartermaster) -> None:
        super().__init__()
        self.context = context
        self.settings = context.settings

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception(
            "Quartermaster modal %s failed for interaction %s",
            type(self).__name__,
            interaction.id,
            exc_info=error,
        )
        await _send_error(
            interaction,
            "Quartermaster could not record that. Nothing was changed unless you were told "
            "otherwise; open the panel again to see current state.",
        )


def _positive_quantity(raw: object) -> int:
    """Read a quantity a person typed, refusing everything that is not one."""
    try:
        quantity = int(str(raw).strip())
    except ValueError as error:
        raise ValueError("Quantity must be a positive whole number.") from error
    if quantity <= 0:
        raise ValueError("Quantity must be a positive whole number.")
    return quantity


def _coin_amounts(fields: dict[str, object]) -> dict[str, int]:
    """Read a four-coin modal, treating an empty box as nothing rather than junk."""
    amounts: dict[str, int] = {}
    for coin, raw in fields.items():
        text = str(raw or "").strip()
        if not text:
            amounts[coin] = 0
            continue
        try:
            amounts[coin] = int(text)
        except ValueError as error:
            raise ValueError(f"{coin} must be a whole number of coins.") from error
    return amounts


# Party Stash ----------------------------------------------------------------


class TakeView(QuartermasterView):
    """The per-stack take controls, with the way back to a live list.

    Every control here is a single-use handle, so the view is spent the moment
    it is used. Refresh is not a convenience: without it the player is left
    pressing buttons that have already been consumed.
    """

    def __init__(
        self,
        context: Quartermaster,
        items: list[dict],
        handles: dict[str, str],
        take_all_handles: dict[str, str] | None = None,
        *,
        refresh: Opener,
        back: Opener,
    ) -> None:
        # Reopening mints fresh handles rather than restoring spent ones, which
        # is the only honest way back onto a view whose controls were single-use.
        super().__init__(context, reopen=refresh)
        take_all_handles = take_all_handles or {}
        for item in items:
            if len(self.children) >= MAX_VIEW_BUTTONS - 2:
                break
            handle_id = handles.get(item["id"])
            if handle_id is not None:
                button = discord.ui.Button(
                    label=f"Take 1 {item['item_name'][:60]}",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"qm:h:{handle_id}",
                )
                button.callback = self._callback_for(handle_id)
                self.add_item(button)
            take_all_id = take_all_handles.get(item["id"])
            if take_all_id is not None and len(self.children) < MAX_VIEW_BUTTONS - 2:
                button = discord.ui.Button(
                    label=f"Take all {item['item_name'][:58]}",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"qm:hall:{take_all_id}",
                )
                button.callback = self._callback_for(take_all_id)
                self.add_item(button)
        self.add_navigation(refresh, label="Refresh", custom_id="qm:take:refresh")
        self.add_navigation(back, label="◀ Party Stash", custom_id="qm:take:back")

    def _callback_for(self, handle_id: str) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            try:
                execution = await _run_fast(
                    interaction,
                    self.settings,
                    lambda: self.context.inventory.take_interaction(
                        str(interaction.id), handle_id=handle_id, actor_id=_actor_id(interaction)
                    ),
                    ephemeral=True,
                )
                response = execution.value.logical_response
                await _send_execution(
                    interaction,
                    execution,
                    f"You took {response['quantity']} {response['item_name']}. {response['remaining']} remain.",
                    ephemeral=True,
                )
            except SemanticStaleness:
                await _send_staleness_prompt(
                    interaction,
                    "The quantity changed. Confirm taking the current quantity if that is still what you intend.",
                    TakeConfirmationView(self.context, handle_id),
                )
            except (HandleError, InventoryError) as error:
                await _send_error(interaction, f"That action could not be completed: {error}")

        return callback


async def _send_staleness_prompt(
    interaction: discord.Interaction,
    message: str,
    view: discord.ui.View,
) -> None:
    """Ask for the confirmation, whichever half of the response is still free."""
    if interaction.response.is_done():
        sent = await interaction.followup.send(message, ephemeral=True, view=view, wait=True)
        _bind_view(view, sent.edit, screen=_message_id(sent))
    else:
        await interaction.response.send_message(message, ephemeral=True, view=view)
        _bind_view(view, interaction.edit_original_response)


class TakeConfirmationView(QuartermasterView):
    def __init__(self, context: Quartermaster, handle_id: str) -> None:
        super().__init__(context)
        self.handle_id = handle_id

    @discord.ui.button(label="Confirm current quantity", style=discord.ButtonStyle.danger, custom_id="qm:confirm-take")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.inventory.confirm_take_interaction(
                    str(interaction.id), handle_id=self.handle_id, actor_id=_actor_id(interaction)
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"You took {response['quantity']} {response['item_name']}. {response['remaining']} remain.",
                ephemeral=True,
            )
        except (HandleError, InventoryError) as error:
            await _send_error(interaction, f"That confirmation could not be completed: {error}")


# Loot Drops -----------------------------------------------------------------


class LootClaimView(QuartermasterView):
    def __init__(
        self,
        context: Quartermaster,
        drops: list[dict],
        handles: dict[str, str],
        *,
        refresh: Opener,
        back: Opener,
    ) -> None:
        super().__init__(context, reopen=refresh)
        for drop in drops:
            for item in drop["items"]:
                if len(self.children) >= MAX_VIEW_BUTTONS - 2:
                    break
                handle_id = handles.get(item["id"])
                if handle_id is None:
                    continue
                button = discord.ui.Button(
                    label=f"Take {item['item_name'][:65]}",
                    style=discord.ButtonStyle.success,
                    custom_id=f"qm:loot:{handle_id}",
                )
                button.callback = self._callback_for(handle_id)
                self.add_item(button)
        self.add_navigation(refresh, label="Refresh", custom_id="qm:loot:refresh")
        self.add_navigation(back, label="◀ Home", custom_id="qm:loot:back")

    def _callback_for(self, handle_id: str) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            try:
                execution = await _run_fast(
                    interaction,
                    self.settings,
                    lambda: self.context.loot.claim_interaction(
                        str(interaction.id), handle_id=handle_id, actor_id=_actor_id(interaction)
                    ),
                    ephemeral=True,
                )
                response = execution.value.logical_response
                if response["status"] == "CLAIMED":
                    await _send_execution(
                        interaction,
                        execution,
                        f"You claimed {response['quantity']} {response['item_name']}. {response['remaining']} remain.",
                        ephemeral=True,
                    )
                else:
                    await _send_execution(
                        interaction,
                        execution,
                        "That Loot Drop is no longer active. Open Party Stash to see the remaining items.",
                        ephemeral=True,
                    )
            except (LootDropError, HandleError) as error:
                await _send_error(interaction, f"That Loot Drop action could not be completed: {error}")

        return callback


# Giving ---------------------------------------------------------------------


def _destination_options(
    recipients: list[dict],
    *,
    selected: str,
    party_label: str = "The Party Stash",
    party_description: str = "Put it back where the party can take it",
    recipient_description: str = "Hand it to this character",
) -> list[discord.SelectOption]:
    options = [
        discord.SelectOption(
            label=party_label,
            value=PARTY_DESTINATION,
            description=party_description,
            default=selected == PARTY_DESTINATION,
        )
    ]
    for recipient in recipients[: MAX_VIEW_BUTTONS - 1]:
        options.append(
            discord.SelectOption(
                label=str(recipient["name"])[:100],
                value=str(recipient["id"]),
                description=recipient_description,
                default=selected == str(recipient["id"]),
            )
        )
    return options


class GiveItemView(QuartermasterView):
    """Give a held stack away: how much, and to whom, without typing either.

    The quantity controls carry handles minted against the quantity on screen,
    so "Give all" cannot quietly mean a number the giver never saw. The
    destination is view state rather than a handle: it is chosen here and now,
    and there is nothing about it that can go stale underneath the giver.
    """

    def __init__(
        self,
        context: Quartermaster,
        item: dict,
        handles: dict[str, str],
        recipients: list[dict],
        *,
        back: Opener,
        destination: str = PARTY_DESTINATION,
    ) -> None:
        super().__init__(context, reopen=back)
        self.item = item
        self.handles = handles
        self.recipients = recipients
        self.destination = destination
        select = discord.ui.Select(
            placeholder="Give to…",
            options=_destination_options(recipients, selected=destination),
            custom_id="qm:give:destination",
            row=0,
        )
        select.callback = self._choose_destination(select)
        self.add_item(select)
        one = handles.get("one")
        if one is not None:
            button = discord.ui.Button(label="Give 1", style=discord.ButtonStyle.secondary, custom_id="qm:give:one", row=1)
            button.callback = self._give_with(one)
            self.add_item(button)
        every = handles.get("all")
        if every is not None:
            button = discord.ui.Button(label="Give all", style=discord.ButtonStyle.primary, custom_id="qm:give:all", row=1)
            button.callback = self._give_with(every)
            self.add_item(button)
        amount = discord.ui.Button(label="Give some…", style=discord.ButtonStyle.secondary, custom_id="qm:give:some", row=1)
        amount.callback = self._give_some
        self.add_item(amount)
        # Using something up is not a give, so it sits on its own row rather
        # than beside three controls the destination select governs: nothing
        # about "to the Party Stash" applies to a potion that has been drunk.
        use = discord.ui.Button(label="Use…", style=discord.ButtonStyle.danger, custom_id="qm:give:use", row=2)
        use.callback = self._use
        self.add_item(use)
        self.add_navigation(back, label="◀ My Items", custom_id="qm:give:back", row=2)

    def _destination_name(self) -> str:
        if self.destination == PARTY_DESTINATION:
            return "the Party Stash"
        for recipient in self.recipients:
            if str(recipient["id"]) == self.destination:
                return str(recipient["name"])
        return "the chosen character"

    def _choose_destination(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            self.destination = select.values[0]
            for option in select.options:
                option.default = option.value == self.destination
            await _rerender(interaction, render_give_item(self.item, self._destination_name()), self)

        return callback

    def _give_with(self, handle_id: str) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            destination = self.destination
            try:
                execution = await _run_fast(
                    interaction,
                    self.settings,
                    lambda: self.context.inventory.give_with_handle_interaction(
                        str(interaction.id),
                        handle_id=handle_id,
                        actor_id=_actor_id(interaction),
                        destination=destination,
                    ),
                    ephemeral=True,
                )
                await _send_execution(
                    interaction, execution, _render_given(execution.value.logical_response), ephemeral=True
                )
            except SemanticStaleness:
                await _send_staleness_prompt(
                    interaction,
                    "You are holding a different number of those now. Confirm giving everything you "
                    "currently hold if that is still what you intend.",
                    GiveConfirmationView(self.context, handle_id, destination),
                )
            except (HandleError, InventoryError) as error:
                await _send_error(interaction, f"That item could not be given: {error}")

        return callback

    async def _give_some(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            GiveQuantityModal(self.context, self.item, self.destination, self._destination_name())
        )

    async def _use(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(UseItemModal(self.context, self.item))


class GiveConfirmationView(QuartermasterView):
    def __init__(self, context: Quartermaster, handle_id: str, destination: str) -> None:
        super().__init__(context)
        self.handle_id = handle_id
        self.destination = destination

    @discord.ui.button(label="Confirm current quantity", style=discord.ButtonStyle.danger, custom_id="qm:confirm-give")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.inventory.confirm_give_with_handle_interaction(
                    str(interaction.id),
                    handle_id=self.handle_id,
                    actor_id=_actor_id(interaction),
                    destination=self.destination,
                ),
                ephemeral=True,
            )
            await _send_execution(
                interaction, execution, _render_given(execution.value.logical_response), ephemeral=True
            )
        except (HandleError, InventoryError) as error:
            await _send_error(interaction, f"That confirmation could not be completed: {error}")


class GiveQuantityModal(QuartermasterModal, title="Give some of a stack"):
    quantity = discord.ui.TextInput(label="How many?", placeholder="1", max_length=7)

    def __init__(
        self,
        context: Quartermaster,
        item: dict,
        destination: str,
        destination_name: str,
    ) -> None:
        super().__init__(context)
        self.item = item
        self.destination = destination
        self.destination_name = destination_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            quantity = _positive_quantity(self.quantity.value)
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.inventory.give_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    item_name=str(self.item["item_name"]),
                    quantity=quantity,
                    destination=self.destination,
                ),
                ephemeral=True,
            )
            await _send_execution(
                interaction, execution, _render_given(execution.value.logical_response), ephemeral=True
            )
        except InventoryError as error:
            await _send_error(interaction, f"That item could not be given: {error}")


class UseItemModal(QuartermasterModal, title="Use something up"):
    """Spend a quantity of what the caller is carrying, out of the campaign.

    The quantity is typed rather than pressed. Every other quantity control on
    this panel is a handle minted against a render, because a give can be
    undone by giving it back and a stale one is worth a confirmation prompt;
    this one cannot be undone by anything, so it asks the person removing the
    items to say the number themselves.
    """

    quantity = discord.ui.TextInput(label="How many?", placeholder="1", max_length=7)
    reason = discord.ui.TextInput(
        label="What happened to them?", placeholder="Drunk in the tomb", required=False, max_length=200
    )

    def __init__(self, context: Quartermaster, item: dict) -> None:
        super().__init__(context)
        self.item = item
        self.title = f"Use {item['item_name']}"[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            quantity = _positive_quantity(self.quantity.value)
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.inventory.consume_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    stack_id=str(self.item["id"]),
                    quantity=quantity,
                    reason=str(self.reason.value),
                ),
                ephemeral=True,
            )
            await _send_execution(
                interaction, execution, _render_consumed(execution.value.logical_response), ephemeral=True
            )
        except InventoryError as error:
            await _send_error(interaction, f"That could not be used up: {error}")


class StashRemoveModal(QuartermasterModal, title="Remove from the Party Stash"):
    """The DM's correction: take a quantity out of the shared stash for good.

    `party_authorized` is only ever passed from here, after the same check
    every other DM control makes when it is pressed. The domain refuses a
    party-owned stack without it, so a panel left open across a role change
    cannot spend a stale render on the one operation with no way back.
    """

    quantity = discord.ui.TextInput(label="How many?", placeholder="1", max_length=7)
    reason = discord.ui.TextInput(
        label="Why?", placeholder="Miscounted the grant", required=False, max_length=200
    )

    def __init__(self, context: Quartermaster, item: dict) -> None:
        super().__init__(context)
        self.item = item
        self.title = f"Remove {item['item_name']}"[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            quantity = _positive_quantity(self.quantity.value)
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.inventory.consume_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    stack_id=str(self.item["id"]),
                    quantity=quantity,
                    reason=str(self.reason.value),
                    party_authorized=True,
                ),
                ephemeral=True,
            )
            await _send_execution(
                interaction, execution, _render_consumed(execution.value.logical_response), ephemeral=True
            )
        except InventoryError as error:
            await _send_error(interaction, f"That could not be removed: {error}")


def _render_consumed(response: dict) -> str:
    if response["owner_type"] == "PARTY":
        return (
            f"Removed {response['quantity']} {response['item_name']} from the Party Stash. "
            f"{response['remaining']} remain. This did not return anything to anyone."
        )
    return (
        f"You used {response['quantity']} {response['item_name']}. "
        f"{response['remaining']} still held."
    )


def render_give_item(item: dict, destination_name: str) -> str:
    """The panel for one held stack: hand it on, or use it up.

    The heading is the item rather than the verb, because there are now two
    verbs. Naming what the give controls will do and what **Use…** will do on
    the same screen is the difference between a player finding the way to
    drink their own potion and a player concluding the only thing they can do
    with it is give it away.
    """
    return "\n".join(
        [
            f"**{str(item['item_name']).upper()}**",
            "",
            f"You hold {item['quantity']}.",
            f"Give → {destination_name}.",
            "Use → gone from the campaign, on the record.",
        ]
    )


def _render_given(response: dict) -> str:
    return (
        f"{response['character_name']} gave {response['quantity']} {response['item_name']}"
        f" to {response['destination_name']}. {response['remaining']} still held."
    )


def render_give_coin(purse: dict, destination_name: str) -> str:
    """The give-coin panel: what the player has, and where it is headed."""
    character = purse["character"]
    return "\n".join(
        [
            "**GIVE COIN**",
            "",
            f"{character['name']} is carrying {format_currency(purse['balance'])}.",
            f"Going to {destination_name}.",
        ]
    )


def _render_coin_given(response: dict) -> str:
    return (
        f"{response['character_name']} gave {format_currency(response['amount'])}"
        f" to {response['destination_name']}."
        f" Still carrying {format_currency(response['character_after'])}."
    )


class GiveCurrencyView(QuartermasterView):
    """Give coin away: how much, and to whom.

    The same shape as `GiveItemView`, without the handles. A held stack has a
    quantity on screen that another character can move underneath the giver, so
    "Give all" has to be minted against what was rendered; coin is typed into a
    modal at the moment it is given, so there is nothing on screen to go stale.
    """

    def __init__(
        self,
        context: Quartermaster,
        purse: dict,
        recipients: list[dict],
        *,
        back: Opener,
        destination: str = PARTY_DESTINATION,
    ) -> None:
        super().__init__(context, reopen=back)
        self.purse = purse
        self.recipients = recipients
        self.destination = destination
        select = discord.ui.Select(
            placeholder="Give to…",
            options=_destination_options(
                recipients,
                selected=destination,
                party_label="The treasury",
                party_description="Put it back where the party's money lives",
                recipient_description="Hand it to this character",
            ),
            custom_id="qm:coin:destination",
            row=0,
        )
        select.callback = self._choose_destination(select)
        self.add_item(select)
        give = discord.ui.Button(
            label="Give coin…", style=discord.ButtonStyle.primary, custom_id="qm:coin:give", row=1
        )
        give.callback = self._give
        self.add_item(give)
        self.add_navigation(back, label="◀ Treasury", custom_id="qm:coin:back", row=1)

    def _destination_name(self) -> str:
        if self.destination == PARTY_DESTINATION:
            return "the treasury"
        for recipient in self.recipients:
            if str(recipient["id"]) == self.destination:
                return str(recipient["name"])
        return "the chosen character"

    def _choose_destination(self, select: discord.ui.Select) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            self.destination = select.values[0]
            for option in select.options:
                option.default = option.value == self.destination
            await _rerender(interaction, render_give_coin(self.purse, self._destination_name()), self)

        return callback

    async def _give(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            GiveCurrencyModal(self.context, self.destination, self._destination_name())
        )


class GiveCurrencyModal(QuartermasterModal, title="Give coin"):
    cp = discord.ui.TextInput(label="Copper", placeholder="0", required=False, max_length=12)
    sp = discord.ui.TextInput(label="Silver", placeholder="0", required=False, max_length=12)
    gp = discord.ui.TextInput(label="Gold", placeholder="0", required=False, max_length=12)
    pp = discord.ui.TextInput(label="Platinum", placeholder="0", required=False, max_length=12)

    def __init__(self, context: Quartermaster, destination: str, destination_name: str) -> None:
        super().__init__(context)
        self.destination = destination
        self.title = f"Give coin to {destination_name}"[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amounts = _coin_amounts(
                {"cp": self.cp.value, "sp": self.sp.value, "gp": self.gp.value, "pp": self.pp.value}
            )
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.currency.give_from_character_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    amounts=amounts,
                    destination=self.destination,
                ),
                ephemeral=True,
            )
            await _send_execution(
                interaction, execution, _render_coin_given(execution.value.logical_response), ephemeral=True
            )
        except CurrencyError as error:
            await _send_error(interaction, f"That coin could not be given: {error}")


# DM modals ------------------------------------------------------------------


class GrantLootModal(QuartermasterModal, title="Grant loot"):
    item_name = discord.ui.TextInput(label="Item", placeholder="Silvered dagger", max_length=100)
    quantity = discord.ui.TextInput(label="Quantity", placeholder="1", max_length=7)
    provenance = discord.ui.TextInput(label="Provenance", required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            quantity = _positive_quantity(self.quantity.value)
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.inventory.grant_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    item_name=str(self.item_name.value),
                    quantity=quantity,
                    provenance=str(self.provenance.value).strip() or None,
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Granted {response['quantity']} {response['item_name']}. Total: {response['new_quantity']}.",
                ephemeral=True,
            )
        except InventoryError as error:
            await _send_error(interaction, f"Loot could not be granted: {error}")


class LootDropModal(QuartermasterModal, title="Open a Loot Drop"):
    item_name = discord.ui.TextInput(label="Item", placeholder="Loot gem", max_length=100)
    quantity = discord.ui.TextInput(label="Quantity", placeholder="1", max_length=7)
    expiry_hours = discord.ui.TextInput(label="Expires in (hours)", placeholder="72", required=False, max_length=3)
    provenance = discord.ui.TextInput(label="Provenance", required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            quantity = _positive_quantity(self.quantity.value)
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        raw_expiry = str(self.expiry_hours.value or "").strip()
        try:
            expiry = _positive_quantity(raw_expiry) if raw_expiry else 72
        except ValueError:
            await _send_error(interaction, "Expiry must be a positive whole number of hours.")
            return
        if expiry > 720:
            await _send_error(interaction, "A Loot Drop cannot stay open for more than 720 hours.")
            return
        item = str(self.item_name.value)
        provenance = str(self.provenance.value).strip() or None
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.loot.create_drop_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    items=[(item, quantity, provenance)],
                    expiry_hours=expiry,
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Loot Drop `{response['drop_id'][:8]}` created with {quantity} {item.strip()}.",
                ephemeral=True,
            )
        except LootDropError as error:
            await _send_error(interaction, f"That Loot Drop could not be opened: {error}")


class SessionEndModal(QuartermasterModal, title="End the session"):
    where_ended = discord.ui.TextInput(label="Where did it end?", placeholder="The Sunken Tomb", max_length=200)
    # The other half of "where we stopped", for a table that records. Optional,
    # because most evenings are not recorded and a required field somebody has
    # to put a space in is a required field they have already worked around.
    recording_url = discord.ui.TextInput(
        label="Recording link (optional)",
        placeholder="https://…",
        required=False,
        max_length=RECORDING_URL_LIMIT,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.sessions.end_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    where_ended=str(self.where_ended.value),
                    recording_url=str(self.recording_url.value or "") or None,
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            if response["status"] == "NO_ACTIVE_SESSION":
                await _send_execution(interaction, execution, "There is no active session.", ephemeral=True)
            else:
                await _send_execution(
                    interaction, execution, f"Session {response['session_number']} closed.", ephemeral=True
                )
        except SessionError as error:
            await _send_error(interaction, f"The session could not be closed: {error}")


class CharacterAddModal(QuartermasterModal, title="Register a character"):
    name = discord.ui.TextInput(label="Character name", placeholder="Tamsin", max_length=100)

    def __init__(self, context: Quartermaster, discord_user_id: str | None, player_label: str) -> None:
        super().__init__(context)
        self.discord_user_id = discord_user_id
        self.player_label = player_label

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.characters.create_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    name=str(self.name.value),
                    discord_user_id=self.discord_user_id,
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Registered {response['name']} to {self.player_label}.",
                ephemeral=True,
            )
        except CharacterError as error:
            await _send_error(interaction, f"That character could not be registered: {error}")


class TreasuryAdjustModal(QuartermasterModal, title="Adjust the treasury"):
    cp = discord.ui.TextInput(label="Copper", placeholder="0", required=False, max_length=12)
    sp = discord.ui.TextInput(label="Silver", placeholder="0", required=False, max_length=12)
    gp = discord.ui.TextInput(label="Gold", placeholder="0", required=False, max_length=12)
    pp = discord.ui.TextInput(label="Platinum", placeholder="0", required=False, max_length=12)
    reason = discord.ui.TextInput(label="Reason", required=False, max_length=200)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            deltas = _coin_amounts(
                {"cp": self.cp.value, "sp": self.sp.value, "gp": self.gp.value, "pp": self.pp.value}
            )
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        reason = str(self.reason.value).strip() or None
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.currency.adjust_treasury_interaction(
                    str(interaction.id), actor_id=_actor_id(interaction), deltas=deltas, reason=reason
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction, execution, f"Treasury updated: {format_currency(response['after'])}.", ephemeral=True
            )
        except CurrencyError as error:
            await _send_error(interaction, f"The treasury could not be adjusted: {error}")


def _render_split_preview(preview: dict[str, Any]) -> str:
    """Who the split pays, and what each of them gets, before anything moves."""
    recipients = preview["recipients"]
    names = ", ".join(recipient["name"] for recipient in recipients)
    noun = "character" if len(recipients) == 1 else "characters"
    lines = [
        f"**Split {format_currency(preview['amounts'])}**",
        "",
        f"Among {len(recipients)} active {noun}: {names}",
        f"Each receives {format_currency(preview['per_recipient'])}.",
    ]
    if any(preview["remainder"].values()):
        lines.append(
            f"The treasury keeps {format_currency(preview['remainder'])}, which will not divide evenly."
        )
    lines.extend(["", "Nothing has moved yet."])
    return "\n".join(lines)


def _render_split_result(response: dict[str, Any]) -> str:
    recipients = len(response["recipients"])
    noun = "character" if recipients == 1 else "characters"
    return (
        f"Split among {recipients} active {noun}: "
        f"{format_currency(response['per_recipient'])} each."
    )


class TreasurySplitModal(QuartermasterModal, title="Split the treasury"):
    """Collect the amounts, then show the shares before any coin moves.

    Submitting used to be the split. The share each character gets depends on
    how many are alive, and the DM cannot see the roster from inside a modal —
    so a death between opening this and pressing submit silently changed
    everyone's share and the first anyone knew of it was the receipt. The modal
    now prepares the split and names the recipients; the button on the preview
    is what commits it.
    """

    cp = discord.ui.TextInput(label="Copper", placeholder="0", required=False, max_length=12)
    sp = discord.ui.TextInput(label="Silver", placeholder="0", required=False, max_length=12)
    gp = discord.ui.TextInput(label="Gold", placeholder="0", required=False, max_length=12)
    pp = discord.ui.TextInput(label="Platinum", placeholder="0", required=False, max_length=12)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            amounts = _coin_amounts(
                {"cp": self.cp.value, "sp": self.sp.value, "gp": self.gp.value, "pp": self.pp.value}
            )
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.currency.prepare_split(
                    actor_id=_actor_id(interaction), amounts=amounts
                ),
                ephemeral=True,
            )
        except CurrencyError as error:
            await _send_error(interaction, f"The treasury could not be split: {error}")
            return
        preview = execution.value
        await _send_execution(
            interaction,
            execution,
            _render_split_preview(preview),
            ephemeral=True,
            view=TreasurySplitConfirmationView(self.context, preview["handle_id"], amounts),
        )


class TreasurySplitConfirmationView(QuartermasterView):
    """The button that actually moves the coin.

    Constructed twice on the unhappy path: once against the roster the DM was
    shown, and once — with `confirm_current` set — against the roster as it is
    now, after the first attempt found it had changed.
    """

    def __init__(
        self,
        context: Quartermaster,
        handle_id: str,
        amounts: dict[str, int],
        *,
        confirm_current: bool = False,
    ) -> None:
        super().__init__(context)
        self.handle_id = handle_id
        self.amounts = amounts
        self.confirm_current = confirm_current

    @discord.ui.button(label="Split the treasury", style=discord.ButtonStyle.danger, custom_id="qm:confirm-split")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.currency.split_relative_interaction(
                    str(interaction.id),
                    handle_id=self.handle_id,
                    actor_id=_actor_id(interaction),
                    confirm_current=self.confirm_current,
                ),
                ephemeral=True,
            )
            await _send_execution(
                interaction, execution, _render_split_result(execution.value.logical_response), ephemeral=True
            )
        except CurrencySemanticStaleness:
            # Off the event loop like every other database call, but not through
            # `_run_fast`: the acknowledgement for this interaction may already
            # have been spent deferring the attempt that just refused, and a
            # second deferral of the same interaction is an error.
            try:
                preview = await asyncio.to_thread(self.context.currency.preview_split, amounts=self.amounts)
            except CurrencyError as error:
                await _send_error(interaction, f"The treasury could not be split: {error}")
                return
            await _send_staleness_prompt(
                interaction,
                "The treasury or the roster changed since that preview. This is the split "
                f"against the party as it stands now.\n\n{_render_split_preview(preview)}",
                TreasurySplitConfirmationView(
                    self.context, self.handle_id, self.amounts, confirm_current=True
                ),
            )
        except (HandleError, CurrencyError) as error:
            await _send_error(interaction, f"The treasury could not be split: {error}")


class TreasuryGiveModal(QuartermasterModal, title="Give treasury currency"):
    cp = discord.ui.TextInput(label="Copper", placeholder="0", required=False, max_length=12)
    sp = discord.ui.TextInput(label="Silver", placeholder="0", required=False, max_length=12)
    gp = discord.ui.TextInput(label="Gold", placeholder="0", required=False, max_length=12)
    pp = discord.ui.TextInput(label="Platinum", placeholder="0", required=False, max_length=12)

    def __init__(self, context: Quartermaster, character_id: str, character_name: str) -> None:
        super().__init__(context)
        self.character_id = character_id
        self.character_name = character_name
        self.title = f"Give currency to {character_name}"[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_dm(interaction, self.settings):
            return
        try:
            amounts = _coin_amounts(
                {"cp": self.cp.value, "sp": self.sp.value, "gp": self.gp.value, "pp": self.pp.value}
            )
        except ValueError as error:
            await _send_error(interaction, str(error))
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.context.currency.give_to_character_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    character_id=self.character_id,
                    amounts=amounts,
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Gave {format_currency(response['amount'])} to {response['character_name']}.",
                ephemeral=True,
            )
        except CurrencyError as error:
            await _send_error(interaction, f"That currency could not be given: {error}")
