"""Who is calling the Activity API, and how the answer is proved.

The bot never had to ask. Discord signed every interaction, so
`interaction.user.id` was a fact the adapter could read straight off the wire,
and `_is_dm` could read the caller's roles off the same object.

An Activity has neither. The frontend is a web page running on a player's
machine, and every byte it sends is a claim rather than a fact. So identity is
established once, against Discord, using an authorization code the client
cannot mint; and from then on it travels in a token this process signed and can
therefore re-verify. Nothing downstream reads an actor from a request body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "Actor",
    "Identity",
    "IdentityError",
    "IdentityProvider",
    "SessionTokens",
    "TokenError",
]


class TokenError(RuntimeError):
    """Raised when a session token is missing, malformed, forged, or expired."""


class IdentityError(RuntimeError):
    """Raised when Discord will not confirm who a launching client is."""


@dataclass(frozen=True)
class Identity:
    """What Discord says about a caller, before Quartermaster interprets it.

    `access_token` is Discord's, not ours, and it goes back to the client on
    purpose. The Embedded App SDK will not answer `getInstanceConnectedParticipants`
    — the roster that makes the party visible to itself — until the client has
    called `authenticate()` with it. It is scoped to `identify` and
    `guilds.members.read`, it authorizes nothing in Quartermaster, and the
    session token is what this API actually checks.
    """

    user_id: str
    guild_roles: tuple[str, ...] = ()
    is_owner: bool = False
    access_token: str = ""


@dataclass(frozen=True)
class Actor:
    """A verified caller, as the service layer wants them.

    `id` is what every service method already calls `actor_id`: the Discord
    user id as a string. It arrives here from a signed token, which is the
    whole point of the type existing.
    """

    id: str
    is_dm: bool
    instance_id: str | None = None


class IdentityProvider(Protocol):
    """The one place the API talks to Discord about identity."""

    async def exchange_code(self, code: str) -> Identity:
        """Trade an OAuth authorization code for the caller's identity.

        Raises `IdentityError` if the code is bad, or if the caller is not a
        member of the configured guild.
        """
        ...


OwnerChecker = Callable[[str], Awaitable[bool]]


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class SessionTokens:
    """Short-lived bearer tokens, signed with the application's own secret.

    Deliberately not a JWT library. The payload is three facts and an expiry,
    the only algorithm is HMAC-SHA256, and there is no `alg` field for a caller
    to talk us out of — which is the failure mode a hand-rolled JWT verifier
    usually ships with.

    The signing key is derived from the OAuth client secret rather than
    configured separately, so a table cannot enable the Activity while leaving
    token signing on a default.
    """

    def __init__(
        self,
        client_secret: str,
        *,
        ttl_seconds: int = 3600,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not client_secret:
            raise ValueError("session tokens need a signing secret")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._key = hashlib.sha256(f"quartermaster.session.v1:{client_secret}".encode()).digest()
        self.ttl_seconds = ttl_seconds
        self._now = now

    def _sign(self, body: str) -> str:
        return _b64encode(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())

    def issue(self, actor: Actor) -> str:
        payload = {
            "sub": actor.id,
            "dm": actor.is_dm,
            "iid": actor.instance_id,
            "exp": int(self._now()) + self.ttl_seconds,
        }
        body = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str) -> Actor:
        body, separator, signature = (token or "").partition(".")
        if not separator or not body or not signature:
            raise TokenError("malformed session token")
        if not hmac.compare_digest(self._sign(body), signature):
            raise TokenError("session token signature does not verify")
        try:
            payload = json.loads(_b64decode(body))
        except (ValueError, json.JSONDecodeError) as exc:
            raise TokenError("unreadable session token") from exc
        if not isinstance(payload, dict):
            raise TokenError("unreadable session token")
        expires_at = payload.get("exp")
        if not isinstance(expires_at, int) or expires_at <= int(self._now()):
            raise TokenError("session token has expired")
        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenError("session token names no actor")
        instance_id = payload.get("iid")
        return Actor(
            id=subject,
            is_dm=payload.get("dm") is True,
            instance_id=instance_id if isinstance(instance_id, str) else None,
        )


def is_dm(roles: Sequence[str], dm_role_ids: Sequence[str]) -> bool:
    """Whether a member's roles carry DM authority.

    Same question `_is_dm` asks of an interaction, asked of a role list, so the
    two surfaces cannot drift into disagreeing about who runs the table. A
    configuration with no DM roles grants nothing, rather than everything.
    """
    if not dm_role_ids:
        return False
    return bool(set(roles) & set(dm_role_ids))


class DiscordIdentityProvider:
    """The real provider: an OAuth code exchange against Discord.

    Two calls, both with the caller's own access token rather than the bot's.
    The second one — the guild member lookup — is what makes the guild check
    and the role-based DM check facts about Discord's state rather than about
    what the client sent us. Guild ownership is checked through the bot's
    already-authoritative guild object when one is supplied, because the
    member endpoint does not report the guild owner.
    """

    TOKEN_URL = "https://discord.com/api/oauth2/token"
    MEMBER_URL = "https://discord.com/api/users/@me/guilds/{guild_id}/member"
    ACTIVITY_REDIRECT_URI = "https://127.0.0.1"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        guild_id: str,
        redirect_uri: str = ACTIVITY_REDIRECT_URI,
        session_factory: Any = None,
        owner_checker: OwnerChecker | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.guild_id = guild_id
        self.redirect_uri = redirect_uri
        self._session_factory = session_factory
        self._owner_checker = owner_checker

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        import aiohttp

        return aiohttp.ClientSession()

    async def exchange_code(self, code: str) -> Identity:
        if not code:
            raise IdentityError("no authorization code was supplied")
        session = self._session()
        async with session:
            async with session.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status != 200:
                    raise IdentityError("Discord rejected the authorization code")
                granted = await response.json()
            access_token = granted.get("access_token")
            if not access_token:
                raise IdentityError("Discord returned no access token")
            authorization = {"Authorization": f"Bearer {access_token}"}
            async with session.get(
                self.MEMBER_URL.format(guild_id=self.guild_id), headers=authorization
            ) as response:
                if response.status == 404:
                    raise IdentityError("you are not a member of this table's guild")
                if response.status != 200:
                    raise IdentityError("Discord would not confirm your membership")
                member = await response.json()
        user = member.get("user") or {}
        user_id = user.get("id")
        if not user_id:
            raise IdentityError("Discord returned a member with no user")
        roles = member.get("roles") or []
        is_owner = False
        if self._owner_checker is not None:
            is_owner = await self._owner_checker(str(user_id))
        return Identity(
            user_id=str(user_id),
            guild_roles=tuple(str(role) for role in roles),
            is_owner=is_owner,
            access_token=str(access_token),
        )
