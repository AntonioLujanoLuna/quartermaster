"""discord.py bot assembly: services, command registration, and the runtime loop."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .avrae_handoff import AvraeHandoffService
from .characters import CharacterService
from .combat import CombatService
from .config import Settings
from .currency import CurrencyService
from .db import SQLiteStore
from .discord_commands import register_commands
from .discord_common import (
    BotServices,
    Quartermaster,
    _send_error,
)
from .discord_projection import DiscordProjectionTransport, ProjectionRunner
from .handles import HandleRepository
from .inventory import InventoryService
from .loot import LootDropService
from .operations import run_maintenance
from .receipts import ReceiptRepository
from .recovery import recover_startup
from .sessions import SessionService

logger = logging.getLogger(__name__)

__all__ = ["BotServices", "create_bot", "run_bot"]


def create_bot(settings: Settings, services: BotServices) -> commands.Bot:
    intents = discord.Intents.none()
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
    guild = discord.Object(id=int(settings.guild_id))
    handoff = services.avrae_handoff or AvraeHandoffService(services.store)
    projection_task: asyncio.Task | None = None
    api_task: asyncio.Task | None = None
    stop_event = asyncio.Event()
    loot = services.loot or LootDropService(services.store, services.receipts, HandleRepository(services.store))
    characters = services.characters or CharacterService(services.store, services.receipts)
    currency = services.currency or CurrencyService(services.store, services.receipts, handles=HandleRepository(services.store))
    combat_service = services.combat or CombatService(services.store, services.receipts)

    context = Quartermaster(
        services=services,
        settings=settings,
        characters=characters,
        currency=currency,
        loot=loot,
        combat=combat_service,
        handoff=handoff,
    )
    register_commands(bot, guild, context)

    async def setup_hook() -> None:
        nonlocal projection_task, api_task
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
        if settings.activity_enabled:
            # Imported here rather than at module scope: FastAPI and uvicorn
            # are an optional extra, and the bot has to keep starting without
            # them for a table that has not enabled the Activity.
            from .api_server import serve_api

            api_task = asyncio.create_task(serve_api(context, stop_event))

    bot.setup_hook = setup_hook  # type: ignore[method-assign]

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
        logger.exception("Quartermaster command failed for interaction %s", interaction.id, exc_info=error)
        await _send_error(interaction, "Quartermaster could not complete that command.")

    async def close() -> None:
        stop_event.set()
        if projection_task is not None:
            await projection_task
        if api_task is not None:
            # Awaited before the store closes, so an in-flight read finishes
            # against an open connection rather than a closed one.
            await api_task
        services.store.close()
        await commands.Bot.close(bot)

    bot.close = close  # type: ignore[method-assign]
    return bot


def run_bot(settings: Settings) -> None:
    settings.require_discord_token()
    settings.require_projection_channels()
    store = SQLiteStore(settings.database_path).open()
    receipts = ReceiptRepository(store)
    handles = HandleRepository(store)
    recovery = recover_startup(
        store,
        receipts,
        handles,
        receipt_retention_seconds=settings.receipt_retention_seconds,
        handle_retention_seconds=settings.handle_retention_seconds,
    )
    logger.info("startup recovery completed: %s", recovery)
    maintenance = run_maintenance(
        store,
        receipt_retention_seconds=settings.receipt_retention_seconds,
        handle_retention_seconds=settings.handle_retention_seconds,
    )
    logger.info("startup maintenance completed: %s", maintenance)
    loot = LootDropService(store, receipts, handles)
    combat = CombatService(store, receipts)
    services = BotServices(
        store=store,
        receipts=receipts,
        inventory=InventoryService(store, receipts, handles),
        sessions=SessionService(store, receipts, loot, combat),
        characters=CharacterService(store, receipts),
        currency=CurrencyService(store, receipts, handles=handles),
        loot=loot,
        combat=combat,
    )
    bot = create_bot(settings, services)
    bot.run(settings.require_discord_token())
