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
        lines.extend(f"• {item['item_name']} x{item['remaining_quantity']}" for item in drop["items"])
    return "\n".join(lines)


def _render_characters(rows: list[dict]) -> str:
    if not rows:
        return "No characters are registered."
    return "\n".join(f"{row['name']} · `{row['id']}` · {row['lifecycle']}" for row in rows)


async def _launcher_admin(interaction: discord.Interaction, settings: Settings) -> bool:
    if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
        await _send_error(interaction, "Only configured DM administrators can use the Quartermaster launcher.")
        return False
    return True
