"""Component views, modals, and launcher actions for the Discord adapter."""

from __future__ import annotations

from collections.abc import Callable

import discord

from .characters import CharacterService
from .config import Settings
from .currency import CurrencyError, CurrencyService, format_currency
from .discord_common import (
    BotServices,
    _actor_id,
    _in_configured_guild,
    _launcher_admin,
    _render_characters,
    _render_loot,
    _render_stash,
    _run_deferred,
    _run_fast,
    _send_deferred_backup,
    _send_deferred_export,
    _send_error,
    _send_execution,
)
from .export import render_export
from .handles import HandleError
from .inventory import InventoryError, InventoryService, SemanticStaleness
from .loot import LootDropError, LootDropService
from .operations import create_scheduled_backup, health_report, render_health
from .rendering import DISCORD_VIEW_COMPONENT_LIMIT
from .response import DeferredExecutionError
from .sessions import SessionError

MAX_VIEW_BUTTONS = DISCORD_VIEW_COMPONENT_LIMIT


class TakeView(discord.ui.View):
    def __init__(
        self,
        inventory: InventoryService,
        settings: Settings,
        actor_id: str,
        items: list[dict],
        handles: dict[str, str],
        take_all_handles: dict[str, str] | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.inventory = inventory
        self.settings = settings
        self.actor_id = actor_id
        take_all_handles = take_all_handles or {}
        for item in items:
            if len(self.children) >= MAX_VIEW_BUTTONS:
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
            if take_all_id is not None and len(self.children) < MAX_VIEW_BUTTONS:
                button = discord.ui.Button(
                    label=f"Take all {item['item_name'][:58]}",
                    style=discord.ButtonStyle.primary,
                    custom_id=f"qm:hall:{take_all_id}",
                )
                button.callback = self._callback_for(take_all_id)
                self.add_item(button)

    def _callback_for(self, handle_id: str) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            try:
                execution = await _run_fast(
                    interaction,
                    self.settings,
                    lambda: self.inventory.take_interaction(
                        str(interaction.id), handle_id=handle_id, actor_id=_actor_id(interaction)
                    ),
                    ephemeral=True,
                )
                result = execution.value
                response = result.logical_response
                await _send_execution(
                    interaction,
                    execution,
                    f"You took {response['quantity']} {response['item_name']}. {response['remaining']} remain.",
                    ephemeral=True,
                )
            except SemanticStaleness:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        "The quantity changed. Confirm taking the current quantity if that is still what you intend.",
                        ephemeral=True,
                        view=TakeConfirmationView(self.inventory, self.settings, handle_id),
                    )
                else:
                    await interaction.response.send_message(
                        "The quantity changed. Confirm taking the current quantity if that is still what you intend.",
                        ephemeral=True,
                        view=TakeConfirmationView(self.inventory, self.settings, handle_id),
                    )
            except (HandleError, InventoryError) as error:
                await _send_error(interaction, f"That action could not be completed: {error}")

        return callback


class TakeConfirmationView(discord.ui.View):
    def __init__(self, inventory: InventoryService, settings: Settings, handle_id: str) -> None:
        super().__init__(timeout=300)
        self.inventory = inventory
        self.settings = settings
        self.handle_id = handle_id

    @discord.ui.button(label="Confirm current quantity", style=discord.ButtonStyle.danger, custom_id="qm:confirm-take")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.inventory.confirm_take_interaction(
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


class PartyStashView(discord.ui.View):
    def __init__(self, inventory: InventoryService, settings: Settings) -> None:
        super().__init__(timeout=300)
        self.inventory = inventory
        self.settings = settings

    @discord.ui.button(label="Browse", style=discord.ButtonStyle.primary, custom_id="qm:browse")
    async def browse(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.inventory.prepare_take_view(actor_id=_actor_id(interaction)),
                ephemeral=True,
            )
            prepared = execution.value
            view = TakeView(
                self.inventory,
                self.settings,
                _actor_id(interaction),
                prepared["items"],
                prepared["handles"],
                prepared["take_all_handles"],
            )
            await _send_execution(
                interaction,
                execution,
                _render_stash(
                    prepared["items"],
                    total=prepared.get("total_items"),
                    controls=prepared["handles"],
                ),
                ephemeral=True,
                view=view,
            )
        except InventoryError as error:
            await _send_error(interaction, f"Party Stash could not be opened: {error}")


class LootDropView(discord.ui.View):
    def __init__(self, loot: LootDropService, settings: Settings, actor_id: str, drops: list[dict], handles: dict[str, str]) -> None:
        super().__init__(timeout=300)
        self.loot = loot
        self.settings = settings
        for drop in drops:
            for item in drop["items"][:25]:
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

    def _callback_for(self, handle_id: str) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            try:
                execution = await _run_fast(
                    interaction,
                    self.settings,
                    lambda: self.loot.claim_interaction(
                        str(interaction.id), handle_id=handle_id, actor_id=_actor_id(interaction)
                    ),
                    ephemeral=True,
                )
                result = execution.value
                response = result.logical_response
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


def _launcher_snapshot(services: BotServices, characters: CharacterService) -> dict[str, int | None]:
    items = services.inventory.browse()
    character_rows = characters.list_characters()
    with services.store.read() as connection:
        active = connection.execute(
            "SELECT session_number FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
        ).fetchone()
    return {
        "stash_count": len(items),
        "active_session_number": int(active["session_number"]) if active else None,
        "unresolved_estates": sum(1 for row in character_rows if row["lifecycle"] != "ACTIVE"),
    }


def _render_launcher(snapshot: dict[str, int | None]) -> str:
    session = (
        f"Session {snapshot['active_session_number']} active"
        if snapshot["active_session_number"] is not None
        else "No active session"
    )
    lines = [
        "**QUARTERMASTER**",
        "",
        f"Party Stash · {snapshot['stash_count']} entries",
        session,
    ]
    estates = int(snapshot["unresolved_estates"] or 0)
    if estates:
        suffix = "estate" if estates == 1 else "estates"
        lines.append(f"{estates} unresolved character {suffix}")
    return "\n".join(lines)


async def _launcher_stash(interaction: discord.Interaction, services: BotServices, settings: Settings) -> None:
    if not _in_configured_guild(interaction, settings):
        await _send_error(interaction, "This bot is configured for a different guild.")
        return
    try:
        execution = await _run_fast(interaction, settings, services.inventory.browse, ephemeral=True)
        await _send_execution(
            interaction,
            execution,
            _render_stash(execution.value),
            ephemeral=True,
            view=PartyStashView(services.inventory, settings),
        )
    except InventoryError as error:
        await _send_error(interaction, f"Party Stash could not be opened: {error}")


async def _launcher_loot(
    interaction: discord.Interaction,
    services: BotServices,
    loot: LootDropService,
    settings: Settings,
) -> None:
    if not _in_configured_guild(interaction, settings):
        await _send_error(interaction, "This bot is configured for a different guild.")
        return
    try:
        execution = await _run_fast(
            interaction,
            settings,
            lambda: loot.prepare_claim_view(actor_id=_actor_id(interaction)),
            ephemeral=True,
        )
        prepared = execution.value
        await _send_execution(
            interaction,
            execution,
            _render_loot(prepared["drops"], prepared["handles"]),
            ephemeral=True,
            view=LootDropView(loot, settings, _actor_id(interaction), prepared["drops"], prepared["handles"]),
        )
    except LootDropError as error:
        await _send_error(interaction, f"Loot Drops could not be opened: {error}")


async def _launcher_treasury(
    interaction: discord.Interaction,
    services: BotServices,
    currency: CurrencyService,
    settings: Settings,
) -> None:
    if not _in_configured_guild(interaction, settings):
        await _send_error(interaction, "This bot is configured for a different guild.")
        return
    try:
        execution = await _run_fast(interaction, settings, currency.view_treasury, ephemeral=True)
        await _send_execution(
            interaction,
            execution,
            f"Treasury: {format_currency(execution.value)}",
            ephemeral=True,
        )
    except CurrencyError as error:
        await _send_error(interaction, f"Treasury could not be read: {error}")


async def _launcher_characters(
    interaction: discord.Interaction,
    services: BotServices,
    characters: CharacterService,
    settings: Settings,
) -> None:
    if not _in_configured_guild(interaction, settings):
        await _send_error(interaction, "This bot is configured for a different guild.")
        return
    execution = await _run_fast(
        interaction,
        settings,
        characters.list_characters,
        ephemeral=True,
    )
    await _send_execution(interaction, execution, _render_characters(execution.value), ephemeral=True)


async def _launcher_export(interaction: discord.Interaction, services: BotServices) -> None:
    try:
        execution = await _run_deferred(
            interaction,
            services,
            lambda: {"export": render_export(services.store)},
            response_kind="export",
            ephemeral=True,
        )
        await _send_deferred_export(interaction, execution)
    except DeferredExecutionError as error:
        await _send_error(interaction, str(error))


async def _launcher_backup(interaction: discord.Interaction, services: BotServices, settings: Settings) -> None:
    try:
        execution = await _run_deferred(
            interaction,
            services,
            lambda: create_scheduled_backup(
                services.store,
                settings.backup_directory,
                off_device_directory=settings.backup_off_device_directory,
                retention_count=settings.backup_retention_count,
            ),
            response_kind="backup",
            ephemeral=True,
        )
        await _send_deferred_backup(interaction, execution)
    except DeferredExecutionError as error:
        await _send_error(interaction, str(error))


class LauncherMoreView(discord.ui.View):
    def __init__(
        self,
        services: BotServices,
        settings: Settings,
        characters: CharacterService,
        currency: CurrencyService,
        loot: LootDropService,
    ) -> None:
        super().__init__(timeout=600)
        self.services = services
        self.settings = settings
        self.characters = characters
        self.currency = currency
        self.loot = loot

    @discord.ui.button(label="Stash", style=discord.ButtonStyle.secondary, custom_id="qm:launcher:stash")
    async def stash(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await _launcher_stash(interaction, self.services, self.settings)

    @discord.ui.button(label="Open Loot", style=discord.ButtonStyle.secondary, custom_id="qm:launcher:loot")
    async def loot_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await _launcher_loot(interaction, self.services, self.loot, self.settings)

    @discord.ui.button(label="Treasury", style=discord.ButtonStyle.secondary, custom_id="qm:launcher:treasury")
    async def treasury(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await _launcher_treasury(interaction, self.services, self.currency, self.settings)

    @discord.ui.button(label="Characters", style=discord.ButtonStyle.secondary, custom_id="qm:launcher:characters")
    async def characters_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await _launcher_characters(interaction, self.services, self.characters, self.settings)

    @discord.ui.button(label="Export", style=discord.ButtonStyle.primary, custom_id="qm:launcher:export")
    async def export(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await _launcher_export(interaction, self.services)

    @discord.ui.button(label="Backup", style=discord.ButtonStyle.primary, custom_id="qm:launcher:backup")
    async def backup(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await _launcher_backup(interaction, self.services, self.settings)

    @discord.ui.button(label="Health", style=discord.ButtonStyle.secondary, custom_id="qm:launcher:health")
    async def health(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _launcher_admin(interaction, self.settings):
            return
        execution = await _run_fast(
            interaction,
            self.settings,
            lambda: render_health(health_report(self.services.store)),
            ephemeral=True,
        )
        await _send_execution(interaction, execution, execution.value, ephemeral=True)


class GrantLootModal(discord.ui.Modal, title="Grant loot"):
    item_name = discord.ui.TextInput(label="Item", placeholder="Silvered dagger", max_length=100)
    quantity = discord.ui.TextInput(label="Quantity", placeholder="1", max_length=7)
    provenance = discord.ui.TextInput(label="Provenance", required=False, max_length=200)

    def __init__(self, inventory: InventoryService, settings: Settings) -> None:
        super().__init__()
        self.inventory = inventory
        self.settings = settings

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _launcher_admin(interaction, self.settings):
            return
        try:
            quantity = int(str(self.quantity.value).strip())
        except ValueError:
            await _send_error(interaction, "Quantity must be a positive whole number.")
            return
        if quantity <= 0:
            await _send_error(interaction, "Quantity must be a positive whole number.")
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.inventory.grant_interaction(
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


class CombatCloseoutView(discord.ui.View):
    """The end-of-combat controls: spoils into the stash, or the open drops.

    Combat ending is the moment loot exists, and it was also the moment the
    handoff used to stop — the DM read `!i end` and was left to remember which
    command records what they just won. These two buttons are the existing
    Party Stash and Loot Drop workflows reached from where the fight ended.
    """

    def __init__(
        self,
        services: BotServices,
        settings: Settings,
        loot: LootDropService,
    ) -> None:
        super().__init__(timeout=600)
        self.services = services
        self.settings = settings
        self.loot = loot

    @discord.ui.button(label="Record spoils", style=discord.ButtonStyle.primary, custom_id="qm:combat:grant")
    async def grant(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await interaction.response.send_modal(GrantLootModal(self.services.inventory, self.settings))

    @discord.ui.button(label="Open Loot", style=discord.ButtonStyle.secondary, custom_id="qm:combat:loot")
    async def loot_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await _launcher_loot(interaction, self.services, self.loot, self.settings)


class QuartermasterLauncherView(discord.ui.View):
    def __init__(
        self,
        services: BotServices,
        settings: Settings,
        characters: CharacterService,
        currency: CurrencyService,
        loot: LootDropService,
    ) -> None:
        super().__init__(timeout=600)
        self.services = services
        self.settings = settings
        self.characters = characters
        self.currency = currency
        self.loot = loot

    @discord.ui.button(label="Grant loot", style=discord.ButtonStyle.primary, custom_id="qm:launcher:grant")
    async def grant(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if await _launcher_admin(interaction, self.settings):
            await interaction.response.send_modal(GrantLootModal(self.services.inventory, self.settings))

    @discord.ui.button(label="Session", style=discord.ButtonStyle.primary, custom_id="qm:launcher:session")
    async def session(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _launcher_admin(interaction, self.settings):
            return
        try:
            execution = await _run_fast(
                interaction,
                self.settings,
                lambda: self.services.sessions.start_interaction(
                    str(interaction.id), actor_id=_actor_id(interaction)
                ),
                ephemeral=True,
            )
            response = execution.value.logical_response
            if response["status"] == "ACTIVE_EXISTS":
                message = (
                    f"Session {response['active_session_number']} is still active. "
                    "End it explicitly with /session-end before starting another."
                )
            else:
                message = f"Session {response['session_number']} started."
            await _send_execution(interaction, execution, message, ephemeral=True)
        except SessionError as error:
            await _send_error(interaction, f"Session could not be started: {error}")

    @discord.ui.button(label="More…", style=discord.ButtonStyle.secondary, custom_id="qm:launcher:more")
    async def more(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not await _launcher_admin(interaction, self.settings):
            return
        await interaction.response.send_message(
            "Quartermaster admin actions",
            ephemeral=True,
            view=LauncherMoreView(
                self.services,
                self.settings,
                self.characters,
                self.currency,
                self.loot,
            ),
        )
