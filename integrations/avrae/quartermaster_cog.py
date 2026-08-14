"""Disposable Avrae-side context probe for the Quartermaster integration spike.

Install this Cog only in a self-hosted/forked Avrae process. It deliberately
uses Avrae's native interaction and Combat.from_ctx path. It records a
correlation receipt in Quartermaster, but does not execute or reproduce any
combat mechanic yet.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import disnake
from cogs5e.initiative import Combat, CombatNotFound
from disnake.ext import commands

from quartermaster.db import SQLiteStore
from quartermaster.integration import AvraeInteractionContext, ProviderIntegrationService, ProviderResult
from quartermaster.receipts import ReceiptRepository


class QuartermasterAvraeCog(commands.Cog):
    """Proves actor/channel/combat context handoff without mechanic execution."""

    def __init__(self, bot, *, database_path: str | Path | None = None):
        self.bot = bot
        self.guild_id = os.environ.get("QM_GUILD_ID")
        if not self.guild_id:
            raise RuntimeError("QM_GUILD_ID is required by the Quartermaster Avrae Cog")
        configured_path = database_path or os.environ.get("QM_DATABASE_PATH")
        if not configured_path:
            raise RuntimeError("QM_DATABASE_PATH is required by the Quartermaster Avrae Cog")
        self.store = SQLiteStore(configured_path).open()
        self.receipts = ReceiptRepository(self.store)
        self.integration = ProviderIntegrationService(
            self.store,
            self.receipts,
            integration_version="avrae-cog-context-probe-v1",
        )

    def cog_unload(self):
        self.store.close()

    @commands.slash_command(
        name="qm-combat-probe",
        description="Probe the native Avrae combat context for Quartermaster",
    )
    async def qm_combat_probe(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if inter.guild is None:
            await inter.response.send_message("Quartermaster combat probes are guild-only.", ephemeral=True)
            return
        if str(inter.guild.id) != self.guild_id:
            await inter.response.send_message("Quartermaster combat probes are not enabled in this guild.", ephemeral=True)
            return
        session = await asyncio.to_thread(self._active_session)
        if session is None:
            await inter.response.send_message("No active Quartermaster session.", ephemeral=True)
            return

        await inter.response.defer(ephemeral=True)
        context = AvraeInteractionContext.from_interaction(inter, session_id=session["id"])
        execution = await asyncio.to_thread(
            self.integration.begin,
            str(inter.id),
            **context.begin_kwargs(),
            operation_kind="status",
            payload={"source": "avrae-cog-context-probe"},
        )
        if execution.receipt.status != "PROCESSING":
            await inter.followup.send(self._render_receipt(execution.receipt.logical_response), ephemeral=True)
            return

        try:
            combat = await Combat.from_ctx(inter)
            provider_result = ProviderResult(
                status="COMMITTED",
                provider_reference=context.provider_reference,
                provider_version="avrae-native-context",
                payload={
                    "active": True,
                    "channel_id": context.channel_id,
                    "summary_message_id": str(combat.summary_message_id),
                },
            )
        except CombatNotFound:
            provider_result = ProviderResult(
                status="COMMITTED",
                provider_reference=context.provider_reference,
                provider_version="avrae-native-context",
                payload={"active": False, "channel_id": context.channel_id},
            )
        except Exception as error:
            result = await asyncio.to_thread(self.integration.unknown, execution, str(error))
            await inter.followup.send(self._render_receipt(result.logical_response), ephemeral=True)
            return

        result = await asyncio.to_thread(self.integration.committed, execution, provider_result)
        await inter.followup.send(self._render_receipt(result.logical_response), ephemeral=True)

    def _active_session(self) -> dict[str, str] | None:
        with self.store.transaction(immediate=False) as connection:
            row = connection.execute(
                "SELECT id FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
        return {"id": str(row["id"])} if row is not None else None

    @staticmethod
    def _render_receipt(logical_response: dict) -> str:
        result = logical_response.get("result") or {}
        active = result.get("active")
        active_text = "active" if active else "not active"
        return (
            f"Quartermaster correlation `{logical_response.get('correlation_id', '?')}`. "
            f"Native Avrae combat is {active_text}."
        )


def setup(bot):
    """Avrae extension entry point."""
    bot.add_cog(QuartermasterAvraeCog(bot))
