"""Dependency-light Avrae-side half of the Quartermaster provider protocol.

This file is intended to be imported by a self-hosted Avrae Cog. It does not
open Quartermaster's SQLite database. The Cog supplies a native status reader
that runs inside Avrae, while this handler authenticates and validates the
request envelope before calling it. The operation adapter currently permits
only the bounded ``next``, ``attack``, ``check``, and ``save`` operations.

An HTTP server is intentionally not started here. Avrae's extension should
own the listener lifecycle (and its bind address, TLS, and process policy) and
delegate each POST body to :class:`QuartermasterStatusAdapter`.
"""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from quartermaster.avrae_gateway import (
    AVRAE_OPERATION_KINDS,
    AVRAE_OPERATION_PROTOCOL,
    AVRAE_STATUS_PROTOCOL,
    DEFAULT_CLOCK_SKEW_SECONDS,
    NONCE_HEADER,
    PROTOCOL_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    verify_signature,
)
from quartermaster.integration import ProviderIntegrationError


class NativeStatusProvider(Protocol):
    """The only Avrae-internal dependency this adapter needs."""

    def combat_status(self, request: Mapping[str, Any]) -> Mapping[str, Any] | Any:
        """Return a provider-owned status projection for the native context."""


class NativeOperationProvider(Protocol):
    """The Avrae-internal seam for bounded provider operations."""

    def execute_operation(self, request: Mapping[str, Any]) -> Mapping[str, Any] | Any:
        """Execute the already-authenticated native operation."""


class RequestRejected(ProviderIntegrationError):
    """Raised when the signed request is not acceptable to this adapter."""


@dataclass
class _NonceCache:
    max_age_seconds: float
    values: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def claim(self, nonce: str, *, now: float) -> None:
        with self.lock:
            cutoff = now - self.max_age_seconds
            self.values = {key: seen for key, seen in self.values.items() if seen >= cutoff}
            if nonce in self.values:
                raise RequestRejected("Avrae request nonce has already been used")
            self.values[nonce] = now


@dataclass
class QuartermasterStatusAdapter:
    """Verify and dispatch one read-only Quartermaster status request."""

    secret: str
    guild_id: str
    provider: NativeStatusProvider
    max_clock_skew_seconds: float = DEFAULT_CLOCK_SKEW_SECONDS
    _nonces: _NonceCache = field(init=False)

    def __post_init__(self) -> None:
        if not self.secret.strip():
            raise ValueError("Avrae adapter secret must not be empty")
        if not self.guild_id.strip():
            raise ValueError("Avrae adapter guild must not be empty")
        self._nonces = _NonceCache(self.max_clock_skew_seconds)

    async def handle(
        self,
        body: bytes,
        *,
        headers: Mapping[str, str],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Validate a POST and return a JSON-serializable provider result."""

        request = self._validated_request(
            body,
            headers=headers,
            expected_protocol=AVRAE_STATUS_PROTOCOL,
            expected_operation="status",
            now=now,
        )

        try:
            payload = self.provider.combat_status(request)
            if inspect.isawaitable(payload):
                payload = await payload
        except RequestRejected:
            raise
        except Exception:
            # Do not leak Avrae internals over the adapter. A local native
            # read failure is known to the adapter, while an HTTP timeout is
            # the unresolved case handled by Quartermaster's gateway.
            return {
                "status": "FAILED",
                "provider_reference": request["provider_reference"],
                "correlation_id": request["correlation_id"],
                "provider_version": "avrae-native-status-v1",
                "payload": {},
                "error": "native Avrae status is unavailable",
                "retryable": False,
            }
        if not isinstance(payload, Mapping):
            raise RequestRejected("native Avrae status must be an object")
        return {
            "status": "COMMITTED",
            "provider_reference": request["provider_reference"],
            "correlation_id": request["correlation_id"],
            "provider_version": "avrae-native-status-v1",
            "payload": dict(payload),
            "retryable": False,
        }

    def _validated_request(
        self,
        body: bytes,
        *,
        headers: Mapping[str, str],
        expected_protocol: str,
        expected_operation: str | None,
        now: float | None,
    ) -> dict[str, Any]:
        timestamp = headers.get(TIMESTAMP_HEADER, "")
        nonce = headers.get(NONCE_HEADER, "")
        signature = headers.get(SIGNATURE_HEADER, "")
        if headers.get(PROTOCOL_HEADER) != expected_protocol:
            raise RequestRejected("Avrae request protocol is missing or unsupported")
        observed_now = time.time() if now is None else now
        verify_signature(
            secret=self.secret,
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
            body=body,
            now=observed_now,
            max_clock_skew_seconds=self.max_clock_skew_seconds,
        )
        self._nonces.claim(nonce, now=observed_now)
        request = _decode_request(body)
        if request["protocol"] != expected_protocol:
            raise RequestRejected("Avrae request body protocol is unsupported")
        if request["provider"] != "avrae":
            raise RequestRejected("Avrae adapter only accepts the Avrae provider")
        if expected_operation is not None and request["operation_kind"] != expected_operation:
            raise RequestRejected(f"Avrae adapter only accepts the {expected_operation} operation")
        if request["guild_id"] != self.guild_id:
            raise RequestRejected("Avrae adapter request targets the wrong guild")
        return request


@dataclass
class QuartermasterOperationAdapter(QuartermasterStatusAdapter):
    """Verify and dispatch the bounded state-changing operations.

    The native provider must authorize the real actor and commit through
    Avrae's own combat model; Quartermaster only carries the request identity
    and records the outcome on its side.
    """

    provider: NativeOperationProvider

    async def handle(
        self,
        body: bytes,
        *,
        headers: Mapping[str, str],
        now: float | None = None,
    ) -> dict[str, Any]:
        request = self._validated_request(
            body,
            headers=headers,
            expected_protocol=AVRAE_OPERATION_PROTOCOL,
            expected_operation=None,
            now=now,
        )
        if request["operation_kind"] not in AVRAE_OPERATION_KINDS:
            raise RequestRejected("Avrae adapter does not permit this operation")
        try:
            payload = self.provider.execute_operation(request)
            if inspect.isawaitable(payload):
                payload = await payload
        except RequestRejected:
            raise
        except Exception:
            return {
                "status": "FAILED",
                "provider_reference": request["provider_reference"],
                "correlation_id": request["correlation_id"],
                "provider_version": "avrae-native-operation-v1",
                "payload": {},
                "error": "native Avrae operation is unavailable",
                "retryable": False,
            }
        if not isinstance(payload, Mapping):
            raise RequestRejected("native Avrae operation must return an object")
        return {
            "status": "COMMITTED",
            "provider_reference": request["provider_reference"],
            "correlation_id": request["correlation_id"],
            "provider_version": "avrae-native-operation-v1",
            "payload": dict(payload),
            "retryable": False,
        }


def _decode_request(body: bytes) -> dict[str, Any]:
    import json

    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestRejected("Avrae request body is not valid JSON") from error
    if not isinstance(value, dict):
        raise RequestRejected("Avrae request body must be an object")
    required = (
        "protocol",
        "operation_id",
        "provider",
        "operation_kind",
        "actor_id",
        "guild_id",
        "channel_id",
        "session_id",
        "correlation_id",
        "payload",
    )
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required[:-1]):
        raise RequestRejected("Avrae request identity fields are incomplete")
    if not isinstance(value.get("payload"), dict):
        raise RequestRejected("Avrae request payload must be an object")
    if value.get("provider_reference") is not None and not isinstance(value["provider_reference"], str):
        raise RequestRejected("Avrae provider reference must be a string")
    return value
