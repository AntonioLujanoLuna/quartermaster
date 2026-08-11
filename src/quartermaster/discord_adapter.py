"""discord.py command and component adapter for the current product slice."""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings
from .characters import CharacterError, CharacterService
from .currency import CurrencyError, CurrencyService, format_currency
from .db import SQLiteStore
from .discord_projection import DiscordProjectionTransport, ProjectionRunner
from .export import render_export
from .handles import HandleError, HandleRepository
from .inventory import InventoryError, InventoryService, SemanticStaleness
from .loot import LootDropError, LootDropService
from .operations import create_scheduled_backup
from .recovery import recover_startup
from .receipts import ReceiptRepository
from .response import (
    DeferredExecutionError,
    DeferredExecutionResult,
    FastExecutionResult,
    execute_deferred,
    execute_fast,
)
from .sessions import SessionError, SessionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotServices:
    store: SQLiteStore
    receipts: ReceiptRepository
    inventory: InventoryService
    sessions: SessionService
    characters: CharacterService | None = None
    currency: CurrencyService | None = None
    loot: LootDropService | None = None


def _actor_id(interaction: discord.Interaction) -> str:
    return str(interaction.user.id)


def _in_configured_guild(interaction: discord.Interaction, settings: Settings) -> bool:
    return interaction.guild_id == int(settings.guild_id)


async def _is_dm(interaction: discord.Interaction, settings: Settings) -> bool:
    if not interaction.guild:
        return False
    if interaction.guild.owner_id == interaction.user.id:
        return True
    if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild:
        return True
    if isinstance(interaction.user, discord.Member):
        allowed = set(settings.dm_role_ids)
        if allowed.intersection(str(role.id) for role in interaction.user.roles):
            return True
    if interaction.guild.owner_id is None:
        try:
            fetched_guild = await interaction.client.fetch_guild(interaction.guild.id)
        except discord.HTTPException:
            return False
        return fetched_guild.owner_id == interaction.user.id
    return False


async def _send_error(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def _run_fast(
    interaction: discord.Interaction,
    store: SQLiteStore,
    settings: Settings,
    operation: Callable[[], object],
    *,
    ephemeral: bool = False,
) -> FastExecutionResult:
    return await execute_fast(
        interaction,
        operation,
        soft_deadline_seconds=settings.soft_deadline_seconds,
        write_active=lambda: store.write_transaction_active,
        ephemeral=ephemeral,
    )


async def _send_execution(
    interaction: discord.Interaction,
    execution: FastExecutionResult,
    message: str,
    *,
    ephemeral: bool = False,
    view: discord.ui.View | None = None,
) -> None:
    kwargs: dict[str, object] = {"ephemeral": ephemeral}
    if view is not None:
        kwargs["view"] = view
    if execution.deferred:
        await interaction.followup.send(message, **kwargs)
    else:
        await interaction.response.send_message(message, **kwargs)


async def _run_deferred(
    interaction: discord.Interaction,
    services: BotServices,
    operation: Callable[[], object],
    *,
    response_kind: str,
    ephemeral: bool = False,
) -> DeferredExecutionResult:
    return await execute_deferred(
        interaction,
        services.receipts,
        operation,
        actor_id=_actor_id(interaction),
        response_kind=response_kind,
        ephemeral=ephemeral,
    )


async def _send_deferred_export(
    interaction: discord.Interaction,
    execution: DeferredExecutionResult,
) -> None:
    receipt = execution.receipt
    if receipt.status == "PROCESSING":
        await _send_error(interaction, "An export for this interaction is already in progress.")
        return
    if receipt.status == "FAILED":
        await _send_error(
            interaction,
            receipt.logical_response.get("message", "The export could not be completed."),
        )
        return
    export = receipt.logical_response.get("export")
    if not isinstance(export, str):
        await _send_error(interaction, "The stored export result is invalid.")
        return
    file = discord.File(io.BytesIO(export.encode("utf-8")), filename="quartermaster-export.md")
    if execution.deferred:
        await interaction.followup.send("Quartermaster export", file=file, ephemeral=True)
    else:
        await interaction.response.send_message("Quartermaster export", file=file, ephemeral=True)


async def _send_deferred_backup(
    interaction: discord.Interaction,
    execution: DeferredExecutionResult,
) -> None:
    receipt = execution.receipt
    if receipt.status == "PROCESSING":
        await _send_error(interaction, "A backup for this interaction is already in progress.")
        return
    if receipt.status == "FAILED":
        await _send_error(
            interaction,
            receipt.logical_response.get("message", "The backup could not be completed."),
        )
        return
    primary_path = receipt.logical_response.get("primary_path")
    if not isinstance(primary_path, str) or not primary_path:
        await _send_error(interaction, "The stored backup result is invalid.")
        return
    message = (
        f"Backup completed: `{Path(primary_path).name}`. "
        f"Integrity and schema {receipt.logical_response.get('schema_version', '?')} validation passed."
    )
    off_device_path = receipt.logical_response.get("off_device_path")
    if isinstance(off_device_path, str) and off_device_path:
        message += f" Off-device copy: `{Path(off_device_path).name}`."
    if execution.deferred:
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def _render_stash(items: list[dict]) -> str:
    lines = ["**PARTY STASH**", ""]
    if not items:
        lines.append("Nothing is recorded yet.")
    else:
        lines.extend(f"• {item['item_name']} x{item['quantity']}" for item in items)
    return "\n".join(lines)


def _render_loot(drops: list[dict]) -> str:
    lines = ["**OPEN LOOT**", ""]
    if not drops:
        return "\n".join(lines + ["There are no open Loot Drops."])
    for drop in drops:
        lines.append(f"Drop `{drop['drop_id'][:8]}`")
        lines.extend(f"â€¢ {item['item_name']} x{item['remaining_quantity']}" for item in drop["items"])
    return "\n".join(lines)


class TakeView(discord.ui.View):
    def __init__(self, inventory: InventoryService, settings: Settings, actor_id: str, items: list[dict], handles: dict[str, str]) -> None:
        super().__init__(timeout=300)
        self.inventory = inventory
        self.settings = settings
        self.actor_id = actor_id
        for item in items[:25]:
            handle_id = handles.get(item["id"])
            if handle_id is None:
                continue
            button = discord.ui.Button(
                label=f"Take {item['item_name'][:65]}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"qm:h:{handle_id}",
            )
            button.callback = self._callback_for(handle_id)
            self.add_item(button)

    def _callback_for(self, handle_id: str) -> Callable[[discord.Interaction], object]:
        async def callback(interaction: discord.Interaction) -> None:
            try:
                execution = await _run_fast(
                    interaction,
                    self.inventory.store,
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
                self.inventory.store,
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
                self.inventory.store,
                self.settings,
                lambda: self.inventory.prepare_take_view(actor_id=_actor_id(interaction)),
                ephemeral=True,
            )
            prepared = execution.value
            view = TakeView(self.inventory, self.settings, _actor_id(interaction), prepared["items"], prepared["handles"])
            await _send_execution(
                interaction,
                execution,
                _render_stash(prepared["items"]),
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
                    self.loot.store,
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


def create_bot(settings: Settings, services: BotServices) -> commands.Bot:
    intents = discord.Intents.none()
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
    guild = discord.Object(id=int(settings.guild_id))
    projection_task: asyncio.Task | None = None
    stop_event = asyncio.Event()
    loot = services.loot or LootDropService(services.store, services.receipts, HandleRepository(services.store))
    characters = services.characters or CharacterService(services.store, services.receipts)
    currency = services.currency or CurrencyService(services.store, services.receipts, handles=HandleRepository(services.store))

    @bot.tree.command(name="stash", description="View the Party Stash")
    @app_commands.guilds(guild)
    async def stash(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings):
            await _send_error(interaction, "This bot is configured for a different guild.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                services.inventory.browse,
            )
            await _send_execution(
                interaction,
                execution,
                _render_stash(execution.value),
                view=PartyStashView(services.inventory, settings),
            )
        except InventoryError as error:
            await _send_error(interaction, f"Party Stash could not be opened: {error}")

    @bot.tree.command(name="loot", description="View open Loot Drops")
    @app_commands.guilds(guild)
    async def loot_command(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings):
            await _send_error(interaction, "This bot is configured for a different guild.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: loot.prepare_claim_view(actor_id=_actor_id(interaction)),
                ephemeral=True,
            )
            prepared = execution.value
            await _send_execution(
                interaction,
                execution,
                _render_loot(prepared["drops"]),
                ephemeral=True,
                view=LootDropView(loot, settings, _actor_id(interaction), prepared["drops"], prepared["handles"]),
            )
        except LootDropError as error:
            await _send_error(interaction, f"Loot Drops could not be opened: {error}")

    @bot.tree.command(name="treasury", description="View the shared treasury")
    @app_commands.guilds(guild)
    async def treasury(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings):
            await _send_error(interaction, "This bot is configured for a different guild.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                currency.view_treasury,
                ephemeral=True,
            )
            await _send_execution(
                interaction,
                execution,
                f"Treasury: {format_currency(execution.value)}",
                ephemeral=True,
            )
        except CurrencyError as error:
            await _send_error(interaction, f"Treasury could not be read: {error}")

    @bot.tree.command(name="characters", description="List campaign characters")
    @app_commands.guilds(guild)
    async def characters_command(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings):
            await _send_error(interaction, "This bot is configured for a different guild.")
            return
        rows = await asyncio.to_thread(characters.list_characters)
        if not rows:
            message = "No characters are registered."
        else:
            message = "\n".join(
                f"{row['name']} · `{row['id']}` · {row['lifecycle']}"
                for row in rows
            )
        await interaction.response.send_message(message, ephemeral=True)

    @bot.tree.command(name="character-add", description="Register a campaign character")
    @app_commands.guilds(guild)
    @app_commands.describe(name="Character name", discord_user_id="Optional Discord user ID")
    async def character_add(
        interaction: discord.Interaction,
        name: str,
        discord_user_id: str | None = None,
    ) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can register characters.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: characters.create_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    name=name,
                    discord_user_id=discord_user_id,
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Registered {response['name']} with ID `{response['character_id']}`.",
            )
        except CharacterError as error:
            await _send_error(interaction, f"Character could not be registered: {error}")

    @bot.tree.command(name="character-lifecycle", description="Change a character lifecycle state")
    @app_commands.guilds(guild)
    @app_commands.describe(character_id="Character ID", lifecycle="ACTIVE, DEAD, RETIRED, or DEPARTED")
    async def character_lifecycle(
        interaction: discord.Interaction,
        character_id: str,
        lifecycle: str,
    ) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can change character lifecycle.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: characters.transition_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    character_id=character_id,
                    lifecycle=lifecycle,
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"{response['name']} moved from {response['from']} to {response['to']}.",
            )
        except CharacterError as error:
            await _send_error(interaction, f"Character lifecycle could not be changed: {error}")

    @bot.tree.command(name="character-resolve", description="Resolve belongings from a non-active character")
    @app_commands.guilds(guild)
    @app_commands.describe(character_id="Non-active character ID", destination="party or an active character ID")
    async def character_resolve(
        interaction: discord.Interaction,
        character_id: str,
        destination: str,
    ) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can resolve character belongings.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: characters.resolve_belongings_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    character_id=character_id,
                    destination=destination,
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Resolved {response['items_moved']} item stacks and currency from {response['source_character_name']} to {response['destination_name']}.",
            )
        except CharacterError as error:
            await _send_error(interaction, f"Character belongings could not be resolved: {error}")

    @bot.tree.command(name="treasury-adjust", description="Adjust the shared treasury")
    @app_commands.guilds(guild)
    @app_commands.describe(cp="Copper delta", sp="Silver delta", gp="Gold delta", pp="Platinum delta", reason="Optional reason")
    async def treasury_adjust(
        interaction: discord.Interaction,
        cp: int = 0,
        sp: int = 0,
        gp: int = 0,
        pp: int = 0,
        reason: str | None = None,
    ) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can adjust the treasury.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: currency.adjust_treasury_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    deltas={"cp": cp, "sp": sp, "gp": gp, "pp": pp},
                    reason=reason,
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Treasury updated: {format_currency(response['after'])}.",
            )
        except CurrencyError as error:
            await _send_error(interaction, f"Treasury adjustment could not be completed: {error}")

    @bot.tree.command(name="treasury-split", description="Split treasury currency among active characters")
    @app_commands.guilds(guild)
    @app_commands.describe(cp="Copper amount", sp="Silver amount", gp="Gold amount", pp="Platinum amount")
    async def treasury_split(
        interaction: discord.Interaction,
        cp: int = 0,
        sp: int = 0,
        gp: int = 0,
        pp: int = 0,
    ) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can split the treasury.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: currency.split_treasury_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    amounts={"cp": cp, "sp": sp, "gp": gp, "pp": pp},
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Split among {len(response['recipients'])} active characters: {format_currency(response['per_recipient'])} each.",
            )
        except CurrencyError as error:
            await _send_error(interaction, f"Treasury split could not be completed: {error}")

    @bot.tree.command(name="treasury-give", description="Give treasury currency to an active character")
    @app_commands.guilds(guild)
    @app_commands.describe(character_id="Character ID", cp="Copper amount", sp="Silver amount", gp="Gold amount", pp="Platinum amount")
    async def treasury_give(
        interaction: discord.Interaction,
        character_id: str,
        cp: int = 0,
        sp: int = 0,
        gp: int = 0,
        pp: int = 0,
    ) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can give treasury currency.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: currency.give_to_character_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    character_id=character_id,
                    amounts={"cp": cp, "sp": sp, "gp": gp, "pp": pp},
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Gave {format_currency(response['amount'])} to {response['character_name']}.",
            )
        except CurrencyError as error:
            await _send_error(interaction, f"Currency transfer could not be completed: {error}")

    @bot.tree.command(name="export", description="Export canonical Quartermaster state")
    @app_commands.guilds(guild)
    async def export_command(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can export Quartermaster state.")
            return
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

    @bot.tree.command(name="backup", description="Create a validated canonical backup")
    @app_commands.guilds(guild)
    async def backup_command(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can create backups.")
            return
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

    @bot.tree.command(name="loot-drop", description="Create a transient Loot Drop")
    @app_commands.guilds(guild)
    @app_commands.describe(item="Item name", quantity="Positive quantity", expiry_hours="Absolute expiry in hours", provenance="Optional provenance")
    async def loot_drop(
        interaction: discord.Interaction,
        item: str,
        quantity: app_commands.Range[int, 1, 1000000],
        expiry_hours: app_commands.Range[int, 1, 720] = 72,
        provenance: str | None = None,
    ) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can create Loot Drops.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: loot.create_drop_interaction(
                    str(interaction.id),
                    actor_id=_actor_id(interaction),
                    items=[(item, quantity, provenance)],
                    expiry_hours=expiry_hours,
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Loot Drop `{response['drop_id'][:8]}` created with {quantity} {item.strip()}."
            )
        except LootDropError as error:
            await _send_error(interaction, str(error))

    @bot.tree.command(name="loot-close", description="Close a Loot Drop")
    @app_commands.guilds(guild)
    @app_commands.describe(drop_id="Loot Drop ID")
    async def loot_close(interaction: discord.Interaction, drop_id: str) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can close Loot Drops.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: loot.close_drop_interaction(
                    str(interaction.id), drop_id=drop_id, actor_id=_actor_id(interaction)
                ),
            )
            await _send_execution(interaction, execution, f"Loot Drop `{drop_id[:8]}` closed.")
        except LootDropError as error:
            await _send_error(interaction, str(error))

    @bot.tree.command(name="grant", description="Grant an item to the Party Stash")
    @app_commands.guilds(guild)
    @app_commands.describe(item="Item name", quantity="Positive quantity", provenance="Optional provenance")
    async def grant(interaction: discord.Interaction, item: str, quantity: app_commands.Range[int, 1, 1000000], provenance: str | None = None) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can grant Party Stash items.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: services.inventory.grant_interaction(
                    str(interaction.id), actor_id=_actor_id(interaction), item_name=item, quantity=quantity, provenance=provenance
                ),
            )
            response = execution.value.logical_response
            await _send_execution(
                interaction,
                execution,
                f"Granted {response['quantity']} {response['item_name']}. Total: {response['new_quantity']}."
            )
        except InventoryError as error:
            await _send_error(interaction, str(error))

    @bot.tree.command(name="session-start", description="Start the next Quartermaster session")
    @app_commands.guilds(guild)
    async def session_start(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can start sessions.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: services.sessions.start_interaction(str(interaction.id), actor_id=_actor_id(interaction)),
            )
            response = execution.value.logical_response
            if response["status"] == "ACTIVE_EXISTS":
                await _send_execution(
                    interaction,
                    execution,
                    f"Session {response['active_session_number']} is still active. Close it explicitly before starting another.",
                    ephemeral=True,
                )
            else:
                await _send_execution(interaction, execution, f"Session {response['session_number']} started.")
        except SessionError as error:
            await _send_error(interaction, str(error))

    @bot.tree.command(name="session-end", description="Close the active Quartermaster session")
    @app_commands.guilds(guild)
    @app_commands.describe(where_ended="Where the session ended")
    async def session_end(interaction: discord.Interaction, where_ended: str) -> None:
        if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
            await _send_error(interaction, "Only configured DM administrators can end sessions.")
            return
        try:
            execution = await _run_fast(
                interaction,
                services.store,
                settings,
                lambda: services.sessions.end_interaction(
                    str(interaction.id), actor_id=_actor_id(interaction), where_ended=where_ended
                ),
            )
            response = execution.value.logical_response
            if response["status"] == "NO_ACTIVE_SESSION":
                await _send_execution(interaction, execution, "There is no active session.", ephemeral=True)
            else:
                await _send_execution(interaction, execution, f"Session {response['session_number']} closed.")
        except SessionError as error:
            await _send_error(interaction, str(error))

    async def setup_hook() -> None:
        nonlocal projection_task
        await bot.tree.sync(guild=guild)
        logger.info("synced Quartermaster commands to guild %s", settings.guild_id)
        if settings.party_inventory_channel_id and settings.session_log_channel_id:
            runner = ProjectionRunner(
                services.store,
                DiscordProjectionTransport(bot, settings, services.store),
                receipt_retention_seconds=settings.receipt_retention_seconds,
                handle_retention_seconds=settings.handle_retention_seconds,
                backup_directory=str(settings.backup_directory),
                backup_off_device_directory=(
                    str(settings.backup_off_device_directory)
                    if settings.backup_off_device_directory is not None
                    else None
                ),
                backup_retention_count=settings.backup_retention_count,
                backup_interval_seconds=settings.backup_interval_seconds,
                discord_surface_health_max_age_seconds=settings.discord_surface_health_max_age_seconds,
            )
            projection_task = asyncio.create_task(runner.run(stop_event))
        else:
            logger.warning("projection delivery disabled: configure QM_PARTY_INVENTORY_CHANNEL_ID and QM_SESSION_LOG_CHANNEL_ID")

    bot.setup_hook = setup_hook  # type: ignore[method-assign]

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logger.exception("Quartermaster command failed for interaction %s", interaction.id, exc_info=error)
        await _send_error(interaction, "Quartermaster could not complete that command.")

    async def close() -> None:
        stop_event.set()
        if projection_task is not None:
            await projection_task
        services.store.close()
        await commands.Bot.close(bot)

    bot.close = close  # type: ignore[method-assign]
    return bot


def run_bot(settings: Settings) -> None:
    settings.require_discord_token()
    settings.require_projection_channels()
    store = SQLiteStore(settings.database_path).open()
    receipts = ReceiptRepository(store)
    from .handles import HandleRepository

    handles = HandleRepository(store)
    recovery = recover_startup(
        receipts,
        handles,
        receipt_retention_seconds=settings.receipt_retention_seconds,
        handle_retention_seconds=settings.handle_retention_seconds,
    )
    logger.info("startup recovery completed: %s", recovery)
    from .operations import run_maintenance

    maintenance = run_maintenance(
        store,
        receipt_retention_seconds=settings.receipt_retention_seconds,
        handle_retention_seconds=settings.handle_retention_seconds,
    )
    logger.info("startup maintenance completed: %s", maintenance)
    loot = LootDropService(store, receipts, handles)
    services = BotServices(
        store=store,
        receipts=receipts,
        inventory=InventoryService(store, receipts, handles),
        sessions=SessionService(store, receipts, loot),
        characters=CharacterService(store, receipts),
        currency=CurrencyService(store, receipts, handles=handles),
        loot=loot,
    )
    bot = create_bot(settings, services)
    bot.run(settings.require_discord_token())
