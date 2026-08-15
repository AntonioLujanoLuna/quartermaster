"""The one guild-scoped command Quartermaster registers.

Everything Quartermaster does used to be a command with typed arguments: an
item name spelled the way the DM remembered it, a quantity, a thirty-six
character character ID pasted from `/characters`. That surface put the burden
of recall on the table — you had to know a command existed, what it was called,
and what order its arguments went in, at the moment you wanted to use it.

`/quartermaster` opens the panel instead. Discord needs one command to give a
player a way in; the panel gives them the way around, and every choice past the
entry point is something already on screen to press.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .discord_common import Quartermaster
from .discord_panels import open_home


def register_commands(bot: commands.Bot, guild: discord.Object, context: Quartermaster) -> None:
    """Register the Quartermaster entry point on the given bot tree."""

    @bot.tree.command(name="quartermaster", description="Open Quartermaster")
    @app_commands.guilds(guild)
    async def quartermaster(interaction: discord.Interaction) -> None:
        await open_home(interaction, context)
