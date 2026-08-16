"""The HTTP surface the Activity reads and writes, and the socket that says when
to read again.

Stages 1, 3, and 4 of `docs/activity-migration-plan.md`: every read a panel
performs, available over HTTP with the actor derived from a signed token rather
than from anything the client sent; `/api/live`, a WebSocket carrying change
notifications keyed on `domain_events.sequence`; and the mutations a player
performs at the table, each keyed by an idempotency key the client generates
and scoped to the actor that proved who they are.

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

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
from .characters import CharacterError
from .config import ConfigurationError, Settings
from .currency import CurrencyError, CurrencySemanticStaleness
from .db import SCHEMA_VERSION
from .discord_common import Quartermaster
from .export import render_export
from .handles import HandleError
from .inventory import InventoryError, SemanticStaleness
from .loot import LootDropError
from .receipts import ReceiptError, ReceiptResult
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


class TransitionRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=64)
    lifecycle: str = Field(min_length=1, max_length=32)


def _committed(result: ReceiptResult) -> dict[str, Any]:
    """A mutation's answer: what it did, and the receipt that says it did it."""
    return {
        "operation_id": result.operation_id,
        "receipt_status": result.status,
        "result": result.logical_response,
    }


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
    flag set, and it is a DM control on `/api/stash/correct` in Stage 5.
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


@router.post("/characters/transition")
def transition_character(
    state: State, dm: CurrentDM, action: Idempotency, request: TransitionRequest
) -> dict[str, Any]:
    return _committed(
        state.context.characters.transition_interaction(
            action, actor_id=dm.id, character_id=request.character_id, lifecycle=request.lifecycle
        )
    )


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
