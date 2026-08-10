"""Validated runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


def _required_id(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    if not value.isdigit() or int(value) <= 0:
        raise ConfigurationError(f"{name} must be a positive numeric Discord ID")
    return value


@dataclass(frozen=True)
class Settings:
    guild_id: str
    database_path: Path
    discord_token: str | None = None
    dm_role_ids: tuple[str, ...] = ()
    party_inventory_channel_id: str | None = None
    session_log_channel_id: str | None = None
    dm_channel_id: str | None = None
    soft_deadline_seconds: float = 1.2
    internal_hard_deadline_seconds: float = 2.5
    receipt_retention_seconds: int = 86_400
    handle_retention_seconds: int = 600

    @classmethod
    def from_env(cls, environment: Mapping[str, str]) -> "Settings":
        guild_id = _required_id(environment, "QM_GUILD_ID")
        raw_path = environment.get("QM_DATABASE_PATH", "").strip()
        if not raw_path:
            raise ConfigurationError("QM_DATABASE_PATH is required")
        path = Path(raw_path).expanduser()
        if path.name in {"", ".", ".."}:
            raise ConfigurationError("QM_DATABASE_PATH must name a database file")
        return cls(
            guild_id=guild_id,
            database_path=path,
            discord_token=environment.get("QM_DISCORD_TOKEN", "").strip() or None,
            dm_role_ids=_id_list(environment.get("QM_DM_ROLE_IDS", ""), "QM_DM_ROLE_IDS"),
            party_inventory_channel_id=_optional_id(environment.get("QM_PARTY_INVENTORY_CHANNEL_ID", ""), "QM_PARTY_INVENTORY_CHANNEL_ID"),
            session_log_channel_id=_optional_id(environment.get("QM_SESSION_LOG_CHANNEL_ID", ""), "QM_SESSION_LOG_CHANNEL_ID"),
            dm_channel_id=_optional_id(environment.get("QM_DM_CHANNEL_ID", ""), "QM_DM_CHANNEL_ID"),
            soft_deadline_seconds=_positive_float(environment, "QM_SOFT_DEADLINE_SECONDS", 1.2),
            internal_hard_deadline_seconds=_positive_float(
                environment, "QM_INTERNAL_HARD_DEADLINE_SECONDS", 2.5
            ),
            receipt_retention_seconds=_positive_int(environment, "QM_RECEIPT_RETENTION_SECONDS", 86_400),
            handle_retention_seconds=_positive_int(environment, "QM_HANDLE_RETENTION_SECONDS", 600),
        )

    def require_discord_token(self) -> str:
        if not self.discord_token:
            raise ConfigurationError("QM_DISCORD_TOKEN is required to run the Discord bot")
        return self.discord_token


def _positive_float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw = environment.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive number")
    return value


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _id_list(raw: str, name: str) -> tuple[str, ...]:
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if any(not value.isdigit() or int(value) <= 0 for value in values):
        raise ConfigurationError(f"{name} must contain only positive numeric Discord IDs")
    return values


def _optional_id(raw: str, name: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise ConfigurationError(f"{name} must be a positive numeric Discord ID")
    return value
