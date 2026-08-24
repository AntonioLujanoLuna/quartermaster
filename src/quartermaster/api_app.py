"""The HTTP surface the Activity reads and writes, and the socket that says when
to read again.

Stages 1, 3, 4, and 5 of `docs/activity-migration-plan.md`: every read a panel
performs, available over HTTP with the actor derived from a signed token rather
than from anything the client sent; `/api/live`, a WebSocket carrying change
notifications keyed on `domain_events.sequence`; the mutations a player
performs at the table, each keyed by an idempotency key the client generates
and scoped to the actor that proved who they are; and the ones a DM performs,
which are the same shape with the authority checked twice.

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
import string
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

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
from .characters import CharacterError
from .combat import CombatError
from .config import ConfigurationError, Settings
from .currency import CurrencyError, CurrencySemanticStaleness
from .db import SCHEMA_VERSION
from .dice import DiceRollError, DiceService
from .discord_common import Quartermaster
from .dossiers import CharacterDossierService, DossierError
from .export import render_export
from .handles import HandleError
from .integration import (
    ProviderIntegrationError,
    ProviderIntegrationService,
    ProviderRequest,
    ProviderResult,
    ProviderTimeout,
)
from .inventory import InventoryError, SemanticStaleness
from .loot import LootDropError
from .operations import (
    create_scheduled_backup,
    health_report,
    render_health,
    run_maintenance,
)
from .receipts import ReceiptError, ReceiptResult
from .sessions import SessionError
from .snapshots import home_snapshot

logger = logging.getLogger(__name__)

__all__ = ["PROXY_PREFIX", "ApiState", "create_app", "router"]


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


def _dice_service(context: Quartermaster) -> DiceService:
    """Use the assembled service, while keeping older test contexts valid."""

    return context.dice or DiceService(context.store, context.services.receipts)


def _dossier_service(context: Quartermaster) -> CharacterDossierService:
    """Use the assembled dossier service while keeping older test contexts valid."""

    return context.dossiers or CharacterDossierService(context.store, context.services.receipts)


def _provider_operations(context: Quartermaster) -> ProviderIntegrationService:
    """Use the assembled provider service while keeping older test contexts valid."""

    return context.provider_operations or ProviderIntegrationService(context.store, context.services.receipts)


#: What a client-generated idempotency key may look like. A Discord interaction
#: id was a number nobody could choose; this is a string a browser picks, and it
#: becomes the primary key of a receipt row, so it is bounded here rather than
#: wherever it lands.
#:
#: The separator this namespaces with is deliberately not in the alphabet, so
#: no key can reach into another actor's namespace by containing one.
IDEMPOTENCY_KEY_LIMIT = 100
IDEMPOTENCY_KEY_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-_.")


def action_id(actor: CurrentActor, idempotency_key: Annotated[str, Header()] = "") -> str:
    """The receipt key for one action at the table, scoped to who took it.

    `ReceiptRepository` keys on an interaction id, and a replayed key returns
    the receipt already stored under it rather than running the mutation again.
    That is what makes a retry safe over a flaky socket, and it is why the key
    cannot be taken at face value now that a client chooses it: an unscoped key
    would let one player quote another's and be handed their result.

    So the key the receipts table sees is this actor's key. Two players who
    generate the same UUID — or one who copies another's on purpose — hold two
    different receipts, and a retry still finds its own.

    The client generates it when the player acts, not per request, which is the
    difference between retrying an action and performing a second one.
    """
    key = idempotency_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="an Idempotency-Key header is required")
    if len(key) > IDEMPOTENCY_KEY_LIMIT or not set(key) <= IDEMPOTENCY_KEY_CHARACTERS:
        raise HTTPException(status_code=400, detail="that Idempotency-Key is not a usable key")
    return f"activity:{actor.id}:{key}"


Idempotency = Annotated[str, Depends(action_id)]

#: The prefix Discord's proxy puts in front of everything the client asks for.
#:
#: The frontend addresses `/.proxy/api/...` because that form is carried by
#: every version of the proxy; the proxy forwards it to `/api/...` here. The
#: API answers on both so that neither end has to be right about the other —
#: see the second `include_router` in `create_app`.
PROXY_PREFIX = "/.proxy"

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
        is_dm=confirmed.is_owner or is_dm(confirmed.guild_roles, state.settings.dm_role_ids),
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


@router.get("/me/dossier")
def my_dossier(state: State, actor: CurrentActor) -> dict[str, Any]:
    """Read the caller's imported snapshot, never a client-supplied sheet."""

    return _dossier_service(state.context).read_for_actor(actor.id)


@router.get("/combat")
def combat(state: State, _: CurrentActor) -> dict[str, Any]:
    return state.context.combat.status()


@router.get("/combat/avrae")
def avrae_combat(state: State, actor: CurrentActor) -> dict[str, Any]:
    """Read Avrae status through the optional authenticated adapter.

    Quartermaster's own combat record gates this call and supplies the
    session/channel context. No Avrae state is copied into SQLite, and no
    provider receipt is created because this first adapter operation is a
    read. A timeout is returned as ``UNKNOWN`` so the client cannot mistake a
    missing response for an inactive combat.
    """

    quartermaster_status = state.context.combat.status()
    encounter = quartermaster_status.get("encounter")
    if quartermaster_status.get("status") != "OPEN" or not isinstance(encounter, dict):
        return {
            "status": "NOT_QUERIED",
            "provider": "avrae",
            "quartermaster": quartermaster_status,
            "result": None,
        }

    gateway = state.context.avrae_gateway
    if gateway is None:
        return {
            "status": "NOT_CONFIGURED",
            "provider": "avrae",
            "quartermaster": quartermaster_status,
            "result": None,
            "error": "the Avrae status adapter is not configured",
        }

    channel_id = str(encounter["channel_id"])
    request = ProviderRequest(
        operation_id=f"status:{uuid4().hex}",
        provider="avrae",
        operation_kind="status",
        actor_id=actor.id,
        guild_id=state.settings.guild_id,
        channel_id=channel_id,
        session_id=str(quartermaster_status["session_id"]),
        provider_reference=f"channel:{channel_id}",
        correlation_id=f"qm:status:{uuid4().hex}",
        payload={"source": "quartermaster-api"},
    )
    try:
        result = gateway.execute(request).validate()
    except ProviderTimeout as error:
        result = ProviderResult(status="UNKNOWN", error=str(error) or "Avrae status is unknown")
    except Exception as error:
        # A read failure must not turn the Activity into a generic 500 page;
        # the provider boundary still distinguishes a known failure from an
        # unresolved timeout.
        result = ProviderResult(status="FAILED", error=str(error) or "Avrae status failed")
    return {
        "status": result.status,
        "provider": request.provider,
        "operation_kind": request.operation_kind,
        "operation_id": request.operation_id,
        "correlation_id": result.correlation_id or request.correlation_id,
        "provider_reference": result.provider_reference or request.provider_reference,
        "provider_version": result.provider_version,
        "result": dict(result.payload or {}),
        "retryable": result.retryable,
        "error": result.error,
        "quartermaster": quartermaster_status,
    }


@router.get("/session/continuity")
def continuity(state: State, _: CurrentActor, limit: int = 20) -> dict[str, Any]:
    return state.context.sessions.continuity(limit=_bounded(limit))


@router.get("/dice/rolls")
def dice_rolls(state: State, _: CurrentActor, limit: int = 20) -> dict[str, Any]:
    return _dice_service(state.context).public_rolls(limit=_bounded(limit))


@router.get("/export")
def export(state: State, _: CurrentDM) -> dict[str, Any]:
    return {"export": render_export(state.context.store)}


# Mutations ------------------------------------------------------------------
#
# Stage 4. Three rules hold across every route below, and each of them is the
# difference between a signed Discord interaction and a web page:
#
# `actor_id` is never read from a body. It comes from `current_actor`, which
# reads it out of the token this process signed, so a request that names an
# actor is not refused — it is ignored, which is the only outcome that cannot
# be worked around.
#
# `party_authorized` is set here, never accepted. Using an item and correcting
# the Party Stash are the same call separated only by that flag, so a body that
# could set it would turn "use up what you carry" into "remove what the party
# shares" for anyone who can edit a request.
#
# Each mutation is `def`, not `async def`, so FastAPI runs it in a worker
# thread against the same serialized store the bot writes through.


class TakePrepareRequest(BaseModel):
    stack_id: str = Field(min_length=1, max_length=64)
    amount: int | Literal["all"] = 1


class HandleRequest(BaseModel):
    handle_id: str = Field(min_length=1, max_length=64)


class GivePrepareRequest(BaseModel):
    stack_id: str = Field(min_length=1, max_length=64)


class GiveRequest(BaseModel):
    handle_id: str = Field(min_length=1, max_length=64)
    #: `party`, or a character id. The service resolves it and refuses a
    #: non-active recipient; nothing here decides who may receive what.
    destination: str = Field(default="party", min_length=1, max_length=64)


class GiveQuantityRequest(BaseModel):
    item_name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1)
    destination: str = Field(default="party", min_length=1, max_length=64)


class UseRequest(BaseModel):
    stack_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=200)


class ClaimRequest(BaseModel):
    drop_item_id: str = Field(min_length=1, max_length=64)
    amount: int = Field(default=1, ge=1)


class CoinRequest(BaseModel):
    amounts: dict[str, int]
    destination: str = Field(default="party", min_length=1, max_length=64)


class TreasuryGiveRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    amounts: dict[str, int]


class CharacterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    discord_user_id: str | None = Field(default=None, max_length=32)


class CharacterDossierRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=200)
    system: str = Field(min_length=1, max_length=80)
    rules_version: str = Field(min_length=1, max_length=80)
    level: int | None = Field(default=None, ge=1, le=30)
    proficiency_bonus: int | None = Field(default=None, ge=0, le=20)
    ability_scores: dict[str, int] = Field(default_factory=dict)
    ability_modifiers: dict[str, int] = Field(default_factory=dict)
    armor_class: int | None = Field(default=None, ge=0, le=100)
    hit_points: int | None = Field(default=None, ge=0, le=1000)
    temporary_hit_points: int = Field(default=0, ge=0, le=1000)
    initiative: int | None = Field(default=None, ge=-100, le=100)
    saving_throws: dict[str, int] = Field(default_factory=dict)
    spell_attack_modifier: int | None = Field(default=None, ge=-100, le=100)
    spell_save_dc: int | None = Field(default=None, ge=0, le=100)
    spell_resources: dict[str, int] = Field(default_factory=dict)
    equipped: dict[str, str] = Field(default_factory=dict)
    observed_at: str = Field(min_length=1, max_length=64)
    source_freshness: Literal["CURRENT", "STALE"] = "CURRENT"


class TransitionRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    lifecycle: str = Field(min_length=1, max_length=32)


class DiceRollRequest(BaseModel):
    expression: str = Field(min_length=1, max_length=40)
    mode: Literal["normal", "advantage", "disadvantage"] = "normal"
    label: str | None = Field(default=None, max_length=100)
    visibility: Literal["PUBLIC", "DM_ONLY"] = "PUBLIC"


def _committed(result: ReceiptResult) -> dict[str, Any]:
    """A mutation's answer: what it did, and the receipt that says it did it."""
    return {
        "operation_id": result.operation_id,
        "receipt_status": result.status,
        "result": result.logical_response,
    }


@router.post("/dice/roll")
def dice_roll(
    state: State,
    actor: CurrentActor,
    action: Idempotency,
    request: DiceRollRequest,
) -> dict[str, Any]:
    if request.visibility == "DM_ONLY" and not actor.is_dm:
        raise HTTPException(status_code=403, detail="DM-only rolls require DM authority")
    try:
        result = _dice_service(state.context).roll_interaction(
            action,
            actor_id=actor.id,
            expression=request.expression,
            mode=request.mode,
            label=request.label,
            visibility=request.visibility,
        )
    except DiceRollError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _committed(result)


# Taking from the Party Stash ------------------------------------------------


@router.post("/stash/take/prepare")
def prepare_take(state: State, actor: CurrentActor, request: TakePrepareRequest) -> dict[str, Any]:
    """Mint the handle one take will spend.

    The handle carries the read set the take was decided against, which is what
    lets `all` mean a number rather than "whatever is there by the time this
    arrives". In Discord that read set was the rendered message, minted minutes
    before anyone pressed. Here it is minted when the player acts, so it means
    "what was true when you pressed" — and the stale *screen* the panel had to
    defend against is prevented upstream, by the live feed, rather than caught
    downstream by a confirmation.

    That does not make the check ornamental. The race the plan names — two
    players taking all of one stack in the same tick — happens inside this
    round trip, and this is still what catches it.

    No idempotency key: minting is not an operation. A retry mints a second
    handle, which expires unused in five minutes, and that is a better failure
    than a receipt for an action nobody completed.
    """
    handle_id = state.context.inventory.create_take_handle(
        stack_id=request.stack_id, actor_id=actor.id, amount=request.amount
    )
    return {"handle_id": handle_id}


@router.post("/stash/take")
def take(state: State, actor: CurrentActor, action: Idempotency, request: HandleRequest) -> dict[str, Any]:
    return _committed(
        state.context.inventory.take_interaction(action, handle_id=request.handle_id, actor_id=actor.id)
    )


@router.post("/stash/take/confirm")
def confirm_take(
    state: State, actor: CurrentActor, action: Idempotency, request: HandleRequest
) -> dict[str, Any]:
    """Take the current quantity, after being told it moved.

    A separate route rather than a flag on the one above, because answering
    "yes, the new number" is a second decision by the player and has to look
    like one from the outside.
    """
    return _committed(
        state.context.inventory.confirm_take_interaction(
            action, handle_id=request.handle_id, actor_id=actor.id
        )
    )


# Giving and using what a character holds ------------------------------------


@router.post("/items/give/prepare")
def prepare_give(state: State, actor: CurrentActor, request: GivePrepareRequest) -> dict[str, Any]:
    """Mint the give controls for one held stack, for the same reason as above.

    Both handles are minted and one is spent. The unused one expires, and the
    alternative — asking the client which it wants before it knows — would put
    the decision a round trip earlier without making it any more informed.
    """
    return state.context.inventory.create_give_handles(stack_id=request.stack_id, actor_id=actor.id)


@router.post("/items/give")
def give(state: State, actor: CurrentActor, action: Idempotency, request: GiveRequest) -> dict[str, Any]:
    return _committed(
        state.context.inventory.give_with_handle_interaction(
            action, handle_id=request.handle_id, actor_id=actor.id, destination=request.destination
        )
    )


@router.post("/items/give/confirm")
def confirm_give(
    state: State, actor: CurrentActor, action: Idempotency, request: GiveRequest
) -> dict[str, Any]:
    return _committed(
        state.context.inventory.confirm_give_with_handle_interaction(
            action, handle_id=request.handle_id, actor_id=actor.id, destination=request.destination
        )
    )


@router.post("/items/give/some")
def give_some(
    state: State, actor: CurrentActor, action: Idempotency, request: GiveQuantityRequest
) -> dict[str, Any]:
    """A give whose quantity the player typed, which needs no handle.

    Not in the plan's table, and it belongs there: the panel has had both since
    the surface pass. A named quantity has nothing on screen to go stale,
    because the number came from the person giving rather than from a render.
    """
    return _committed(
        state.context.inventory.give_interaction(
            action,
            actor_id=actor.id,
            item_name=request.item_name,
            quantity=request.quantity,
            destination=request.destination,
        )
    )


@router.post("/items/use")
def use(state: State, actor: CurrentActor, action: Idempotency, request: UseRequest) -> dict[str, Any]:
    """Spend what the caller's own character is carrying.

    `party_authorized=False`, written here and reachable from nowhere else on
    this route. Correcting the Party Stash is the same service call with that
    flag set, and it is a DM control on `/api/stash/correct`.
    """
    return _committed(
        state.context.inventory.consume_interaction(
            action,
            actor_id=actor.id,
            stack_id=request.stack_id,
            quantity=request.quantity,
            reason=request.reason,
            party_authorized=False,
        )
    )


# Loot Drops -----------------------------------------------------------------


@router.post("/loot/claim")
def claim(state: State, actor: CurrentActor, action: Idempotency, request: ClaimRequest) -> dict[str, Any]:
    """Mint and spend in one request, deliberately unlike take and give.

    A claim handle carries `remaining_quantity`, and nothing compares it to
    anything: the claim is for an absolute amount and re-checks the remainder
    inside the transaction, so there is no relative meaning for a read set to
    preserve. What the handle is actually doing here is binding the claim to
    one actor and to one use, and minting it inside the request satisfies both.

    A retry under the same key mints a handle that is then never spent, because
    the receipt answers first. It expires in five minutes.
    """
    handle_id = state.context.loot.create_claim_handle(
        drop_item_id=request.drop_item_id, actor_id=actor.id, amount=request.amount
    )
    return _committed(state.context.loot.claim_interaction(action, handle_id=handle_id, actor_id=actor.id))


# Coin -----------------------------------------------------------------------


@router.post("/treasury/return")
def return_coin(
    state: State, actor: CurrentActor, action: Idempotency, request: CoinRequest
) -> dict[str, Any]:
    """The player's own coin, back to the treasury or on to another character."""
    return _committed(
        state.context.currency.give_from_character_interaction(
            action, actor_id=actor.id, amounts=request.amounts, destination=request.destination
        )
    )


@router.post("/treasury/give")
def give_coin(state: State, dm: CurrentDM, action: Idempotency, request: TreasuryGiveRequest) -> dict[str, Any]:
    """Treasury → a character, and a DM control.

    The plan's table leaves the DM column blank on this row and on the two
    character routes below. That is a mistake in the table rather than a
    decision: all three are behind `_require_dm` on the panel, and an API that
    grants authority the surface it replaces does not grant is not a migration.
    """
    return _committed(
        state.context.currency.give_to_character_interaction(
            action, actor_id=dm.id, character_id=request.character_id, amounts=request.amounts
        )
    )


# Characters -----------------------------------------------------------------


@router.post("/characters")
def register_character(
    state: State, dm: CurrentDM, action: Idempotency, request: CharacterRequest
) -> dict[str, Any]:
    """Register a character, for a player the DM names.

    `discord_user_id` is whose character it is, not who is asking — the DM is
    the actor on this call and the player is its subject, exactly as on the
    panel, where the player comes from a user select. It is the one identifier
    on this surface that is legitimately in a body.
    """
    return _committed(
        state.context.characters.create_interaction(
            action, actor_id=dm.id, name=request.name, discord_user_id=request.discord_user_id
        )
    )


@router.post("/characters/dossier")
def import_character_dossier(
    state: State,
    dm: CurrentDM,
    action: Idempotency,
    request: CharacterDossierRequest,
) -> dict[str, Any]:
    """Import one explicitly typed manual snapshot for a character.

    The Activity only reads this data. The DM is the source boundary for this
    first slice; the route never accepts a provider label or lets the snapshot
    authorize a mechanic.
    """

    payload = request.model_dump()
    payload["source"] = "MANUAL_IMPORT"
    try:
        result = _dossier_service(state.context).save_interaction(
            action,
            actor_id=dm.id,
            character_id=request.character_id,
            snapshot=payload,
        )
    except DossierError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _committed(result)


@router.post("/characters/transition")
def transition_character(
    state: State, dm: CurrentDM, action: Idempotency, request: TransitionRequest
) -> dict[str, Any]:
    return _committed(
        state.context.characters.transition_interaction(
            action, actor_id=dm.id, character_id=request.character_id, lifecycle=request.lifecycle
        )
    )


# DM controls ----------------------------------------------------------------
#
# Stage 5 of `docs/activity-migration-plan.md`: what the DM does, so that an
# evening can be run without opening a panel.
#
# Everything below is `CurrentDM` rather than `CurrentActor`, and that is the
# whole difference between this section and the one above it. The authority is
# re-checked per request, in `current_dm`, and again by the service inside the
# transaction — the same two checks the panel makes when it renders a control
# and when somebody presses it.
#
# Three of these are not receipt-backed and say so where they are declared:
# maintenance, backup, and the full health report are operator actions against
# the runtime rather than mutations of the campaign, and there is nothing for a
# receipt to replay.


class GrantRequest(BaseModel):
    item_name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1)
    provenance: str | None = Field(default=None, max_length=200)


class CorrectRequest(BaseModel):
    stack_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=200)


class DropItemRequest(BaseModel):
    item_name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1)
    provenance: str | None = Field(default=None, max_length=200)


class DropRequest(BaseModel):
    #: The panel makes a drop of one item because a modal holds five fields.
    #: This one takes a list because a form does not, which is the first place
    #: on this surface where the Activity is allowed to be better rather than
    #: merely equivalent.
    items: list[DropItemRequest] = Field(min_length=1, max_length=50)
    #: Bounded here as well as in the service, and to the same 720 hours the
    #: panel's modal accepts: an absolute expiry is what closes a drop nobody
    #: came back to, and a year-long one is not one.
    expiry_hours: int = Field(default=72, ge=1, le=720)


class CloseDropRequest(BaseModel):
    drop_id: str = Field(min_length=1, max_length=64)


class TreasuryAdjustRequest(BaseModel):
    deltas: dict[str, int]
    reason: str | None = Field(default=None, max_length=200)


class SplitPreviewRequest(BaseModel):
    amounts: dict[str, int]


class SplitRequest(BaseModel):
    handle_id: str = Field(min_length=1, max_length=64)
    #: Set only by a client answering the question a `STALE` refusal asked.
    confirm_current: bool = False


class SessionEndRequest(BaseModel):
    #: Stripped before it is measured, so a space is not an endpoint. The
    #: panel's modal cannot make that distinction and this can, and the
    #: sentence is the whole of what the next evening opens on.
    where_ended: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CombatOpenRequest(BaseModel):
    channel_id: str = Field(min_length=1, max_length=32)


class CombatCloseRequest(BaseModel):
    outcome: str | None = Field(default=None, max_length=200)


class AvraeAttackRequest(BaseModel):
    attack: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)
    args: str = Field(default="", max_length=500)


class AvraeCheckRequest(BaseModel):
    skill: str = Field(min_length=1, max_length=120)
    args: str = Field(default="", max_length=500)


class AvraeSaveRequest(BaseModel):
    save: str = Field(min_length=1, max_length=120)
    args: str = Field(default="", max_length=500)


class AvraeCastRequest(BaseModel):
    spell: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)
    args: str = Field(default="", max_length=500)


class EstateRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    destination: str = Field(default="party", min_length=1, max_length=64)


# The Party Stash, from the other side --------------------------------------


@router.post("/stash/grant")
def grant(state: State, dm: CurrentDM, action: Idempotency, request: GrantRequest) -> dict[str, Any]:
    """Put something into the Party Stash.

    The one mutation that mints rather than moves, which is why provenance is
    on it: nothing upstream of a grant can say where the item came from.
    """
    return _committed(
        state.context.inventory.grant_interaction(
            action,
            actor_id=dm.id,
            item_name=request.item_name,
            quantity=request.quantity,
            provenance=request.provenance,
        )
    )


@router.post("/stash/correct")
def correct(state: State, dm: CurrentDM, action: Idempotency, request: CorrectRequest) -> dict[str, Any]:
    """Take something out of the Party Stash and out of the campaign.

    The same call as `/api/items/use`, separated from it only by
    `party_authorized`, which is set here and is not in `CorrectRequest` —
    there is no field for a body to fill. That is the flag the plan names: a
    request that could carry it would turn "use up what you carry" into
    "remove what the party shares" for anyone who can edit one.

    The DM check is therefore doing two jobs. `consume_interaction` follows
    possession — a player may spend what they hold — and this is the only route
    that reaches the other half of it.
    """
    return _committed(
        state.context.inventory.consume_interaction(
            action,
            actor_id=dm.id,
            stack_id=request.stack_id,
            quantity=request.quantity,
            reason=request.reason,
            party_authorized=True,
        )
    )


# Loot Drops -----------------------------------------------------------------


@router.post("/loot/drops")
def create_drop(state: State, dm: CurrentDM, action: Idempotency, request: DropRequest) -> dict[str, Any]:
    return _committed(
        state.context.loot.create_drop_interaction(
            action,
            actor_id=dm.id,
            items=[(item.item_name, item.quantity, item.provenance) for item in request.items],
            expiry_hours=request.expiry_hours,
        )
    )


@router.post("/loot/drops/close")
def close_drop(state: State, dm: CurrentDM, action: Idempotency, request: CloseDropRequest) -> dict[str, Any]:
    """Close a drop early. What nobody claimed goes back to the Party Stash."""
    return _committed(
        state.context.loot.close_drop_interaction(action, drop_id=request.drop_id, actor_id=dm.id)
    )


# The treasury ---------------------------------------------------------------


@router.post("/treasury/adjust")
def adjust_treasury(
    state: State, dm: CurrentDM, action: Idempotency, request: TreasuryAdjustRequest
) -> dict[str, Any]:
    """Add to or take from the treasury, by a signed amount per denomination."""
    return _committed(
        state.context.currency.adjust_treasury_interaction(
            action, actor_id=dm.id, deltas=request.deltas, reason=request.reason
        )
    )


@router.post("/treasury/split/preview")
def preview_split(state: State, dm: CurrentDM, request: SplitPreviewRequest) -> dict[str, Any]:
    """Say who would be paid what, and mint the handle that would pay them.

    A split reads the roster twice — once to show the DM the shares, once to
    move the coin — and a character dying in between changes every share. The
    handle carries the roster and treasury the preview was computed against, so
    the commit can tell that it is about to pay out numbers nobody was shown.

    No idempotency key, for the same reason the take and give preparations have
    none: minting is not an operation. A retry costs an unspent handle.

    Answering a `STALE` refusal means calling this again — to see the shares as
    they now stand — and then committing the *original* handle with
    `confirm_current`. The second preview's handle is the cost of asking the
    question, and it expires unspent.
    """
    return state.context.currency.prepare_split(actor_id=dm.id, amounts=request.amounts)


@router.post("/treasury/split")
def split_treasury(state: State, dm: CurrentDM, action: Idempotency, request: SplitRequest) -> dict[str, Any]:
    """Commit a previewed split.

    `confirm_current` is the DM answering "yes, against the party as it stands
    now" to a refusal they were shown. It is a field rather than a second route
    because the domain takes it as an argument, and because — unlike take and
    give — the confirmation does not change which handle is spent.
    """
    return _committed(
        state.context.currency.split_relative_interaction(
            action, handle_id=request.handle_id, actor_id=dm.id, confirm_current=request.confirm_current
        )
    )


# The session ----------------------------------------------------------------


@router.post("/session/start")
def start_session(state: State, dm: CurrentDM, action: Idempotency) -> dict[str, Any]:
    """Start a session, or report the one already running.

    A stale active session is never closed silently — the domain answers
    `ACTIVE_EXISTS` and names it, and ending it is a separate decision.
    """
    return _committed(state.context.sessions.start_interaction(action, actor_id=dm.id))


@router.post("/session/end")
def end_session(
    state: State, dm: CurrentDM, action: Idempotency, request: SessionEndRequest
) -> dict[str, Any]:
    """End the session, with the one sentence the next evening opens on.

    `where_ended` is required rather than optional, here as on the panel: it is
    the whole of specification 29's continuity, and a session ended without one
    leaves the table with nothing to pick up.
    """
    return _committed(
        state.context.sessions.end_interaction(action, actor_id=dm.id, where_ended=request.where_ended)
    )


# Combat ---------------------------------------------------------------------


@router.post("/combat/open")
def open_combat(
    state: State, dm: CurrentDM, action: Idempotency, request: CombatOpenRequest
) -> dict[str, Any]:
    """Open Quartermaster's own record of a fight. Nothing Avrae owns is in it.

    `channel_id` is in the body, and it is not an exception to the rule above
    it. The panel reads it off the interaction because Discord put it there;
    a browser has it from the SDK and there is nowhere else to get it. It
    names where the fight is happening on a record this process writes, and
    authorizes nothing — the authority on this route is the DM check, and a
    channel a client made up buys whoever sent it a mislabelled row.
    """
    return _committed(
        state.context.combat.open_interaction(action, actor_id=dm.id, channel_id=request.channel_id)
    )


@router.post("/combat/close")
def close_combat(
    state: State, dm: CurrentDM, action: Idempotency, request: CombatCloseRequest
) -> dict[str, Any]:
    """Close the fight, and report what is still unclaimed while everyone is here."""
    return _committed(
        state.context.combat.close_interaction(action, actor_id=dm.id, outcome=request.outcome)
    )


@router.post("/combat/avrae/next")
def advance_avrae_turn(state: State, actor: CurrentActor, action: Idempotency) -> dict[str, Any]:
    """Ask Avrae to advance the native turn, preserving its authority checks.

    Quartermaster only supplies the authenticated actor and the channel-bound
    encounter context. Avrae decides whether that actor may advance the turn
    and commits the native combat model; the durable provider receipt records
    the result or an unresolved timeout.
    """

    quartermaster_status = state.context.combat.status()
    encounter = quartermaster_status.get("encounter")
    if quartermaster_status.get("status") != "OPEN" or not isinstance(encounter, dict):
        return {
            "status": "NOT_QUERIED",
            "provider": "avrae",
            "operation_kind": "next",
            "quartermaster": quartermaster_status,
            "result": None,
        }

    gateway = state.context.avrae_gateway
    if gateway is None:
        return {
            "status": "NOT_CONFIGURED",
            "provider": "avrae",
            "operation_kind": "next",
            "quartermaster": quartermaster_status,
            "result": None,
            "error": "the Avrae operation adapter is not configured",
        }

    channel_id = str(encounter["channel_id"])
    provider_operations = _provider_operations(state.context)
    execution = provider_operations.begin(
        action,
        actor_id=actor.id,
        guild_id=state.settings.guild_id,
        channel_id=channel_id,
        session_id=str(quartermaster_status["session_id"]),
        operation_kind="next",
        provider_reference=f"channel:{channel_id}",
        payload={"source": "quartermaster-api"},
    )
    try:
        result = provider_operations.execute(execution, gateway)
    except ProviderIntegrationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return result.logical_response


@router.post("/combat/avrae/attack")
def execute_avrae_attack(
    state: State,
    actor: CurrentActor,
    action: Idempotency,
    request: AvraeAttackRequest,
) -> dict[str, Any]:
    """Ask Avrae to resolve one native attack for the active combatant."""

    quartermaster_status = state.context.combat.status()
    encounter = quartermaster_status.get("encounter")
    if quartermaster_status.get("status") != "OPEN" or not isinstance(encounter, dict):
        return {
            "status": "NOT_QUERIED",
            "provider": "avrae",
            "operation_kind": "attack",
            "quartermaster": quartermaster_status,
            "result": None,
        }

    gateway = state.context.avrae_gateway
    if gateway is None:
        return {
            "status": "NOT_CONFIGURED",
            "provider": "avrae",
            "operation_kind": "attack",
            "quartermaster": quartermaster_status,
            "result": None,
            "error": "the Avrae operation adapter is not configured",
        }

    channel_id = str(encounter["channel_id"])
    provider_operations = _provider_operations(state.context)
    execution = provider_operations.begin(
        action,
        actor_id=actor.id,
        guild_id=state.settings.guild_id,
        channel_id=channel_id,
        session_id=str(quartermaster_status["session_id"]),
        operation_kind="attack",
        provider_reference=f"channel:{channel_id}",
        payload={
            "source": "quartermaster-api",
            "attack": request.attack,
            "target": request.target,
            "args": request.args,
        },
    )
    try:
        result = provider_operations.execute(execution, gateway)
    except ProviderIntegrationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return result.logical_response


@router.post("/combat/avrae/check")
def execute_avrae_check(
    state: State,
    actor: CurrentActor,
    action: Idempotency,
    request: AvraeCheckRequest,
) -> dict[str, Any]:
    """Ask Avrae to resolve one native ability check for the active combatant."""

    quartermaster_status = state.context.combat.status()
    encounter = quartermaster_status.get("encounter")
    if quartermaster_status.get("status") != "OPEN" or not isinstance(encounter, dict):
        return {
            "status": "NOT_QUERIED",
            "provider": "avrae",
            "operation_kind": "check",
            "quartermaster": quartermaster_status,
            "result": None,
        }

    gateway = state.context.avrae_gateway
    if gateway is None:
        return {
            "status": "NOT_CONFIGURED",
            "provider": "avrae",
            "operation_kind": "check",
            "quartermaster": quartermaster_status,
            "result": None,
            "error": "the Avrae operation adapter is not configured",
        }

    channel_id = str(encounter["channel_id"])
    provider_operations = _provider_operations(state.context)
    execution = provider_operations.begin(
        action,
        actor_id=actor.id,
        guild_id=state.settings.guild_id,
        channel_id=channel_id,
        session_id=str(quartermaster_status["session_id"]),
        operation_kind="check",
        provider_reference=f"channel:{channel_id}",
        payload={
            "source": "quartermaster-api",
            "skill": request.skill,
            "args": request.args,
        },
    )
    try:
        result = provider_operations.execute(execution, gateway)
    except ProviderIntegrationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return result.logical_response


@router.post("/combat/avrae/save")
def execute_avrae_save(
    state: State,
    actor: CurrentActor,
    action: Idempotency,
    request: AvraeSaveRequest,
) -> dict[str, Any]:
    """Ask Avrae to resolve one native saving throw for the active combatant."""

    quartermaster_status = state.context.combat.status()
    encounter = quartermaster_status.get("encounter")
    if quartermaster_status.get("status") != "OPEN" or not isinstance(encounter, dict):
        return {
            "status": "NOT_QUERIED",
            "provider": "avrae",
            "operation_kind": "save",
            "quartermaster": quartermaster_status,
            "result": None,
        }

    gateway = state.context.avrae_gateway
    if gateway is None:
        return {
            "status": "NOT_CONFIGURED",
            "provider": "avrae",
            "operation_kind": "save",
            "quartermaster": quartermaster_status,
            "result": None,
            "error": "the Avrae operation adapter is not configured",
        }

    channel_id = str(encounter["channel_id"])
    provider_operations = _provider_operations(state.context)
    execution = provider_operations.begin(
        action,
        actor_id=actor.id,
        guild_id=state.settings.guild_id,
        channel_id=channel_id,
        session_id=str(quartermaster_status["session_id"]),
        operation_kind="save",
        provider_reference=f"channel:{channel_id}",
        payload={
            "source": "quartermaster-api",
            "save": request.save,
            "args": request.args,
        },
    )
    try:
        result = provider_operations.execute(execution, gateway)
    except ProviderIntegrationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return result.logical_response


@router.post("/combat/avrae/cast")
def execute_avrae_cast(
    state: State,
    actor: CurrentActor,
    action: Idempotency,
    request: AvraeCastRequest,
) -> dict[str, Any]:
    """Ask Avrae to cast one native prepared spell at one native target."""

    quartermaster_status = state.context.combat.status()
    encounter = quartermaster_status.get("encounter")
    if quartermaster_status.get("status") != "OPEN" or not isinstance(encounter, dict):
        return {
            "status": "NOT_QUERIED",
            "provider": "avrae",
            "operation_kind": "cast",
            "quartermaster": quartermaster_status,
            "result": None,
        }

    gateway = state.context.avrae_gateway
    if gateway is None:
        return {
            "status": "NOT_CONFIGURED",
            "provider": "avrae",
            "operation_kind": "cast",
            "quartermaster": quartermaster_status,
            "result": None,
            "error": "the Avrae operation adapter is not configured",
        }

    channel_id = str(encounter["channel_id"])
    provider_operations = _provider_operations(state.context)
    execution = provider_operations.begin(
        action,
        actor_id=actor.id,
        guild_id=state.settings.guild_id,
        channel_id=channel_id,
        session_id=str(quartermaster_status["session_id"]),
        operation_kind="cast",
        provider_reference=f"channel:{channel_id}",
        payload={
            "source": "quartermaster-api",
            "spell": request.spell,
            "target": request.target,
            "args": request.args,
        },
    )
    try:
        result = provider_operations.execute(execution, gateway)
    except ProviderIntegrationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return result.logical_response


# Characters -----------------------------------------------------------------


@router.post("/characters/estate")
def resolve_estate(
    state: State, dm: CurrentDM, action: Idempotency, request: EstateRequest
) -> dict[str, Any]:
    """Move a non-active character's belongings, explicitly.

    Kept apart from the lifecycle transition above it on purpose: a character
    dying must never silently move what they were carrying, so the transition
    and the resolution are two decisions and two records.
    """
    return _committed(
        state.context.characters.resolve_belongings_interaction(
            action, actor_id=dm.id, character_id=request.character_id, destination=request.destination
        )
    )


# Maintenance ----------------------------------------------------------------
#
# The one group on this surface that is not a domain mutation. Nothing here
# takes an idempotency key, and the reason is not that a retry is harmless: it
# is that there is no receipt to replay. `run_maintenance` deletes what is past
# its retention window, which is the same answer run twice; a backup writes a
# timestamped snapshot, so a repeat writes a second file that retention then
# reaps. Both record their own outcome in `maintenance_runs`, which is where
# `health` reads them back from.
#
# They are `def`, so they run in a worker thread. A backup snapshots the
# database through the same online-backup path the CLI uses, and holds the
# store while it does — which is a second reason the event loop must not be
# the thread that waits for it.


@router.post("/maintenance/run")
def maintenance(state: State, _: CurrentDM) -> dict[str, Any]:
    settings = state.settings
    return run_maintenance(
        state.context.store,
        receipt_retention_seconds=settings.receipt_retention_seconds,
        handle_retention_seconds=settings.handle_retention_seconds,
    )


@router.post("/maintenance/backup")
def backup(state: State, _: CurrentDM) -> dict[str, Any]:
    """A validated snapshot, beside the scheduled ones rather than somewhere new.

    The directory, the off-device copy, and the retention count all come from
    configuration, so a backup taken from here rotates the same set of files
    the scheduled ones do and `health` reports on whichever was written last.
    """
    settings = state.settings
    try:
        return create_scheduled_backup(
            state.context.store,
            settings.backup_directory,
            off_device_directory=settings.backup_off_device_directory,
            retention_count=settings.backup_retention_count,
        )
    except OSError as error:
        # The one route here that fails for reasons outside the campaign — a
        # full disk, a directory nothing may write to. It answers 500 because
        # the server is what failed, and it carries the reason because the
        # person pressing this is the person who can fix it. The failure is
        # already recorded in `maintenance_runs`, so `health` says so too.
        raise HTTPException(
            status_code=500, detail=f"the backup could not be written: {error}"
        ) from error


@router.get("/maintenance/health")
def full_health(state: State, _: CurrentDM) -> dict[str, Any]:
    """What the runtime can see, both ways it is read.

    `report` is the machine-readable snapshot; `rendered` is what the panel
    prints, carried so that a DM reading it here and a DM reading it in Discord
    are reading the same sentences rather than two renderings of one truth.

    Not `/api/health`, which is unauthenticated and says only that the process
    is up: this one names sessions, receipts, outbox depth, and backups, and
    that is campaign state.
    """
    report = health_report(state.context.store)
    return {"report": report, "rendered": render_health(report)}


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


# Refusals -------------------------------------------------------------------

#: How a refusal from the service layer reaches the client, and what the client
#: is expected to do about it.
#:
#: The panels answer a refusal with a sentence, because a person is reading it.
#: A client is reading this one, and three of these need different behaviour
#: rather than different wording: `STALE` is a question to put to the player
#: and answer on the confirm route, `HANDLE` means the handle is spent or
#: expired and the action has to be prepared again, and `REFUSED` is the
#: domain's answer and the end of it. The message stays in `detail`, where the
#: reads already put theirs, so one client-side error type covers both.
#:
#: Order does not select the handler — Starlette walks the exception's MRO — so
#: `SemanticStaleness` is found before the `InventoryError` it inherits from.
DOMAIN_REFUSALS: tuple[tuple[type[Exception], int, str], ...] = (
    (SemanticStaleness, 409, "STALE"),
    (CurrencySemanticStaleness, 409, "STALE"),
    (HandleError, 409, "HANDLE"),
    (ReceiptError, 409, "RETRY"),
    (InventoryError, 422, "REFUSED"),
    (CurrencyError, 422, "REFUSED"),
    (LootDropError, 422, "REFUSED"),
    (CharacterError, 422, "REFUSED"),
    (SessionError, 422, "REFUSED"),
    (CombatError, 422, "REFUSED"),
)


def _refusal(status_code: int, code: str) -> Any:
    async def handler(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": str(error), "code": code})

    return handler


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

    for error_type, status_code, code in DOMAIN_REFUSALS:
        # Registered on the app rather than caught per route: every mutation
        # raises the same eight types, and a try/except around each of thirteen
        # calls is thirteen chances to map one of them differently.
        app.add_exception_handler(error_type, _refusal(status_code, code))

    app.include_router(router)
    # The same routes again under the prefix the client actually asks for.
    #
    # Discord's proxy has carried `/.proxy/<path>` since Activities shipped and
    # forwards it to `<path>` on the mapped target, so a backend serving only
    # `/api` is the documented arrangement; since 2025-07-30 the prefix is
    # optional and an unprefixed path is forwarded identically. Answering both
    # costs one line and means the first launch does not depend on which of
    # those two behaviours is live, which is not a thing worth discovering from
    # a blank frame inside Discord.
    #
    # It also makes the built page openable straight from the bind for a smoke
    # test: `api.js` asks for `/.proxy/api/...` with no proxy in front of it,
    # and gets an answer rather than a 404.
    app.include_router(router, prefix=PROXY_PREFIX)

    if settings.activity_dist is not None:
        # Serving the built page from the same origin as the API is what makes
        # one URL mapping enough, and it keeps the client's fetches
        # same-origin rather than relying on the CORS branch above. Mounted
        # after the routers, so `/api/...` never resolves to a file.
        from fastapi.staticfiles import StaticFiles

        distribution = settings.activity_dist.expanduser()
        if not distribution.is_dir():
            raise ConfigurationError(f"QM_ACTIVITY_DIST is not a directory: {distribution}")
        # Mounted under the proxy prefix as well, for the same reason the router
        # is: a page loaded at `/.proxy/` has to find its own assets.
        app.mount(
            PROXY_PREFIX, StaticFiles(directory=str(distribution), html=True), name="activity-proxy"
        )
        app.mount("/", StaticFiles(directory=str(distribution), html=True), name="activity")

    return app
