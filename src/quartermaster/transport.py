"""Transport boundary and deterministic fake used by tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class RateLimitedError(RuntimeError):
    """Transport rejected a request and supplied a retry delay."""

    def __init__(self, retry_after_seconds: float, message: str = "rate limited") -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class DiscordTransport:
    """Minimal interface the application needs from Discord."""

    def respond_initial(self, interaction_id: str, payload: Any) -> None:
        raise NotImplementedError

    def defer(self, interaction_id: str) -> None:
        raise NotImplementedError

    def follow_up(self, interaction_id: str, payload: Any) -> None:
        raise NotImplementedError

    def upsert_state(self, target_id: str, destination: str, payload: Any, message_id: str | None) -> str:
        raise NotImplementedError

    def deliver_event(self, destination: str, event_type: str, payload: Any) -> None:
        raise NotImplementedError


@dataclass
class FakeDiscordTransport(DiscordTransport):
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    state_messages: dict[str, str] = field(default_factory=dict)
    state_payloads: dict[str, Any] = field(default_factory=dict)
    event_deliveries: list[tuple[str, str, Any]] = field(default_factory=list)

    def respond_initial(self, interaction_id: str, payload: Any) -> None:
        self.calls.append(("respond_initial", interaction_id, payload))

    def defer(self, interaction_id: str) -> None:
        self.calls.append(("defer", interaction_id, None))

    def follow_up(self, interaction_id: str, payload: Any) -> None:
        self.calls.append(("follow_up", interaction_id, payload))

    def upsert_state(self, target_id: str, destination: str, payload: Any, message_id: str | None) -> str:
        resolved_message_id = message_id or f"message-{target_id}"
        self.state_messages[target_id] = resolved_message_id
        self.state_payloads[target_id] = payload
        self.calls.append(("upsert_state", destination, {"target_id": target_id, "payload": payload}))
        return resolved_message_id

    def deliver_event(self, destination: str, event_type: str, payload: Any) -> None:
        self.event_deliveries.append((destination, event_type, payload))
        self.calls.append(("deliver_event", destination, {"event_type": event_type, "payload": payload}))
