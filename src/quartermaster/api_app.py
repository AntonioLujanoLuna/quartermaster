"""The HTTP surface the Activity reads, and the socket that says when to read again.

Stages 1 and 3 of `docs/activity-migration-plan.md`: every read a panel
performs, available over HTTP with the actor derived from a signed token rather
than from anything the client sent, plus `/api/live` — a WebSocket carrying
change notifications keyed on `domain_events.sequence`. No mutations yet —
those are Stage 4, and landing them before the read surface and its
authorization are proven is how the trust boundary gets crossed by accident.

Path operations that touch SQLite are declared `def`, not `async def`, so
FastAPI runs them in a worker thread. The store guards both `read()` and
`transaction()` with the same re-entrant lock and opens its connection with
`check_same_thread=False`, so a threadpool read is serialized against the bot's
writes rather than racing them. An `async def` here would run the read on the
event loop the bot is sharing, and a slow query would stall the bot.

Routes and their dependencies live on a module-level router rather than inside
`create_app`, holding what they need on `app.state`. A closure would read
better, but this module declares `from __future__ import annotations`, so
FastAPI resolves every annotation by name against module globals — and a
dependency alias defined inside the factory is not one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .api_auth import (
    Actor,
    IdentityError,
    IdentityProvider,
    SessionTokens,
    TokenError,
    is_dm,
)
from .api_live import (
    CLOSED,
    EVENTS,
    IDLE,
    REPLAY_LIMIT,
    RESET,
    Change,
    EventFeed,
    Subscription,
    latest_sequence,
    read_changes,
)
from .config import ConfigurationError, Settings
from .db import SCHEMA_VERSION
from .discord_common import Quartermaster
from .export import render_export
from .snapshots import home_snapshot

logger = logging.getLogger(__name__)

__all__ = ["ApiState", "create_app", "router"]


@dataclass(frozen=True)
class ApiState:
    """What every route needs, resolved once at assembly."""

    context: Quartermaster
    identity: IdentityProvider
    tokens: SessionTokens
    feed: EventFeed

    @property
    def settings(self) -> Settings:
        return self.context.settings


class TokenRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)
    instance_id: str | None = Field(default=None, max_length=128)


class TokenResponse(BaseModel):
    """Two tokens with two different audiences.

    `token` is Quartermaster's, signed here, and the only one this API checks.
    `discord_access_token` is Discord's, and exists solely so the client can
    call the Embedded App SDK's `authenticate()` and read the instance roster.
    """

    token: str
    expires_in: int
    actor_id: str
    is_dm: bool
    discord_access_token: str


def _state(request: Request) -> ApiState:
    return request.app.state.quartermaster


State = Annotated[ApiState, Depends(_state)]


def current_actor(state: State, authorization: Annotated[str, Header()] = "") -> Actor:
    """The caller, proved rather than claimed.

    This is the only place an `actor_id` enters the API. Every route reads it
    from here and passes it down; none of them accept one from a body or a
    query string, which is the whole difference between a signed interaction
    and a web page.
    """
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="a bearer session token is required")
    try:
        return state.tokens.verify(token)
    except TokenError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error


CurrentActor = Annotated[Actor, Depends(current_actor)]


def current_dm(actor: CurrentActor) -> Actor:
    """Re-checked per request, which is where the panels check per press.

    The token carries the DM claim it was issued with, so a role revoked
    mid-evening survives until that token expires. That is the same window the
    bot has between rendering a panel and someone pressing it, and it is why
    the service layer checks again inside the transaction rather than trusting
    the surface that called it.
    """
    if not actor.is_dm:
        raise HTTPException(status_code=403, detail="this is a DM control")
    return actor


CurrentDM = Annotated[Actor, Depends(current_dm)]

router = APIRouter(prefix="/api")


# Identity -------------------------------------------------------------------


@router.post("/token", response_model=TokenResponse)
async def issue_token(state: State, request: TokenRequest) -> TokenResponse:
    try:
        confirmed = await state.identity.exchange_code(request.code)
    except IdentityError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    actor = Actor(
        id=confirmed.user_id,
        is_dm=is_dm(confirmed.guild_roles, state.settings.dm_role_ids),
        instance_id=request.instance_id,
    )
    return TokenResponse(
        token=state.tokens.issue(actor),
        expires_in=state.tokens.ttl_seconds,
        actor_id=actor.id,
        is_dm=actor.is_dm,
        discord_access_token=confirmed.access_token,
    )


@router.get("/me")
def me(actor: CurrentActor) -> dict[str, Any]:
    return {"actor_id": actor.id, "is_dm": actor.is_dm, "instance_id": actor.instance_id}


# Reads ----------------------------------------------------------------------


@router.get("/home")
def home(state: State, actor: CurrentActor) -> dict[str, Any]:
    context = state.context
    return home_snapshot(
        inventory=context.inventory,
        loot=context.loot,
        characters=context.characters,
        currency=context.currency,
        sessions=context.sessions,
        actor_id=actor.id,
    )


@router.get("/stash")
def stash(state: State, _: CurrentActor) -> dict[str, Any]:
    # No limit. The panel's twenty-five was a component budget, and this
    # surface scrolls.
    items = state.context.inventory.browse()
    return {"items": items, "total": len(items)}


@router.get("/me/items")
def my_items(state: State, actor: CurrentActor, limit: int = 200) -> dict[str, Any]:
    return state.context.inventory.holdings(actor_id=actor.id, limit=_bounded(limit))


@router.get("/loot")
def loot(state: State, _: CurrentActor) -> dict[str, Any]:
    drops = state.context.loot.list_open()
    return {"drops": drops, "total": len(drops)}


@router.get("/loot/claimable")
def claimable(state: State, actor: CurrentActor, limit: int = 200) -> dict[str, Any]:
    return state.context.loot.prepare_claim_view(actor_id=actor.id, limit=_bounded(limit))


@router.get("/treasury")
def treasury(state: State, actor: CurrentActor) -> dict[str, Any]:
    currency = state.context.currency
    return {"treasury": currency.view_treasury(), "purse": currency.purse(actor_id=actor.id)}


@router.get("/characters")
def characters(state: State, _: CurrentActor) -> dict[str, Any]:
    roster = state.context.characters.list_characters()
    return {"characters": roster, "total": len(roster)}


@router.get("/combat")
def combat(state: State, _: CurrentActor) -> dict[str, Any]:
    return state.context.combat.status()


@router.get("/session/continuity")
def continuity(state: State, _: CurrentActor, limit: int = 20) -> dict[str, Any]:
    return state.context.sessions.continuity(limit=_bounded(limit))


@router.get("/export")
def export(state: State, _: CurrentDM) -> dict[str, Any]:
    return {"export": render_export(state.context.store)}


# Live feed ------------------------------------------------------------------

#: How long a client has to present its token after the socket opens.
HANDSHAKE_SECONDS = 10.0

#: How long a quiet socket waits before saying something. Discord's proxy and
#: any intermediary will close a connection that carries nothing for long
#: enough, and a heartbeat is also how this end notices a client that vanished
#: without closing.
HEARTBEAT_SECONDS = 25.0

#: Close codes in the private range. 1008 would say "policy violation" without
#: saying which, and the client behaves differently for an expired token — it
#: goes and gets another one — than for anything else.
UNAUTHORIZED = 4401
UNAVAILABLE = 4503


@router.websocket("/live")
async def live(websocket: WebSocket, since: int | None = None) -> None:
    """Change notifications, from a cursor.

    `since` is the last sequence the client was told about. Omitted means "I am
    about to read everything anyway" — the client connects first and fetches
    second, so anything committed after the connection is reported and anything
    before it is in the fetch. Reconnecting with a cursor turns a dropped socket
    into a gap to fill rather than a screen to rebuild.
    """
    state: ApiState = websocket.app.state.quartermaster
    await websocket.accept()

    actor = await _authenticate_socket(websocket, state.tokens)
    if actor is None:
        return
    if not state.feed.running:
        # A socket that connects and then silently never delivers is worse than
        # one that refuses: the screen would look live and be frozen.
        await websocket.close(code=UNAVAILABLE, reason="the live feed is not running")
        return

    # Subscribed before the replay is read, so a change that lands between the
    # two is delivered rather than falling into the gap between them. It may be
    # delivered twice; a duplicate costs a refetch and a gap costs correctness.
    subscription = state.feed.subscribe()
    reader: asyncio.Task[None] | None = None
    try:
        store = state.context.store
        head = await asyncio.to_thread(latest_sequence, store)
        await websocket.send_json(
            {"type": "hello", "actor_id": actor.id, "is_dm": actor.is_dm, "sequence": head}
        )
        if since is not None and since < head:
            replay = await asyncio.to_thread(read_changes, store, since, REPLAY_LIMIT)
            if len(replay) >= REPLAY_LIMIT:
                await websocket.send_json({"type": "reset", "sequence": head})
            elif replay:
                await _send_changes(websocket, replay)

        reader = asyncio.create_task(_discard_client_messages(websocket, subscription))
        while True:
            kind, changes = await subscription.next(timeout=HEARTBEAT_SECONDS)
            if kind == CLOSED:
                return
            if kind == EVENTS:
                await _send_changes(websocket, changes)
            elif kind == RESET:
                await websocket.send_json({"type": "reset", "sequence": state.feed.sequence})
            elif kind == IDLE:
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # Starlette raises this for a send after the client went away, which is
        # a disconnect noticed from the wrong end rather than a fault.
        logger.debug("live socket for %s ended mid-send", actor.id)
    finally:
        state.feed.release(subscription)
        if reader is not None:
            reader.cancel()


async def _send_changes(websocket: WebSocket, changes: Sequence[Change]) -> None:
    """A batch of notifications, with no payloads in it.

    The client refetches the reads it has on screen. Sending the payload would
    make this a second rendering of state that the reads already answer for,
    and a second thing to keep true.
    """
    await websocket.send_json(
        {
            "type": "events",
            "events": [change.as_dict() for change in changes],
            "sequence": changes[-1].sequence,
        }
    )


async def _authenticate_socket(websocket: WebSocket, tokens: SessionTokens) -> Actor | None:
    """The same token the reads carry, presented as the first frame.

    A browser cannot set an `Authorization` header on a WebSocket, and the
    remaining choices are a query string or a message. A query string is a
    bearer credential in every access log and proxy log between here and the
    player, so it is the message.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=HANDSHAKE_SECONDS)
    except TimeoutError:
        await websocket.close(code=UNAUTHORIZED, reason="no session token was presented")
        return None
    except WebSocketDisconnect:
        return None
    try:
        opening = json.loads(raw)
    except ValueError:
        await websocket.close(code=UNAUTHORIZED, reason="the opening frame was not JSON")
        return None
    presented = opening.get("token") if isinstance(opening, dict) else None
    try:
        return tokens.verify(presented if isinstance(presented, str) else "")
    except TokenError as error:
        await websocket.close(code=UNAUTHORIZED, reason=str(error)[:120])
        return None


async def _discard_client_messages(websocket: WebSocket, subscription: Subscription) -> None:
    """Read and throw away whatever the client sends after the token.

    Nothing is accepted from a client on this socket — a mutation is a request
    with an idempotency key, not a frame on a feed. This exists so a client
    going away is noticed at once rather than at the next heartbeat, and so its
    frames do not accumulate unread.
    """
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        subscription.disconnect()


# Operations -----------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated on purpose: a load balancer has no session token.

    It states liveness and schema version and nothing about the campaign.
    """
    return {"status": "ok", "schema_version": SCHEMA_VERSION}


def _bounded(limit: int, *, ceiling: int = 500) -> int:
    """Clamp a client-supplied page size.

    The services raise on a non-positive limit and will read whatever large
    number they are handed, so the bound belongs at the edge the number
    arrives from.
    """
    if limit < 1:
        raise HTTPException(status_code=422, detail="limit must be positive")
    return min(limit, ceiling)


def create_app(
    context: Quartermaster,
    identity: IdentityProvider,
    *,
    tokens: SessionTokens | None = None,
) -> FastAPI:
    """Build the Activity API over an already-assembled adapter context.

    Takes `Quartermaster` rather than `BotServices` because its docstring
    describes exactly the bug a second resolution of the optional services
    would reintroduce: two surfaces holding different `LootDropService`
    instances. The adapter fills those gaps once; this reads the result.
    """
    settings = context.settings
    if tokens is None:
        _, client_secret = settings.require_activity()
        tokens = SessionTokens(client_secret, ttl_seconds=settings.session_token_seconds)

    feed = EventFeed(context.store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The pump is started here rather than at import or on first connection
        # so that it is bound to the loop that serves the app, and so that
        # shutdown takes the commit listener off the store before the bot
        # closes it.
        await feed.start()
        try:
            yield
        finally:
            await feed.stop()

    app = FastAPI(
        title="Quartermaster",
        version=str(SCHEMA_VERSION),
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.quartermaster = ApiState(context=context, identity=identity, tokens=tokens, feed=feed)

    if settings.activity_origin:
        # The Activity is normally served same-origin through Discord's proxy,
        # so this exists for the split-origin case — a Vite dev server in
        # Stage 2, or a frontend hosted apart from the API. Never a wildcard:
        # the session token is a bearer credential.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[settings.activity_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )

    app.include_router(router)

    if settings.activity_dist is not None:
        # Serving the built page from the same origin as the API is what makes
        # one URL mapping enough, and it keeps the client's fetches
        # same-origin rather than relying on the CORS branch above. Mounted
        # after the router, so `/api/...` never resolves to a file.
        from fastapi.staticfiles import StaticFiles

        distribution = settings.activity_dist.expanduser()
        if not distribution.is_dir():
            raise ConfigurationError(f"QM_ACTIVITY_DIST is not a directory: {distribution}")
        app.mount("/", StaticFiles(directory=str(distribution), html=True), name="activity")

    return app
