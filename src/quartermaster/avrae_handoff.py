"""Player-facing handoff cards for hosted Avrae deployments.

This is the safe fallback while Avrae remains a separate hosted bot: it makes
Quartermaster the place where a player starts the workflow, then directs the
player to run the native Avrae command in the same channel. It intentionally
does not create a provider operation, because no provider call has happened.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import SQLiteStore
from .integration import SUPPORTED_PROVIDER_OPERATIONS, ProviderIntegrationError


class AvraeHandoffError(RuntimeError):
    """Raised when a hosted-Avrae handoff cannot be prepared."""


@dataclass(frozen=True)
class AvraeHandoffCard:
    status: str
    operation_kind: str
    session_number: int | None
    channel_id: str
    command: str | None
    instruction: str

    def render(self) -> str:
        title = self.operation_kind.replace("_", " ").upper()
        if self.status == "NO_ACTIVE_SESSION":
            return "**AVRAE HANDOFF**\n\nNo active Quartermaster session. Start a session before entering combat."
        lines = [
            f"**AVRAE HANDOFF · {title}**",
            "",
            f"Quartermaster session `{self.session_number}` is active.",
            f"Use the same Discord channel: <#{self.channel_id}>",
            self.instruction,
        ]
        if self.command:
            lines.extend(["", f"`{self.command}`"])
        lines.extend(
            [
                "",
                "Quartermaster is providing the handoff; Avrae remains authoritative for the mechanics.",
            ]
        )
        return "\n".join(lines)


class AvraeHandoffService:
    """Build native Avrae command cards for the current active session."""

    _COMMANDS: dict[str, tuple[str | None, str]] = {
        "start": ("!i begin", "The DM starts the initiative tracker here."),
        "join": ("!i join", "Join the active initiative tracker with your active character."),
        "next": ("!i next", "Advance the active combatant after the current turn is complete."),
        "attack": ("!attack <attack name> -t <target name>", "Replace the placeholders with the native attack arguments."),
        "cast": ("!cast <spell name> -t <target name>", "Replace the placeholders with the native spell arguments."),
        "check": ("!check <skill>", "Replace `<skill>` with the skill to roll."),
        "save": ("!save <ability>", "Replace `<ability>` with the saving-throw ability."),
        "end": ("!i end", "Avrae will request confirmation before ending the combat."),
        "status": (None, "Read Avrae's pinned initiative summary in this channel."),
    }

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def build(self, operation_kind: str, *, channel_id: str) -> AvraeHandoffCard:
        operation_kind = operation_kind.strip().lower()
        if operation_kind not in SUPPORTED_PROVIDER_OPERATIONS:
            raise ProviderIntegrationError(f"unsupported provider operation: {operation_kind}")
        if not channel_id.strip():
            raise AvraeHandoffError("channel is required for an Avrae handoff")
        command, instruction = self._COMMANDS[operation_kind]
        with self.store.transaction(immediate=False) as connection:
            active = connection.execute(
                "SELECT session_number FROM sessions WHERE status = 'ACTIVE' ORDER BY session_number DESC LIMIT 1"
            ).fetchone()
        if active is None:
            return AvraeHandoffCard("NO_ACTIVE_SESSION", operation_kind, None, channel_id, None, "")
        return AvraeHandoffCard(
            "READY",
            operation_kind,
            int(active["session_number"]),
            channel_id,
            command,
            instruction,
        )


def render_avrae_handoff(store: SQLiteStore, operation_kind: str, *, channel_id: str) -> str:
    """Build and render one hosted-Avrae handoff without changing state."""
    return AvraeHandoffService(store).build(operation_kind, channel_id=channel_id).render()
