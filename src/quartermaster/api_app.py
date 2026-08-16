"""The HTTP surface the Activity reads.

Stage 1 of `docs/activity-migration-plan.md`: every read a panel performs,
available over HTTP, with the actor derived from a signed token rather than
from anything the client sent. No mutations yet — those are Stage 4, and
landing them before the read surface and its authorization are proven is how
the trust boundary gets crossed by accident.

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

import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
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
from .config import Settings
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

    @property
    def settings(self) -> Settings:
        return self.context.settings


class TokenRequest(BaseModel):
    code: str = Field(min_length=1, max_length=512)
    instance_id: str | None = Field(default=None, max_length=128)


class TokenResponse(BaseModel):
    token: str
    expires_in: int
    actor_id: str
    is_dm: bool


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

    app = FastAPI(title="Quartermaster", version=str(SCHEMA_VERSION), docs_url=None, redoc_url=None)
    app.state.quartermaster = ApiState(context=context, identity=identity, tokens=tokens)

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
    return app
