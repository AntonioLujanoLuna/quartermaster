"""Shared Discord adapter primitives: services, authorization, and responses."""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import discord

from .avrae_handoff import AvraeHandoffService
from .characters import CharacterService
from .config import Settings
from .currency import CurrencyService
from .db import SQLiteStore
from .inventory import InventoryService
from .loot import LootDropService
from .receipts import ReceiptRepository
from .rendering import clamp_discord_content, fit_discord_lines
from .response import (
    DeferredExecutionResult,
    FastExecutionResult,
    execute_deferred,
    execute_fast,
)
from .sessions import SessionService


@dataclass(frozen=True)
class BotServices:
    store: SQLiteStore
    receipts: ReceiptRepository
    inventory: InventoryService
    sessions: SessionService
    characters: CharacterService | None = None
    currency: CurrencyService | None = None
    loot: LootDropService | None = None
    avrae_handoff: AvraeHandoffService | None = None


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
    """Reply with a failure, never with content Discord will refuse.

    Error text quotes whatever the caller supplied — an item name, a database
    error, a health report — so it is bounded here rather than trusted. An
    over-long error is the worst place to lose a reply: the player is already
    looking at something that did not work.
    """
    content = clamp_discord_content(message)
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


async def _run_fast(
    interaction: discord.Interaction,
    settings: Settings,
    operation: Callable[[], object],
    *,
    ephemeral: bool = False,
) -> FastExecutionResult:
    return await execute_fast(
        interaction,
        operation,
        soft_deadline_seconds=settings.soft_deadline_seconds,
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
    content = clamp_discord_content(message)
    if execution.deferred:
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


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


def _render_stash(items: list[dict], *, total: int | None = None) -> str:
    """Render the stash, saying so when the browse view holds only part of it.

    `total` is the number of stacks the Party Stash actually holds. The browse
    snapshot is capped at what one component view can carry, and a player who is
    not told that reads a short list as the whole stash.
    """
    lines = ["**PARTY STASH**", ""]
    if not items:
        lines.append("Nothing is recorded yet.")
    else:
        lines.extend(f"• {item['item_name']} x{item['quantity']}" for item in items)
    if total is not None and total > len(items):
        lines.append("")
        lines.append(f"Showing {len(items)} of {total} stacks. Take some, or ask the DM for the full export.")
    return fit_discord_lines(lines, label="Party Stash")


def _render_loot(drops: list[dict], handles: dict[str, str] | None = None) -> str:
    """Render open Loot Drops, saying so when some items have no claim control.

    One component view carries a bounded number of buttons, so beyond that an
    item is listed with nothing to press. Naming the gap is the difference
    between a player waiting for a control that is never coming and a player
    who knows another item has to be claimed or closed first.
    """
    lines = ["**OPEN LOOT**", ""]
    if not drops:
        return "\n".join(lines + ["There are no open Loot Drops."])
    unclaimable = 0
    for drop in drops:
        lines.append(f"Drop `{drop['drop_id'][:8]}`")
        for item in drop["items"]:
            lines.append(f"• {item['item_name']} x{item['remaining_quantity']}")
            if handles is not None and item["id"] not in handles:
                unclaimable += 1
    if unclaimable:
        entries = "entry" if unclaimable == 1 else "entries"
        lines.append("")
        lines.append(
            f"{unclaimable} {entries} above have no claim control here. "
            "Claim or close what is showing and open this again."
        )
    return fit_discord_lines(lines, label="Loot Drop")


def _render_characters(rows: list[dict]) -> str:
    if not rows:
        return "No characters are registered."
    return fit_discord_lines(
        [f"{row['name']} · `{row['id']}` · {row['lifecycle']}" for row in rows],
        label="character",
    )


async def _launcher_admin(interaction: discord.Interaction, settings: Settings) -> bool:
    if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
        await _send_error(interaction, "Only configured DM administrators can use the Quartermaster launcher.")
        return False
    return True
