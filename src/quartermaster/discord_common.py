"""Shared Discord adapter primitives: services, authorization, and responses."""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import discord

from .avrae_handoff import AvraeHandoffService, native_command
from .characters import CharacterService
from .combat import CombatService, format_duration
from .config import Settings
from .currency import CurrencyService
from .db import SQLiteStore
from .dice import DiceService
from .dossiers import CharacterDossierService
from .integration import AvraeGateway, ProviderIntegrationService
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
    avrae_handoff: AvraeHandoffService | None = None
    combat: CombatService | None = None
    dice: DiceService | None = None
    dossiers: CharacterDossierService | None = None
    provider_operations: ProviderIntegrationService | None = None


@dataclass(frozen=True)
class Quartermaster:
    """Everything a panel needs, resolved once at bot assembly.

    `BotServices` leaves the later services optional because callers built it
    before they existed. A panel cannot work with `None`, and threading six
    separately-defaulted services through every view constructor is how one
    panel ends up holding a different `LootDropService` than the panel it
    navigated from. The adapter fills the gaps once, here.
    """

    services: BotServices
    settings: Settings
    characters: CharacterService
    currency: CurrencyService
    loot: LootDropService
    combat: CombatService
    handoff: AvraeHandoffService
    avrae_gateway: AvraeGateway | None = None
    dice: DiceService | None = None
    dossiers: CharacterDossierService | None = None
    provider_operations: ProviderIntegrationService | None = None

    @property
    def inventory(self) -> InventoryService:
        return self.services.inventory

    @property
    def sessions(self) -> SessionService:
        return self.services.sessions

    @property
    def store(self) -> SQLiteStore:
        return self.services.store


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


def _interaction_label(interaction: discord.Interaction) -> str:
    """Name what was pressed, from identifiers Quartermaster itself authored.

    Custom IDs are static strings from this package (`qm:stash:take`) and a
    command name is the one command there is, so a latency line can say which
    surface was slow without quoting anything a person typed or chose. A
    select menu's `values` are canonical IDs the caller picked; they are not
    read here.
    """
    data = getattr(interaction, "data", None) or {}
    if not isinstance(data, dict):
        return "interaction"
    custom_id = data.get("custom_id")
    if custom_id:
        return str(custom_id)
    name = data.get("name")
    return f"/{name}" if name else "interaction"


def _record_ack_latency(
    settings: Settings, interaction: discord.Interaction, latency_ms: float | None
) -> None:
    """Log what the acknowledgement actually cost, once per interaction.

    The release gate asks for measured acknowledgement latency inside the
    configured budget, and until this the build could not answer it: the
    numbers were computed and dropped on the floor, and
    `internal_hard_deadline_seconds` was validated at startup by a process that
    never read it. Local metric histograms were removed on purpose — at one
    table's volume percentiles cannot carry meaning — but one line per
    interaction can, and a warning the moment the budget is missed is what an
    operator can act on. Nothing here names the actor: latency is a property of
    the host, not of the person who pressed the button.
    """
    if latency_ms is None:
        return
    budget_ms = settings.internal_hard_deadline_seconds * 1000
    if latency_ms >= budget_ms:
        logger.warning(
            "acknowledgement for %s took %.0fms, past the %.0fms internal hard deadline",
            _interaction_label(interaction),
            latency_ms,
            budget_ms,
        )
    else:
        logger.info("acknowledged %s in %.0fms", _interaction_label(interaction), latency_ms)


async def _run_fast(
    interaction: discord.Interaction,
    settings: Settings,
    operation: Callable[[], object],
    *,
    ephemeral: bool = False,
) -> FastExecutionResult:
    execution = await execute_fast(
        interaction,
        operation,
        soft_deadline_seconds=settings.soft_deadline_seconds,
        ephemeral=ephemeral,
    )
    _record_ack_latency(settings, interaction, execution.ack_latency_ms)
    return execution


def _bind_view(
    view: discord.ui.View | None,
    editor: Callable[..., object],
    *,
    screen: int | None = None,
) -> None:
    """Hand a view the route back to the message it was just sent on.

    A Quartermaster view retires its own controls when it expires, and an
    ephemeral message can only be edited through the interaction or webhook
    that produced it — which only the code doing the sending knows. This is
    duck-typed because the response helpers live below the views that use
    them: `discord_views` imports this module, not the other way round.
    """
    bind = getattr(view, "bind", None)
    if bind is None:
        return
    bind(editor, screen=screen)


def _message_id(message: object) -> int | None:
    identifier = getattr(message, "id", None)
    return int(identifier) if identifier is not None else None


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
        sent = await interaction.followup.send(content, wait=view is not None, **kwargs)
        if view is not None:
            _bind_view(view, sent.edit, screen=_message_id(sent))
    else:
        await interaction.response.send_message(content, **kwargs)
        if view is not None:
            _bind_view(view, interaction.edit_original_response)


async def _send_panel(
    interaction: discord.Interaction,
    content: str,
    view: discord.ui.View,
    *,
    execution: FastExecutionResult | None = None,
) -> None:
    """Render a panel where the caller is already looking.

    Navigation is not a result. Pressing Treasury on the home panel should
    replace what is on screen, not leave the player scrolling back through a
    column of ephemeral messages to find the panel they started from. Editing
    in place needs both the message the component belongs to and an unspent
    acknowledgement, so the first panel of a session — sent from the slash
    command, with no message to edit — and any panel whose read overran the
    soft deadline both fall back to sending one.
    """
    body = clamp_discord_content(content)
    deferred = execution is not None and execution.deferred
    message = getattr(interaction, "message", None)
    if not deferred and message is not None and not interaction.response.is_done():
        await interaction.response.edit_message(content=body, view=view)
        _bind_view(view, interaction.edit_original_response, screen=_message_id(message))
        return
    if interaction.response.is_done():
        sent = await interaction.followup.send(body, view=view, ephemeral=True, wait=True)
        _bind_view(view, sent.edit, screen=_message_id(sent))
    else:
        await interaction.response.send_message(body, view=view, ephemeral=True)
        _bind_view(view, interaction.edit_original_response)


async def _rerender(
    interaction: discord.Interaction,
    content: str,
    view: discord.ui.View,
) -> None:
    """Redraw a view on the message it is already on, and renew its way back.

    A select that changes what a panel says redraws the same view rather than
    navigating, so the view outlives the interaction it was first sent on.
    Rebinding here keeps the route it would use to retire itself pointed at the
    most recent interaction, whose token is the one still alive.
    """
    await interaction.response.edit_message(content=clamp_discord_content(content), view=view)
    _bind_view(
        view,
        interaction.edit_original_response,
        screen=_message_id(getattr(interaction, "message", None)),
    )


async def _run_deferred(
    interaction: discord.Interaction,
    services: BotServices,
    operation: Callable[[], object],
    *,
    settings: Settings,
    response_kind: str,
    ephemeral: bool = False,
) -> DeferredExecutionResult:
    execution = await execute_deferred(
        interaction,
        services.receipts,
        operation,
        actor_id=_actor_id(interaction),
        response_kind=response_kind,
        ephemeral=ephemeral,
    )
    _record_ack_latency(settings, interaction, execution.ack_latency_ms)
    return execution


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


def _render_stash(
    items: list[dict],
    *,
    total: int | None = None,
    controls: dict[str, str] | None = None,
) -> str:
    """Render the stash, saying so when the browse view holds only part of it.

    `total` is the number of stacks the Party Stash actually holds. The browse
    snapshot is capped at what one component view can carry, and a player who is
    not told that reads a short list as the whole stash.

    `controls` names the stacks that have a button in the view being sent with
    this message. A stack above one costs two controls and one view holds
    twenty-five, so a full snapshot can list stacks the view has no room for —
    the same gap the Loot Drop listing names, and just as invisible if it is not
    said out loud.
    """
    lines = ["**PARTY STASH**", ""]
    if not items:
        lines.append("Nothing is recorded yet.")
    else:
        lines.extend(f"• {item['item_name']} x{item['quantity']}" for item in items)
    uncontrolled = 0 if controls is None else sum(1 for item in items if item["id"] not in controls)
    if uncontrolled:
        entries = "entry" if uncontrolled == 1 else "entries"
        lines.append("")
        lines.append(
            f"The last {uncontrolled} {entries} above have no take control here. "
            "Take what is showing and open this again."
        )
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
        return "\n".join([*lines, "There are no open Loot Drops."])
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


_AVRAE_AUTHORITY = (
    "Avrae holds initiative, HP, conditions, and every mechanical result. "
    "Quartermaster tracks only that the fight is happening."
)
_NO_SESSION = (
    "No active Quartermaster session. The DM opens one from **DM Tools → Session** "
    "before combat can be recorded."
)


def _render_open_drops(drops: list[dict]) -> list[str]:
    """The outstanding-loot lines shared by the status and closeout cards."""
    if not drops:
        return []
    lines = ["", "Open Loot Drops in this session:"]
    for drop in drops:
        remaining = drop["remaining_quantity"]
        if not remaining:
            lines.append(f"• `{drop['drop_id'][:8]}` — nothing left unclaimed, still open")
            continue
        entries = "entry" if drop["item_count"] == 1 else "entries"
        lines.append(f"• `{drop['drop_id'][:8]}` — {remaining} unclaimed across {drop['item_count']} {entries}")
    return lines


def _render_combat_opened(response: dict) -> str:
    if response["status"] == "NO_ACTIVE_SESSION":
        return f"**COMBAT**\n\n{_NO_SESSION}"
    if response["status"] == "ALREADY_OPEN":
        duration = format_duration(response["elapsed_seconds"])
        running = f", running {duration}" if duration else ""
        return (
            "**COMBAT ALREADY OPEN**\n\n"
            f"Session {response['session_number']} already has combat open in <#{response['channel_id']}>{running}.\n"
            "Close it with **End combat** on this panel before opening another."
        )
    lines = [
        "**COMBAT OPEN**",
        "",
        f"Session {response['session_number']} · Quartermaster is tracking combat in <#{response['channel_id']}>.",
        "",
        "Start it in Avrae:",
        f"`{native_command('start')}`",
        "",
        "Players join from **Combat → Join**. Close it with **End combat** when the fight is done.",
    ]
    return "\n".join(lines)


def _render_combat_closed(response: dict) -> str:
    """The end-of-combat card: what closed, and where the spoils go next."""
    if response["status"] == "NO_ACTIVE_SESSION":
        return f"**COMBAT**\n\n{_NO_SESSION}"
    if response["status"] == "NO_OPEN_COMBAT":
        lines = [
            "**COMBAT**",
            "",
            f"Session {response['session_number']} has no open Quartermaster combat to close.",
            f"End the Avrae tracker with `{native_command('end')}` if it is still running.",
        ]
        lines.extend(_render_open_drops(response["open_drops"]))
        return fit_discord_lines(lines, label="Loot Drop")
    duration = format_duration(response["elapsed_seconds"])
    ran = f" after {duration}" if duration else ""
    lines = [
        "**COMBAT CLOSED**",
        "",
        f"Session {response['session_number']} · combat in <#{response['channel_id']}> closed{ran}.",
    ]
    if response["outcome"]:
        lines.append(f"Outcome: {response['outcome']}")
    lines.extend(["", "End it in Avrae too, if you have not already:", f"`{native_command('end')}`"])
    lines.extend(_render_open_drops(response["open_drops"]))
    lines.extend(
        [
            "",
            "Spoils: **Open Loot** below starts a claimable drop for the party, or record them "
            "straight into the Party Stash with **Record spoils**.",
        ]
    )
    return fit_discord_lines(lines, label="Loot Drop")


def _render_combat_status(status: dict) -> str:
    """Quartermaster's own view of the fight, with no Avrae state in it."""
    lines = ["**COMBAT STATUS**", ""]
    if status["status"] == "NO_ACTIVE_SESSION":
        lines.append(_NO_SESSION)
        return "\n".join(lines)
    lines.append(f"Session {status['session_number']} is active.")
    encounter = status["encounter"]
    if encounter is not None:
        duration = format_duration(encounter["elapsed_seconds"])
        sentence = f"Combat is open in <#{encounter['channel_id']}>"
        if duration:
            sentence += f", running {duration}"
        if encounter["opened_by"]:
            sentence += f", opened by <@{encounter['opened_by']}>"
        lines.append(sentence + ".")
    else:
        lines.append("No Quartermaster combat is open. **Start combat** opens one.")
        last = status["last_closed"]
        if last is not None:
            ran = format_duration(last["elapsed_seconds"])
            ago = format_duration(last["closed_seconds_ago"])
            sentence = "The previous combat"
            if ran:
                sentence += f" ran {ran} and"
            sentence += " closed"
            if ago:
                sentence += f" {ago} ago"
            lines.append(f"{sentence} in <#{last['channel_id']}>.")
            if last["outcome"]:
                lines.append(f"Outcome: {last['outcome']}")
    lines.extend(_render_open_drops(status["open_drops"]))
    lines.extend(["", _AVRAE_AUTHORITY])
    return fit_discord_lines(lines, label="Loot Drop")


def _render_characters(rows: list[dict]) -> str:
    if not rows:
        return "No characters are registered."
    return fit_discord_lines(
        [f"{row['name']} · `{row['id']}` · {row['lifecycle']}" for row in rows],
        label="character",
    )


def _endpoint_summary(where_ended: str | None, *, limit: int = 90) -> str:
    """The endpoint as one line, for a surface that has room for one line."""
    if not where_ended:
        return "no endpoint recorded"
    text = " ".join(str(where_ended).split())
    return text if len(text) <= limit else clamp_discord_content(text, limit=limit)


def _render_last_time(continuity: dict) -> str:
    """Where the table stopped, and what had happened by then.

    This is the surface the product is named for. Everything on it is already
    written down: the endpoint is the one narrative line End Session asks a DM
    to type, and the recap is the end of that session's ledger read through the
    same renderer the session log uses — so a recap cannot describe an evening
    differently from the log the table watched it in.
    """
    previous = continuity["previous"]
    lines = ["**LAST TIME**", ""]
    if previous is None:
        lines.append(
            "No session has been closed yet, so there is nothing to pick up from. "
            "Ending a session records where you stopped, and it shows up here."
        )
        return "\n".join(lines)
    ended = str(previous["ended_at"] or "")[:10]
    lines.append(f"Session {previous['session_number']}" + (f" · ended {ended}" if ended else ""))
    lines.extend(["", "You ended:", previous["where_ended"] or "Nothing was recorded."])
    # The recording is where the table goes for what a one-line endpoint could
    # not hold. It belongs beside the endpoint rather than in the recap: it is
    # the same answer to "what happened", at a different resolution.
    if previous.get("recording_url"):
        lines.extend(["", f"Recording: {previous['recording_url']}"])
    recap = continuity["recap"]
    if recap:
        lines.extend(["", "What happened:"])
        lines.extend(f"• {line}" for line in recap)
        earlier = int(continuity["recap_total"]) - len(recap)
        if earlier > 0:
            entries = "line" if earlier == 1 else "lines"
            lines.append(
                f"({earlier} earlier {entries} not shown; the export holds the full record.)"
            )
    if continuity["active_session_number"] is not None:
        lines.extend(["", f"Session {continuity['active_session_number']} is in progress now."])
    return fit_discord_lines(lines, label="history")


async def _require_dm(interaction: discord.Interaction, settings: Settings) -> bool:
    """Gate a DM control, and say so in one voice wherever it is pressed.

    The panels only render a DM control for a DM, so this is the second check
    rather than the first — a view outlives the render that built it, and the
    only thing standing between a stale panel and a mutation is the check the
    callback makes when it is pressed.
    """
    if not _in_configured_guild(interaction, settings) or not await _is_dm(interaction, settings):
        await _send_error(interaction, "Only configured DM administrators can use that control.")
        return False
    return True


async def _require_guild(interaction: discord.Interaction, settings: Settings) -> bool:
    if not _in_configured_guild(interaction, settings):
        await _send_error(interaction, "This bot is configured for a different guild.")
        return False
    return True
