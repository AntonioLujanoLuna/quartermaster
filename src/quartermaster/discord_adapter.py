"""discord.py command and component adapter for the current product slice."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

from .config import Settings
from .db import SQLiteStore
from .discord_projection import DiscordProjectionTransport, ProjectionRunner
from .handles import HandleError, HandleRepository
from .inventory import InventoryError, InventoryService, SemanticStaleness
from .loot import LootDropError, LootDropService
from .receipts import ReceiptRepository
from .sessions import SessionError, SessionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotServices:
    store: SQLiteStore
    receipts: ReceiptRepository
    inventory: InventoryService
    sessions: SessionService
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
    def __init__(self, inventory: InventoryService, actor_id: str, items: list[dict]) -> None:
        super().__init__(timeout=300)
        self.inventory = inventory
        self.actor_id = actor_id
        for item in items[:25]:
            try:
                handle_id = inventory.create_take_handle(stack_id=item["id"], actor_id=actor_id, amount=1)
            except InventoryError:
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
                result = self.inventory.take_interaction(
                    str(interaction.id), handle_id=handle_id, actor_id=_actor_id(interaction)
                )
                response = result.logical_response
                await interaction.response.send_message(
                    f"You took {response['quantity']} {response['item_name']}. {response['remaining']} remain.",
                    ephemeral=True,
                )
            except SemanticStaleness:
                await _send_error(interaction, "The quantity changed. Open Party Stash again before taking it.")
            except (HandleError, InventoryError) as error:
                await _send_error(interaction, f"That action could not be completed: {error}")

        return callback


class PartyStashView(discord.ui.View):
    def __init__(self, inventory: InventoryService) -> None:
        super().__init__(timeout=300)
        self.inventory = inventory

    @discord.ui.button(label="Browse", style=discord.ButtonStyle.primary, custom_id="qm:browse")
    async def browse(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        items = self.inventory.browse()
        view = TakeView(self.inventory, _actor_id(interaction), items)
        await interaction.response.send_message(_render_stash(items), ephemeral=True, view=view)


class LootDropView(discord.ui.View):
    def __init__(self, loot: LootDropService, actor_id: str, drops: list[dict]) -> None:
        super().__init__(timeout=300)
        self.loot = loot
        for drop in drops:
            for item in drop["items"][:25]:
                try:
                    handle_id = loot.create_claim_handle(drop_item_id=item["id"], actor_id=actor_id, amount=1)
                except LootDropError:
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
                result = self.loot.claim_interaction(
                    str(interaction.id), handle_id=handle_id, actor_id=_actor_id(interaction)
                )
                response = result.logical_response
                if response["status"] == "CLAIMED":
                    await interaction.response.send_message(
                        f"You claimed {response['quantity']} {response['item_name']}. {response['remaining']} remain.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
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

    @bot.tree.command(name="stash", description="View the Party Stash")
    @app_commands.guilds(guild)
    async def stash(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings):
            await _send_error(interaction, "This bot is configured for a different guild.")
            return
        await interaction.response.send_message(
            _render_stash(services.inventory.browse()), view=PartyStashView(services.inventory)
        )

    @bot.tree.command(name="loot", description="View open Loot Drops")
    @app_commands.guilds(guild)
    async def loot_command(interaction: discord.Interaction) -> None:
        if not _in_configured_guild(interaction, settings):
            await _send_error(interaction, "This bot is configured for a different guild.")
            return
        drops = loot.list_open()
        await interaction.response.send_message(
            _render_loot(drops), ephemeral=True, view=LootDropView(loot, _actor_id(interaction), drops)
        )

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
            result = loot.create_drop_interaction(
                str(interaction.id),
                actor_id=_actor_id(interaction),
                items=[(item, quantity, provenance)],
                expiry_hours=expiry_hours,
            )
            response = result.logical_response
            await interaction.response.send_message(
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
            result = loot.close_drop_interaction(
                str(interaction.id), drop_id=drop_id, actor_id=_actor_id(interaction)
            )
            await interaction.response.send_message(f"Loot Drop `{drop_id[:8]}` closed.")
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
            result = services.inventory.grant_interaction(
                str(interaction.id), actor_id=_actor_id(interaction), item_name=item, quantity=quantity, provenance=provenance
            )
            response = result.logical_response
            await interaction.response.send_message(
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
            result = services.sessions.start_interaction(str(interaction.id), actor_id=_actor_id(interaction))
            response = result.logical_response
            if response["status"] == "ACTIVE_EXISTS":
                await interaction.response.send_message(
                    f"Session {response['active_session_number']} is still active. Close it explicitly before starting another.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(f"Session {response['session_number']} started.")
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
            result = services.sessions.end_interaction(
                str(interaction.id), actor_id=_actor_id(interaction), where_ended=where_ended
            )
            if result.logical_response["status"] == "NO_ACTIVE_SESSION":
                await _send_error(interaction, "There is no active session.")
            else:
                await interaction.response.send_message(
                    f"Session {result.logical_response['session_number']} closed."
                )
        except SessionError as error:
            await _send_error(interaction, str(error))

    async def setup_hook() -> None:
        nonlocal projection_task
        await bot.tree.sync(guild=guild)
        logger.info("synced Quartermaster commands to guild %s", settings.guild_id)
        if settings.party_inventory_channel_id and settings.session_log_channel_id:
            runner = ProjectionRunner(services.store, DiscordProjectionTransport(bot, settings))
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
    store = SQLiteStore(settings.database_path).open()
    receipts = ReceiptRepository(store)
    from .handles import HandleRepository

    handles = HandleRepository(store)
    loot = LootDropService(store, receipts, handles)
    services = BotServices(
        store=store,
        receipts=receipts,
        inventory=InventoryService(store, receipts, handles),
        sessions=SessionService(store, receipts, loot),
        loot=loot,
    )
    bot = create_bot(settings, services)
    bot.run(settings.require_discord_token())
