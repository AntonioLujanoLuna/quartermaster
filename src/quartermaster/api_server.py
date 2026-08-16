"""Serving the Activity API next to the bot, in one process.

This is a constraint rather than a preference. SQLite has a single writer;
`recover_startup` assumes one runtime releasing one set of projection claims;
`ProjectionRunner` claims targets on the assumption that it is the only
claimant. A second process serving the API would mean two writers, two recovery
paths, and a claim protocol that currently has no reason to be correct across
processes. One asyncio loop, one store, two servers.

Imported lazily by the adapter, because FastAPI and uvicorn are an optional
extra and a table that has not enabled the Activity should not need them
installed to run the bot.
"""

from __future__ import annotations

import asyncio
import logging

from .api_app import create_app
from .api_auth import DiscordIdentityProvider
from .discord_common import Quartermaster

logger = logging.getLogger(__name__)

__all__ = ["serve_api"]


def _split_bind(bind: str) -> tuple[str, int]:
    host, _, port = bind.rpartition(":")
    return host, int(port)


async def serve_api(context: Quartermaster, stop_event: asyncio.Event) -> None:
    """Run the API until the bot's stop event is set.

    Returns rather than raising when the Activity is not configured, so the
    adapter can start this unconditionally and let configuration decide.
    """
    import uvicorn

    settings = context.settings
    if not settings.activity_enabled:
        logger.info("Activity API disabled: configure QM_DISCORD_CLIENT_ID and QM_DISCORD_CLIENT_SECRET")
        return

    client_id, client_secret = settings.require_activity()
    app = create_app(
        context,
        DiscordIdentityProvider(
            client_id=client_id,
            client_secret=client_secret,
            guild_id=settings.guild_id,
        ),
    )
    host, port = _split_bind(settings.api_bind)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))

    async def shutdown() -> None:
        await stop_event.wait()
        server.should_exit = True

    watcher = asyncio.create_task(shutdown())
    logger.info("serving the Activity API on %s", settings.api_bind)
    try:
        await server.serve()
    finally:
        watcher.cancel()
        # The watcher is only ever waiting on the stop event, so cancelling it
        # after the server has already returned is the normal path, not a
        # failure to report.
        try:
            await watcher
        except asyncio.CancelledError:
            pass
