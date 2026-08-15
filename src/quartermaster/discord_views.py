"""Leaf controls: the components that act, and the modals they open.

The panels in `discord_panels` navigate; everything here changes canonical
state or collects what a change needs. Keeping the two apart is what lets a
panel hand a control the way back to itself without the control having to know
what a panel is.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from .characters import CharacterError
from .currency import CurrencyError, format_currency
from .discord_common import (
    Quartermaster,
    _actor_id,
    _require_dm,
    _run_fast,
    _send_error,
    _send_execution,
)
from .handles import HandleError
from .inventory import InventoryError, SemanticStaleness
from .loot import LootDropError
from .rendering import DISCORD_VIEW_COMPONENT_LIMIT
from .sessions import SessionError

logger = logging.getLogger(__name__)

MAX_VIEW_BUTTONS = DISCORD_VIEW_COMPONENT_LIMIT

#: Handles live for five minutes, so a view that carries them should not outlive
#: them: a button that has quietly expired is worse than one that is gone.
CONTROL_TIMEOUT = 300

Opener = Callable[[discord.Interaction], Awaitable[None]]

PARTY_DESTINATION = "party"


class QuartermasterView(discord.ui.View):
    """A view whose controls answer even when something unforeseen breaks.

    Slash commands route an unexpected exception to `bot.tree.error`, which
    replies and logs. Component callbacks have no equivalent: each one catches
    the domain errors it names, and anything else — a `sqlite3.OperationalError`
    from a contended write, a bug in a renderer — reaches discord.py's default
    `View.on_error`, which logs and leaves the player looking at Discord's bare
    "This interaction failed" with no idea whether their take committed. Every
    view inherits this so the answer is the same wherever it is pressed.
    """

    def __init__(self, context: Quartermaster, *, timeout: float | None = CONTROL_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.context = context
        self.settings = context.settings

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
        super().__init__(context)
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
        await interaction.followup.send(message, ephemeral=True, view=view)
    else:
        await interaction.response.send_message(message, ephemeral=True, view=view)


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
        super().__init__(context)
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
) -> list[discord.SelectOption]:
    options = [
        discord.SelectOption(
            label="The Party Stash",
            value=PARTY_DESTINATION,
            description="Put it back where the party can take it",
            default=selected == PARTY_DESTINATION,
        )
    ]
    for recipient in recipients[: MAX_VIEW_BUTTONS - 1]:
        options.append(
            discord.SelectOption(
                label=str(recipient["name"])[:100],
                value=str(recipient["id"]),
                description="Hand it to this character",
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
        super().__init__(context)
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
        self.add_navigation(back, label="◀ My Items", custom_id="qm:give:back", row=1)

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
            await interaction.response.edit_message(
                content=render_give_item(self.item, self._destination_name()),
                view=self,
            )

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


def render_give_item(item: dict, destination_name: str) -> str:
    return "\n".join(
        [
            "**GIVE**",
            "",
            f"{item['item_name']} · you hold {item['quantity']}",
            f"Going to {destination_name}.",
        ]
    )


def _render_given(response: dict) -> str:
    return (
        f"{response['character_name']} gave {response['quantity']} {response['item_name']}"
        f" to {response['destination_name']}. {response['remaining']} still held."
    )


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


class TreasurySplitModal(QuartermasterModal, title="Split the treasury"):
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
                lambda: self.context.currency.split_treasury_interaction(
                    str(interaction.id), actor_id=_actor_id(interaction), amounts=amounts
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Split among {len(response['recipients'])} active characters: "
                f"{format_currency(response['per_recipient'])} each.",
                ephemeral=True,
            )
        except CurrencyError as error:
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
