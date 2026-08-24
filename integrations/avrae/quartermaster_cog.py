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
from cogs5e.utils import actionutils, checkutils
from cogs5e.utils.actionutils import run_attack
from disnake.ext import commands
from gamedata import lookuputils
from utils import constants
from utils.argparser import argparse
from utils.functions import camel_to_title
from utils.settings import ServerSettings

from quartermaster.integration import ProviderIntegrationError

from .quartermaster_adapter import (
    NativeOperationProvider,
    NativeStatusProvider,
    QuartermasterOperationAdapter,
    QuartermasterStatusAdapter,
    RequestRejected,
)


class _NativeStatusProvider(NativeStatusProvider):
    """Read native combat through Avrae's existing model-loading seam.

    The synthetic object is used only as the deserialization context required
    by ``Combat.from_id``. It is never passed to a command handler or a
    mutating model method, and the adapter requires the signed actor to be a
    member of the configured guild before loading the combat.
    """

    def __init__(self, bot):
        self.bot = bot

    async def _authorized_context(self, request):
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

        context = _NativeExecutionContext(bot=self.bot, author=member, guild=guild, channel=channel)
        return guild, member, context

    async def combat_status(self, request):
        _guild, _member, context = await self._authorized_context(request)
        try:
            combat = await Combat.from_id(request["channel_id"], context)
        except CombatNotFound:
            return {"active": False, "channel_id": request["channel_id"]}
        return {
            "active": True,
            "channel_id": request["channel_id"],
            "summary_message_id": str(combat.summary_message_id),
        }


class _NativeOperationProvider(_NativeStatusProvider, NativeOperationProvider):
    """Execute only the bounded operations approved by the adapter contract."""

    async def execute_operation(self, request):
        guild, member, context = await self._authorized_context(request)
        try:
            combat = await Combat.from_id(request["channel_id"], context)
        except CombatNotFound as error:
            raise RequestRejected("Avrae combat is not active in this channel") from error

        if len(combat.get_combatants()) == 0:
            raise RequestRejected("Avrae combat has no combatants")

        server_settings = await ServerSettings.for_guild(self.bot.mdb, guild.id)
        actor_id = int(request["actor_id"])
        current = combat.current_combatant
        self._require_current_actor(
            actor_id=actor_id,
            member=member,
            combat=combat,
            current=current,
            server_settings=server_settings,
        )

        if request["operation_kind"] == "next":
            advanced_round, messages = combat.advance_turn()
            await combat.final(context)
            current = combat.current_combatant
            return {
                "operation": "next",
                "advanced_round": advanced_round,
                "round": combat.round_num,
                "turn": combat.turn_num,
                "current_combatant": getattr(current, "name", None),
                "notices": messages,
            }
        if request["operation_kind"] == "attack":
            return await self._attack(request, context, combat, current)
        if request["operation_kind"] == "check":
            return await self._check(request, context, current)
        if request["operation_kind"] == "save":
            return await self._save(request, context, current)
        if request["operation_kind"] == "cast":
            return await self._cast(request, context, combat, current)
        raise RequestRejected("Avrae operation is not enabled")

    @staticmethod
    def _require_current_actor(*, actor_id, member, combat, current, server_settings):
        if current is None:
            raise RequestRejected("Avrae combat has no active turn")
        allowed_to_act = (
            actor_id == current.controller_id
            or actor_id == combat.dm_id
            or (server_settings is not None and server_settings.is_dm(member))
        )
        if not allowed_to_act:
            raise RequestRejected("Avrae refused this operation for the actor")

    async def _attack(self, request, context, combat, attacker):
        payload = request.get("payload", {})
        attack_name = _bounded_text(payload, "attack", 120)
        target_name = _bounded_text(payload, "target", 120)
        args_text = payload.get("args", "")
        if not isinstance(args_text, str) or len(args_text) > 500:
            raise RequestRejected("Avrae attack arguments are invalid")

        target = combat.combatant_by_id(target_name) or combat.get_combatant(target_name, strict=True)
        if target is None:
            raise RequestRejected("Avrae attack target was not found")

        attacks = list(attacker.attacks)
        exact = [attack for attack in attacks if attack.name.casefold() == attack_name.casefold()]
        if len(exact) != 1:
            partial = [attack for attack in attacks if attack_name.casefold() in attack.name.casefold()]
            if len(partial) != 1:
                raise RequestRejected("Avrae attack name must select exactly one native attack")
            attack = partial[0]
        else:
            attack = exact[0]
        if attack.automation is None:
            raise RequestRejected("Avrae attack has no native automation")

        embed = disnake.Embed(color=attacker.get_color())
        await run_attack(context, embed, argparse(args_text), attacker, attack, [target], combat)
        return {
            "operation": "attack",
            "attacker": attacker.name,
            "attack": attack.name,
            "target": target.name,
            "embed": embed.to_dict(),
        }

    @staticmethod
    async def _check(request, context, caster):
        payload = request.get("payload", {})
        skill_key = _resolve_skill(_bounded_text(payload, "skill", 120))
        args_text = payload.get("args", "")
        if not isinstance(args_text, str) or len(args_text) > 500:
            raise RequestRejected("Avrae check arguments are invalid")

        embed = disnake.Embed(color=caster.get_color())
        result = checkutils.run_check(skill_key, caster, argparse(args_text), embed)
        return {
            "operation": "check",
            "actor": caster.name,
            "skill": result.skill_name,
            "embed": embed.to_dict(),
        }

    @staticmethod
    async def _save(request, context, caster):
        payload = request.get("payload", {})
        save_key = _resolve_save(_bounded_text(payload, "save", 120))
        args_text = payload.get("args", "")
        if not isinstance(args_text, str) or len(args_text) > 500:
            raise RequestRejected("Avrae save arguments are invalid")

        embed = disnake.Embed(color=caster.get_color())
        result = checkutils.run_save(save_key, caster, argparse(args_text), embed)
        return {
            "operation": "save",
            "actor": caster.name,
            "save": result.skill_name,
            "embed": embed.to_dict(),
        }

    @staticmethod
    async def _cast(request, context, combat, caster):
        payload = request.get("payload", {})
        spell_name = _bounded_text(payload, "spell", 120)
        target_name = _bounded_text(payload, "target", 120)
        args_text = payload.get("args", "")
        if not isinstance(args_text, str) or len(args_text) > 500:
            raise RequestRejected("Avrae spell arguments are invalid")

        args = argparse(args_text)
        if args.last("i", type_=bool):
            raise RequestRejected("Quartermaster casts may not ignore Avrae resource restrictions")
        spellbook_spell_choices = {
            spell.name.casefold() for spell in getattr(caster.spellbook, "spells", ())
        }
        choices = await lookuputils.get_spell_choices(context)
        choices = await lookuputils.filter_spells_by_version(context, choices)
        known_spells = [spell for spell in choices if spell.name.casefold() in spellbook_spell_choices]
        spell = _resolve_spell(spell_name, known_spells)
        spellbook_spell = caster.spellbook.get_spell(spell)
        if spellbook_spell is None:
            raise RequestRejected("Avrae spell is not in the caster's spellbook")
        if not spellbook_spell.prepared:
            raise RequestRejected("Avrae spell is not prepared")

        target = combat.combatant_by_id(target_name) or combat.get_combatant(target_name, strict=True)
        if target is None:
            raise RequestRejected("Avrae spell target was not found")

        result = await actionutils.cast_spell(spell, context, caster, [target], args, combat=combat)
        if not result.success:
            raise RequestRejected("Avrae rejected the spell cast")
        return {
            "operation": "cast",
            "caster": caster.name,
            "spell": spell.name,
            "target": target.name,
            "embed": result.embed.to_dict(),
        }


class _NativeExecutionContext(SimpleNamespace):
    """Minimal native context for model/automation calls made by the adapter."""

    nlp_caster = None
    nlp_targets = None
    prefix = "!"

    async def get_server_settings(self):
        return await ServerSettings.for_guild(self.bot.mdb, self.guild.id)

    async def trigger_typing(self):
        return None


def _bounded_text(payload, key, maximum):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RequestRejected(f"Avrae operation field {key} is invalid")
    return value.strip()


def _resolve_skill(value):
    normalized = value.casefold()
    exact = [
        skill
        for skill in constants.SKILL_NAMES
        if skill.casefold() == normalized or camel_to_title(skill).casefold() == normalized
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        skill
        for skill in constants.SKILL_NAMES
        if normalized in skill.casefold() or normalized in camel_to_title(skill).casefold()
    ]
    if len(partial) != 1:
        raise RequestRejected("Avrae check skill must select exactly one native skill")
    return partial[0]


def _resolve_save(value):
    normalized = value.casefold()
    exact = [
        save
        for save in constants.SAVE_NAMES
        if save.casefold() == normalized or camel_to_title(save).casefold() == normalized
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        save
        for save in constants.SAVE_NAMES
        if normalized in save.casefold() or normalized in camel_to_title(save).casefold()
    ]
    if len(partial) != 1:
        raise RequestRejected("Avrae save must select exactly one native saving throw")
    return partial[0]


def _resolve_spell(value, choices):
    normalized = value.casefold()
    exact = [spell for spell in choices if spell.name.casefold() == normalized]
    if len(exact) == 1:
        return exact[0]
    partial = [spell for spell in choices if normalized in spell.name.casefold()]
    if len(partial) != 1:
        raise RequestRejected("Avrae spell must select exactly one known native spell")
    return partial[0]


class QuartermasterAvraeCog(commands.Cog):
    """Opt-in status/operation adapter and local HTTP listener."""

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
        self.operation_adapter = QuartermasterOperationAdapter(
            secret=self.secret,
            guild_id=self.guild_id,
            provider=_NativeOperationProvider(bot),
        )

    async def cog_load(self) -> None:
        from aiohttp import web

        application = web.Application(client_max_size=64 * 1024)
        application.router.add_post("/quartermaster/v1/status", self._status_endpoint)
        application.router.add_post("/quartermaster/v1/operation", self._operation_endpoint)
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

    async def handle_operation_request(self, body: bytes, headers: dict[str, str]) -> dict:
        """Delegate one bounded state-changing request to the native adapter."""

        return await self.operation_adapter.handle(body, headers=headers)

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

    async def _operation_endpoint(self, request):
        from aiohttp import web

        body = await request.read()
        headers = {
            "X-Quartermaster-Protocol": request.headers.get("X-Quartermaster-Protocol", ""),
            "X-Quartermaster-Timestamp": request.headers.get("X-Quartermaster-Timestamp", ""),
            "X-Quartermaster-Nonce": request.headers.get("X-Quartermaster-Nonce", ""),
            "X-Quartermaster-Signature": request.headers.get("X-Quartermaster-Signature", ""),
        }
        try:
            result = await self.handle_operation_request(body, headers)
        except ProviderIntegrationError:
            return web.json_response({"status": "FAILED", "error": "request rejected"}, status=401)
        except Exception:
            return web.json_response({"status": "UNKNOWN", "error": "operation adapter unavailable"}, status=503)
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
            "The authenticated Quartermaster Avrae listener is loaded. Turn advance, bounded "
            "native attacks, checks, saves, and bounded spell casts are enabled; other mechanics remain disabled.",
            ephemeral=True,
        )


def setup(bot):
    """Avrae extension entry point."""

    bot.add_cog(QuartermasterAvraeCog(bot))
