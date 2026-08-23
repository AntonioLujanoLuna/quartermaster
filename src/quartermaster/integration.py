"""Durable boundaries for provider-backed gameplay operations.

Quartermaster records the intent and outcome of a provider call, but it does
not calculate or mirror the provider's game mechanics. The first provider
adapter is now an optional read-only status transport. State-changing calls
still require an Avrae-owned extension or service where the real Discord actor
and combat context are available.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .clock import iso_now
from .db import SQLiteStore
from .receipts import ReceiptRepository, ReceiptResult

SUPPORTED_PROVIDER_OPERATIONS = frozenset({"start", "join", "next", "attack", "cast", "check", "save", "end", "status"})
PROVIDER_STATUSES = frozenset({"COMMITTED", "FAILED", "UNKNOWN"})


class ProviderIntegrationError(RuntimeError):
    """Raised when a provider request or outcome cannot be represented safely."""


class ProviderTimeout(ProviderIntegrationError):
    """Raised when the provider outcome is not known after the request was sent."""


@dataclass(frozen=True)
class ProviderRequest:
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


@dataclass(frozen=True)
class AvraeInteractionContext:
    """Identity copied from a real Avrae interaction, never inferred by Qm."""

    actor_id: str
    guild_id: str
    channel_id: str
    session_id: str
    provider_reference: str

    @classmethod
    def from_interaction(cls, interaction: Any, *, session_id: str) -> AvraeInteractionContext:
        author = getattr(interaction, "author", None) or getattr(interaction, "user", None)
        guild = getattr(interaction, "guild", None)
        channel = getattr(interaction, "channel", None)
        actor_id = getattr(author, "id", None)
        guild_id = getattr(guild, "id", None)
        channel_id = getattr(channel, "id", None)
        if actor_id is None or guild_id is None or channel_id is None or not session_id.strip():
            raise ProviderIntegrationError("Avrae interaction must include actor, guild, channel, and session")
        return cls(
            actor_id=str(actor_id),
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            session_id=session_id,
            provider_reference=f"channel:{channel_id}",
        )

    def begin_kwargs(self) -> dict[str, str]:
        return {
            "actor_id": self.actor_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "session_id": self.session_id,
            "provider_reference": self.provider_reference,
        }


@dataclass(frozen=True)
class ProviderResult:
    status: str
    provider_reference: str | None = None
    correlation_id: str | None = None
    provider_version: str | None = None
    payload: Mapping[str, Any] | None = None
    error: str | None = None
    retryable: bool = False

    def validate(self) -> ProviderResult:
        if self.status not in PROVIDER_STATUSES:
            raise ProviderIntegrationError(f"unsupported provider result status: {self.status}")
        if self.payload is not None and not isinstance(self.payload, Mapping):
            raise ProviderIntegrationError("provider result payload must be an object")
        return self


class AvraeGateway(Protocol):
    """The seam implemented by the Avrae-side extension or local RPC adapter."""

    def execute(self, request: ProviderRequest) -> ProviderResult:
        """Execute one authenticated operation through Avrae's own command/model path."""


@dataclass(frozen=True)
class ProviderExecution:
    receipt: ReceiptResult
    request: ProviderRequest


class ProviderIntegrationService:
    """Persist and finalize one provider operation with its interaction receipt."""

    def __init__(
        self,
        store: SQLiteStore,
        receipts: ReceiptRepository,
        *,
        provider: str = "avrae",
        integration_version: str = "qm-provider-contract-v1",
    ) -> None:
        if not provider.strip():
            raise ValueError("provider must not be empty")
        if not integration_version.strip():
            raise ValueError("integration_version must not be empty")
        self.store = store
        self.receipts = receipts
        self.provider = provider.strip().lower()
        self.integration_version = integration_version.strip()

    def begin(
        self,
        interaction_id: str,
        *,
        actor_id: str,
        guild_id: str,
        channel_id: str,
        session_id: str,
        operation_kind: str,
        payload: Mapping[str, Any] | None = None,
        provider_reference: str | None = None,
    ) -> ProviderExecution:
        self._require_context(actor_id, guild_id, channel_id, session_id)
        operation_kind = operation_kind.strip().lower()
        if operation_kind not in SUPPORTED_PROVIDER_OPERATIONS:
            raise ProviderIntegrationError(f"unsupported provider operation: {operation_kind}")
        request_payload = dict(payload or {})
        serialized_payload = self._serialize(request_payload)

        def prepare(connection: Any, operation_id: str) -> None:
            now = iso_now()
            correlation_id = f"qm:{operation_id}"
            connection.execute(
                """INSERT INTO provider_operations(
                    operation_id, interaction_id, provider, operation_kind,
                    actor_id, guild_id, channel_id, session_id, provider_reference,
                    integration_version, status, correlation_id, request_payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'REQUESTED', ?, ?, ?, ?)""",
                (
                    operation_id,
                    interaction_id,
                    self.provider,
                    operation_kind,
                    actor_id,
                    guild_id,
                    channel_id,
                    session_id,
                    provider_reference,
                    self.integration_version,
                    correlation_id,
                    serialized_payload,
                    now,
                    now,
                ),
            )

        receipt = self.receipts.begin_deferred(
            interaction_id,
            actor_id=actor_id,
            response_kind=f"provider:{self.provider}:{operation_kind}",
            prepare=prepare,
        )
        request = self._request_for_operation(receipt.operation_id, fallback={
            "operation_id": receipt.operation_id,
            "provider": self.provider,
            "operation_kind": operation_kind,
            "actor_id": actor_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "session_id": session_id,
            "provider_reference": provider_reference,
            "correlation_id": f"qm:{receipt.operation_id}",
            "payload": request_payload,
        })
        return ProviderExecution(receipt, request)

    def execute(self, execution: ProviderExecution, gateway: AvraeGateway) -> ReceiptResult:
        """Execute only a new PROCESSING request, then persist the exact outcome."""
        if execution.receipt.status != "PROCESSING":
            return execution.receipt
        try:
            result = gateway.execute(execution.request).validate()
        except ProviderTimeout as error:
            return self.unknown(execution, str(error) or "provider outcome is unknown")
        except Exception as error:
            return self.failed(execution, str(error) or "provider operation failed")
        return self._finish(execution, result)

    def committed(self, execution: ProviderExecution, result: ProviderResult) -> ReceiptResult:
        result.validate()
        if result.status != "COMMITTED":
            raise ProviderIntegrationError("committed() requires a COMMITTED provider result")
        return self._finish(execution, result)

    def failed(self, execution: ProviderExecution, message: str, *, retryable: bool = True) -> ReceiptResult:
        return self._finish(
            execution,
            ProviderResult(status="FAILED", error=message, retryable=retryable),
        )

    def unknown(self, execution: ProviderExecution, message: str) -> ReceiptResult:
        return self._finish(
            execution,
            ProviderResult(status="UNKNOWN", error=message, retryable=False),
        )

    def _finish(self, execution: ProviderExecution, result: ProviderResult) -> ReceiptResult:
        result.validate()
        logical_response: dict[str, Any] = {
            "status": result.status,
            "operation_id": execution.request.operation_id,
            "provider": execution.request.provider,
            "correlation_id": result.correlation_id or execution.request.correlation_id,
            "provider_reference": result.provider_reference or execution.request.provider_reference,
            "provider_version": result.provider_version,
            "result": dict(result.payload or {}),
            "retryable": result.retryable,
        }
        if result.error:
            logical_response["error"] = result.error

        def finalize(connection: Any, operation_id: str, _receipt_status: str, response: Any) -> None:
            now = iso_now()
            connection.execute(
                """UPDATE provider_operations
                   SET status = ?, provider_reference = ?, provider_version = ?,
                       result_payload = ?, updated_at = ?
                   WHERE operation_id = ? AND status = 'REQUESTED'""",
                (
                    result.status,
                    logical_response["provider_reference"],
                    result.provider_version,
                    json.dumps(response, sort_keys=True, separators=(",", ":")),
                    now,
                    operation_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise ProviderIntegrationError(f"provider operation {operation_id} is not REQUESTED")

        if result.status == "COMMITTED":
            return self.receipts.commit_deferred(execution.receipt.interaction_id, logical_response, finalize=finalize)
        return self.receipts.fail_deferred(execution.receipt.interaction_id, logical_response, finalize=finalize)

    def _request_for_operation(self, operation_id: str, *, fallback: Mapping[str, Any]) -> ProviderRequest:
        with self.store.transaction(immediate=False) as connection:
            row = connection.execute(
                """SELECT operation_id, provider, operation_kind, actor_id, guild_id,
                          channel_id, session_id, provider_reference, correlation_id,
                          request_payload
                   FROM provider_operations WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
        values: Mapping[str, Any] = dict(row) if row is not None else fallback
        try:
            payload = json.loads(values["request_payload"] if row is not None else json.dumps(values["payload"]))
        except (KeyError, json.JSONDecodeError) as error:
            raise ProviderIntegrationError(f"provider operation {operation_id} has invalid request data") from error
        if not isinstance(payload, dict):
            raise ProviderIntegrationError(f"provider operation {operation_id} payload is not an object")
        return ProviderRequest(
            operation_id=str(values["operation_id"]),
            provider=str(values["provider"]),
            operation_kind=str(values["operation_kind"]),
            actor_id=str(values["actor_id"]),
            guild_id=str(values["guild_id"]),
            channel_id=str(values["channel_id"]),
            session_id=str(values["session_id"]),
            provider_reference=values.get("provider_reference"),
            correlation_id=str(values["correlation_id"]),
            payload=payload,
        )

    @staticmethod
    def _serialize(payload: Mapping[str, Any]) -> str:
        try:
            return json.dumps(payload, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ProviderIntegrationError("provider request payload must be JSON-serializable") from error

    @staticmethod
    def _require_context(*values: str) -> None:
        if any(not value or not value.strip() for value in values):
            raise ProviderIntegrationError("actor, guild, channel, and session are required")
