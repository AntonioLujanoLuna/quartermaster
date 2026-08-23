"""Avrae-side wiring notes for Quartermaster's authenticated status adapter.

This extension is still opt-in and is not loaded by the hosted Quartermaster
bot. It intentionally has no Quartermaster SQLite dependency. The reusable
HTTP request verifier lives in ``quartermaster_adapter.py``; loading this Cog
starts the local listener and gives the status provider an Avrae-owned model
context.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import disnake
from cogs5e.initiative.combat import Combat
from cogs5e.initiative.errors import CombatNotFound
from disnake.ext import commands

from quartermaster.integration import ProviderIntegrationError

from .quartermaster_adapter import NativeStatusProvider, QuartermasterStatusAdapter, RequestRejected


class _NativeStatusProvider(NativeStatusProvider):
    """Read native combat through Avrae's existing model-loading seam.

    The synthetic object is used only as the deserialization context required
    by ``Combat.from_id``. It is never passed to a command handler or a
    mutating model method, and the adapter requires the signed actor to be a
    member of the configured guild before loading the combat.
    """

    def __init__(self, bot):
        self.bot = bot

    async def combat_status(self, request):
        guild = self.bot.get_guild(int(request["guild_id"]))
        if guild is None:
            raise RequestRejected("Avrae guild is not available")
        member = guild.get_member(int(request["actor_id"]))
        if member is None:
            raise RequestRejected("Avrae actor is not a member of the configured guild")
        channel = self.bot.get_channel(int(request["channel_id"]))
        channel_guild = getattr(channel, "guild", None)
        if channel is None or channel_guild is None or channel_guild.id != guild.id:
            raise RequestRejected("Avrae channel is not available in the configured guild")

        context = SimpleNamespace(bot=self.bot, author=member, guild=guild, channel=channel)
        try:
            combat = await Combat.from_id(request["channel_id"], context)
        except CombatNotFound:
            return {"active": False, "channel_id": request["channel_id"]}
        return {
            "active": True,
            "channel_id": request["channel_id"],
            "summary_message_id": str(combat.summary_message_id),
        }


class QuartermasterAvraeCog(commands.Cog):
    """Opt-in read-only adapter and local HTTP listener."""

    def __init__(self, bot):
        self.bot = bot
        self.guild_id = os.environ.get("QM_GUILD_ID", "").strip()
        self.secret = os.environ.get("QM_AVRAE_ADAPTER_SECRET", "").strip()
        if not self.guild_id:
            raise RuntimeError("QM_GUILD_ID is required by the Quartermaster Avrae Cog")
        if not self.secret:
            raise RuntimeError("QM_AVRAE_ADAPTER_SECRET is required by the Quartermaster Avrae Cog")
        self.host = os.environ.get("QM_AVRAE_ADAPTER_HOST", "127.0.0.1").strip()
        if self.host not in {"127.0.0.1", "localhost", "::1"} and os.environ.get(
            "QM_AVRAE_ADAPTER_ALLOW_REMOTE", ""
        ).strip() != "1":
            raise RuntimeError(
                "remote Avrae adapter binding requires QM_AVRAE_ADAPTER_ALLOW_REMOTE=1 and TLS at the proxy"
            )
        raw_port = os.environ.get("QM_AVRAE_ADAPTER_PORT", "8787").strip()
        if not raw_port.isdigit() or not 0 < int(raw_port) < 65_536:
            raise RuntimeError("QM_AVRAE_ADAPTER_PORT must be a positive TCP port")
        self.port = int(raw_port)
        self._web_runner = None
        self._web_site = None
        self.status_adapter = QuartermasterStatusAdapter(
            secret=self.secret,
            guild_id=self.guild_id,
            provider=_NativeStatusProvider(bot),
        )

    async def cog_load(self) -> None:
        from aiohttp import web

        application = web.Application(client_max_size=64 * 1024)
        application.router.add_post("/quartermaster/v1/status", self._status_endpoint)
        self._web_runner = web.AppRunner(application, access_log=None)
        await self._web_runner.setup()
        self._web_site = web.TCPSite(self._web_runner, self.host, self.port)
        await self._web_site.start()

    async def cog_unload(self) -> None:
        if self._web_runner is not None:
            await self._web_runner.cleanup()
            self._web_runner = None
            self._web_site = None

    async def handle_status_request(self, body: bytes, headers: dict[str, str]) -> dict:
        """Delegate a POST body from the Cog's chosen HTTP server."""

        return await self.status_adapter.handle(body, headers=headers)

    async def _status_endpoint(self, request):
        from aiohttp import web

        body = await request.read()
        headers = {
            "X-Quartermaster-Protocol": request.headers.get("X-Quartermaster-Protocol", ""),
            "X-Quartermaster-Timestamp": request.headers.get("X-Quartermaster-Timestamp", ""),
            "X-Quartermaster-Nonce": request.headers.get("X-Quartermaster-Nonce", ""),
            "X-Quartermaster-Signature": request.headers.get("X-Quartermaster-Signature", ""),
        }
        try:
            result = await self.handle_status_request(body, headers)
        except ProviderIntegrationError:
            return web.json_response({"status": "FAILED", "error": "request rejected"}, status=401)
        except Exception:
            return web.json_response({"status": "UNKNOWN", "error": "status adapter unavailable"}, status=503)
        return web.json_response(result)

    @commands.slash_command(
        name="qm-combat-probe",
        description="Show the native Avrae combat context needed by Quartermaster",
    )
    async def qm_combat_probe(self, inter: disnake.ApplicationCommandInteraction) -> None:
        if inter.guild is None or str(inter.guild.id) != self.guild_id:
            await inter.response.send_message(
                "Quartermaster combat probes are only enabled in the configured guild.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(
            "The authenticated Quartermaster status listener is loaded. State-changing "
            "operations remain disabled.",
            ephemeral=True,
        )


def setup(bot):
    """Avrae extension entry point."""

    bot.add_cog(QuartermasterAvraeCog(bot))
