"""Authenticated, read-only HTTP transport for the Avrae provider boundary.

This module is deliberately a client, not a second combat implementation. The
Quartermaster process sends the actor, guild, channel, session, and correlation
context to an Avrae-owned adapter. Only that adapter may inspect or change
Avrae's native combat model.

The wire format is small enough to vendor in the Avrae Cog. The signature is
HMAC-SHA256 over ``timestamp\\nnonce\\nbody`` where ``body`` is the exact UTF-8
JSON sent on the wire. A timestamp and one-time nonce prevent a captured
request from being replayed; the Avrae-side handler must retain used nonces for
the allowed clock-skew window.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .integration import ProviderIntegrationError, ProviderRequest, ProviderResult, ProviderTimeout

AVRAE_STATUS_PROTOCOL = "qm-avrae-status-v1"
SIGNATURE_HEADER = "X-Quartermaster-Signature"
TIMESTAMP_HEADER = "X-Quartermaster-Timestamp"
NONCE_HEADER = "X-Quartermaster-Nonce"
PROTOCOL_HEADER = "X-Quartermaster-Protocol"
DEFAULT_CLOCK_SKEW_SECONDS = 30


class AvraeGatewayTransport(Protocol):
    """The small seam used to test the gateway without a live HTTP server."""

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        ...


@dataclass(frozen=True)
class AvraeWireRequest:
    """The JSON object sent to the Avrae-owned adapter."""

    protocol: str
    operation_id: str
    provider: str
    operation_kind: str
    actor_id: str
    guild_id: str
    channel_id: str
    session_id: str
    provider_reference: str | None
    correlation_id: str
    payload: Mapping[str, Any]

    @classmethod
    def from_provider_request(cls, request: ProviderRequest) -> AvraeWireRequest:
        if request.operation_kind != "status":
            raise ProviderIntegrationError(
                "the initial Avrae HTTP adapter only permits the read-only status operation"
            )
        return cls(
            protocol=AVRAE_STATUS_PROTOCOL,
            operation_id=request.operation_id,
            provider=request.provider,
            operation_kind=request.operation_kind,
            actor_id=request.actor_id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            session_id=request.session_id,
            provider_reference=request.provider_reference,
            correlation_id=request.correlation_id,
            payload=dict(request.payload),
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "operation_id": self.operation_id,
            "provider": self.provider,
            "operation_kind": self.operation_kind,
            "actor_id": self.actor_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "session_id": self.session_id,
            "provider_reference": self.provider_reference,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
        }


def encode_wire_request(request: ProviderRequest) -> bytes:
    """Serialize one status request deterministically for signing and transport."""

    wire = AvraeWireRequest.from_provider_request(request)
    try:
        return json.dumps(
            wire.as_mapping(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProviderIntegrationError("Avrae request payload must be JSON-serializable") from error


def signature_for(*, secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Return the hex HMAC used by both halves of the adapter."""

    if not secret:
        raise ValueError("Avrae adapter secret must not be empty")
    message = f"{timestamp}\n{nonce}\n".encode("ascii") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    timestamp: str,
    nonce: str,
    signature: str,
    body: bytes,
    now: float | None = None,
    max_clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS,
) -> None:
    """Validate signature freshness; nonce replay storage belongs to the adapter."""

    if not timestamp.isdigit() or not nonce.strip() or not signature.strip():
        raise ProviderIntegrationError("Avrae request signature headers are malformed")
    observed_at = int(timestamp)
    current_time = time.time() if now is None else now
    if abs(current_time - observed_at) > max_clock_skew_seconds:
        raise ProviderIntegrationError("Avrae request signature is outside the clock-skew window")
    expected = signature_for(secret=secret, timestamp=timestamp, nonce=nonce, body=body)
    if not hmac.compare_digest(expected, signature):
        raise ProviderIntegrationError("Avrae request signature is invalid")


def new_nonce() -> str:
    return uuid4().hex


def _read_url(request: urllib.request.Request, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        # A provider-side rejection is known and can be surfaced as FAILED.
        # A gateway/server timeout leaves the result unresolved.
        if error.code in {408, 429} or error.code >= 500:
            raise ProviderTimeout(f"Avrae adapter returned HTTP {error.code}") from error
        raise ProviderIntegrationError(f"Avrae adapter rejected the request with HTTP {error.code}") from error
    except (TimeoutError, urllib.error.URLError) as error:
        raise ProviderTimeout("Avrae adapter did not return a result") from error


class HttpAvraeGateway:
    """Synchronous status client for FastAPI's worker-thread route."""

    def __init__(
        self,
        endpoint_url: str,
        secret: str,
        *,
        timeout_seconds: float = 2.5,
        transport: AvraeGatewayTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        endpoint_url = endpoint_url.strip()
        if not endpoint_url:
            raise ValueError("Avrae adapter endpoint URL must not be empty")
        parsed_url = urlsplit(endpoint_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Avrae adapter endpoint URL must be an http:// or https:// URL")
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ValueError("Avrae adapter endpoint URL must not contain credentials")
        if not secret.strip():
            raise ValueError("Avrae adapter secret must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("Avrae adapter timeout must be positive")
        self.endpoint_url = endpoint_url
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _read_url
        self.clock = clock

    def execute(self, request: ProviderRequest) -> ProviderResult:
        body = encode_wire_request(request)
        timestamp = str(int(self.clock()))
        nonce = new_nonce()
        headers = {
            "Content-Type": "application/json",
            PROTOCOL_HEADER: AVRAE_STATUS_PROTOCOL,
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: signature_for(
                secret=self.secret, timestamp=timestamp, nonce=nonce, body=body
            ),
        }
        http_request = urllib.request.Request(
            self.endpoint_url,
            data=body,
            headers=headers,
            method="POST",
        )
        raw_response = self.transport(http_request, self.timeout_seconds)
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderIntegrationError("Avrae adapter returned invalid JSON") from error
        if not isinstance(response, Mapping):
            raise ProviderIntegrationError("Avrae adapter response must be an object")
        return _provider_result(response).validate()


def _provider_result(response: Mapping[str, Any]) -> ProviderResult:
    status = response.get("status")
    if not isinstance(status, str):
        raise ProviderIntegrationError("Avrae adapter response has no status")
    payload = response.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        raise ProviderIntegrationError("Avrae adapter response payload must be an object")
    values = {
        "provider_reference": response.get("provider_reference"),
        "correlation_id": response.get("correlation_id"),
        "provider_version": response.get("provider_version"),
        "error": response.get("error"),
    }
    if any(value is not None and not isinstance(value, str) for value in values.values()):
        raise ProviderIntegrationError("Avrae adapter response metadata must be strings")
    retryable = response.get("retryable", False)
    if not isinstance(retryable, bool):
        raise ProviderIntegrationError("Avrae adapter response retryable flag must be boolean")
    return ProviderResult(
        status=status,
        provider_reference=values["provider_reference"],
        correlation_id=values["correlation_id"],
        provider_version=values["provider_version"],
        payload=payload,
        error=values["error"],
        retryable=retryable,
    )


def gateway_for_settings(settings: Any) -> HttpAvraeGateway | None:
    """Build the optional gateway without making an HTTP call at startup."""

    if settings.avrae_adapter_url is None:
        return None
    if settings.avrae_adapter_secret is None:
        raise ValueError("Avrae adapter secret is required when its URL is configured")
    return HttpAvraeGateway(
        settings.avrae_adapter_url,
        settings.avrae_adapter_secret,
        timeout_seconds=settings.avrae_adapter_timeout_seconds,
    )
