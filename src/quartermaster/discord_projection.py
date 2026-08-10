"""Async discord.py transport and background projection runner."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import discord

from .config import Settings
from .db import SQLiteStore
from .loot import expire_due_drops
from .projections import EventOutboxWorker, StateProjectionScheduler
from .transport import RateLimitedError


class ProjectionConfigurationError(RuntimeError):
    """Raised when a projection destination has no configured Discord channel."""


def _content_for_state(target_id: str, payload: dict[str, Any]) -> str:
    if target_id == "party-stash":
        lines = ["**PARTY STASH**", ""]
        items = payload.get("items", [])
        for drop in payload.get("loot_drops", []):
            lines.extend(["", f"**NEW LOOT** · `{drop['drop_id'][:8]}`"])
            lines.extend(f"- {item['item_name']} x{item['remaining']}" for item in drop["items"])
        lines.extend(f"• {item['item_name']} x{item['quantity']}" for item in items)
        return "\n".join(lines) if items or payload.get("loot_drops") else "\n".join(lines + ["Nothing is recorded yet."])
    if target_id == "session-surface":
        active = payload.get("active")
        previous = payload.get("previous")
        lines = ["**SESSION**", ""]
        lines.append(f"Active session: {active['session_number']}" if active else "No active session.")
        if previous:
            lines.append(f"Previous endpoint: {previous.get('where_ended') or 'Not recorded'}")
        return "\n".join(lines)
    if target_id == "dm-surface":
        return f"**QUARTERMASTER**\n\nActive sessions: {payload['active_session_count']}\nStash entries: {payload['stash_count']}"
    raise ProjectionConfigurationError(f"unknown state target {target_id}")


def _content_for_event(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "ITEM_GRANTED":
        return f"DM added {payload['quantity']} {payload['item_name']}."
    if event_type == "ITEM_TAKEN":
        return f"A player took {payload['quantity']} {payload['item_name']}. {payload['remaining']} remain."
    if event_type == "SESSION_STARTED":
        return f"Session {payload['session_number']} started."
    if event_type == "SESSION_CLOSED":
        return f"Session {payload['session_number']} closed."
    if event_type == "LOOT_DROP_CREATED":
        return f"New Loot Drop created ({len(payload['items'])} item entries)."
    if event_type == "LOOT_CLAIMED":
        return f"A player claimed {payload['quantity']} {payload['item_name']} from a Loot Drop."
    if event_type == "LOOT_DROP_CLOSED":
        return f"Loot Drop closed ({payload['reason']})."
    return f"{event_type}: {json.dumps(payload, sort_keys=True)}"


class DiscordProjectionTransport:
    def __init__(self, bot: discord.Client, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings

    def _channel_id_for(self, destination: str) -> str:
        if destination == "party-inventory":
            channel_id = self.settings.party_inventory_channel_id
        elif destination == "dm-surface":
            channel_id = self.settings.dm_channel_id
        elif destination.startswith("session:"):
            channel_id = self.settings.session_log_channel_id
        else:
            channel_id = None
        if channel_id is None:
            raise ProjectionConfigurationError(f"no Discord channel configured for {destination}")
        return channel_id

    async def _fetch_channel(self, destination: str) -> Any:
        try:
            return await self.bot.fetch_channel(int(self._channel_id_for(destination)))
        except discord.HTTPException as error:
            if error.status == 429:
                raise RateLimitedError(getattr(error, "retry_after", 1.0)) from error
            raise

    async def upsert_state(self, target_id: str, destination: str, payload: dict[str, Any], message_id: str | None) -> str:
        channel = await self._fetch_channel(destination)
        content = _content_for_state(target_id, payload)
        try:
            if message_id is not None:
                message = await channel.fetch_message(int(message_id))
                await message.edit(content=content)
                return message_id
            message = await channel.send(content)
            return str(message.id)
        except discord.NotFound:
            message = await channel.send(content)
            return str(message.id)
        except discord.HTTPException as error:
            if error.status == 429:
                raise RateLimitedError(getattr(error, "retry_after", 1.0)) from error
            raise

    async def deliver_event(self, destination: str, event_type: str, payload: dict[str, Any]) -> None:
        channel = await self._fetch_channel(destination)
        try:
            await channel.send(_content_for_event(event_type, payload))
        except discord.HTTPException as error:
            if error.status == 429:
                raise RateLimitedError(getattr(error, "retry_after", 1.0)) from error
            raise


class ProjectionRunner:
    def __init__(self, store: SQLiteStore, transport: DiscordProjectionTransport) -> None:
        self.store = store
        self.transport = transport
        self.state = StateProjectionScheduler(store, transport)
        self.events = EventOutboxWorker(store, transport)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            expire_due_drops(self.store)
            delivered_state = await self.state.run_once_async(self.transport)
            delivered_event = await self.events.run_once_async(self.transport)
            if not delivered_state and not delivered_event:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
