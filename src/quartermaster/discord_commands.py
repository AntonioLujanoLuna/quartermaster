"""Guild-scoped slash command registration for the Discord adapter."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from .avrae_handoff import AvraeHandoffError, AvraeHandoffService
from .characters import CharacterError, CharacterService
from .config import Settings
from .currency import CurrencyError, CurrencyService, format_currency
from .discord_common import (
    BotServices,
    _actor_id,
    _in_configured_guild,
    _is_dm,
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
from .discord_views import (
    LootDropView,
    PartyStashView,
    QuartermasterLauncherView,
    _launcher_snapshot,
    _render_launcher,
)
from .export import render_export
from .inventory import InventoryError
from .loot import LootDropError, LootDropService
from .operations import create_scheduled_backup
from .response import DeferredExecutionError
from .sessions import SessionError


async def _send_avrae_handoff(
    interaction: discord.Interaction,
    services: BotServices,
    settings: Settings,
    handoff: AvraeHandoffService,
    operation_kind: str,
) -> None:
    if not _in_configured_guild(interaction, settings):
        await _send_error(interaction, "This bot is configured for a different guild.")
        return
    try:
        execution = await _run_fast(
            interaction,
            services.store,
            settings,
            lambda: handoff.build(operation_kind, channel_id=str(interaction.channel_id)),
            ephemeral=True,
        )
        card = execution.value
        await _send_execution(interaction, execution, card.render(), ephemeral=True)
    except AvraeHandoffError as error:
        await _send_error(interaction, f"Avrae handoff could not be prepared: {error}")


def register_commands(
    bot: commands.Bot,
    guild: discord.Object,
    settings: Settings,
    services: BotServices,
    *,
    characters: CharacterService,
    currency: CurrencyService,
    loot: LootDropService,
    handoff: AvraeHandoffService,
) -> None:
    """Register every guild-scoped Quartermaster command on the given bot tree."""

    @bot.tree.command(name="quartermaster", description="Open the Quartermaster admin launcher")
    @app_commands.guilds(guild)
    async def quartermaster(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await _launcher_admin(interaction, settings):
            return
        snapshot = await asyncio.to_thread(_launcher_snapshot, services, characters)
        await interaction.followup.send(
            _render_launcher(snapshot),
            ephemeral=True,
            view=QuartermasterLauncherView(services, settings, characters, currency, loot),
        )

    @bot.tree.command(name="combat", description="Open the Quartermaster to Avrae combat handoff")
    @app_commands.guilds(guild)
    @app_commands.describe(action="The native Avrae combat action to prepare")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Start combat", value="start"),
            app_commands.Choice(name="Join combat", value="join"),
            app_commands.Choice(name="Advance turn", value="next"),
            app_commands.Choice(name="Attack", value="attack"),
            app_commands.Choice(name="Cast spell", value="cast"),
            app_commands.Choice(name="Skill check", value="check"),
            app_commands.Choice(name="Saving throw", value="save"),
            app_commands.Choice(name="End combat", value="end"),
            app_commands.Choice(name="Combat status", value="status"),
        ]
    )
    async def combat(interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        await _send_avrae_handoff(interaction, services, settings, handoff, action.value)

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
        execution = await _run_fast(
            interaction,
            services.store,
            settings,
            characters.list_characters,
            ephemeral=True,
        )
        await _send_execution(interaction, execution, _render_characters(execution.value), ephemeral=True)

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
