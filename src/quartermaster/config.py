"""Validated runtime configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


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
    backup_directory: Path = Path("backups")
    backup_off_device_directory: Path | None = None
    backup_retention_count: int = 7
    backup_interval_seconds: int = 86_400
    discord_surface_health_max_age_seconds: int = 300
    discord_client_id: str | None = None
    discord_client_secret: str | None = None
    api_bind: str = "127.0.0.1:8080"
    activity_origin: str | None = None
    session_token_seconds: int = 3600

    @classmethod
    def from_env(cls, environment: Mapping[str, str]) -> Settings:
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
            backup_directory=Path(environment.get("QM_BACKUP_DIRECTORY", "backups").strip() or "backups").expanduser(),
            backup_off_device_directory=_optional_path(environment.get("QM_BACKUP_OFF_DEVICE_DIRECTORY", "")),
            backup_retention_count=_positive_int(environment, "QM_BACKUP_RETENTION_COUNT", 7),
            backup_interval_seconds=_positive_int(environment, "QM_BACKUP_INTERVAL_SECONDS", 86_400),
            discord_surface_health_max_age_seconds=_positive_int(
                environment, "QM_DISCORD_SURFACE_HEALTH_MAX_AGE_SECONDS", 300
            ),
            discord_client_id=_optional_id(environment.get("QM_DISCORD_CLIENT_ID", ""), "QM_DISCORD_CLIENT_ID"),
            discord_client_secret=environment.get("QM_DISCORD_CLIENT_SECRET", "").strip() or None,
            api_bind=_bind(environment.get("QM_API_BIND", "")),
            activity_origin=_optional_origin(environment.get("QM_ACTIVITY_ORIGIN", "")),
            session_token_seconds=_positive_int(environment, "QM_SESSION_TOKEN_SECONDS", 3600),
        )

    def require_activity(self) -> tuple[str, str]:
        """The credentials the Activity's token exchange cannot run without.

        Kept off `require_discord_token` because the bot and the export CLI
        both have to keep starting for a table that has not enabled the
        Activity, and a required-everywhere secret is how that stops being
        true.
        """
        if not self.discord_client_id:
            raise ConfigurationError("QM_DISCORD_CLIENT_ID is required to serve the Activity")
        if not self.discord_client_secret:
            raise ConfigurationError("QM_DISCORD_CLIENT_SECRET is required to serve the Activity")
        return self.discord_client_id, self.discord_client_secret

    @property
    def activity_enabled(self) -> bool:
        return bool(self.discord_client_id and self.discord_client_secret)

    def require_discord_token(self) -> str:
        if not self.discord_token:
            raise ConfigurationError("QM_DISCORD_TOKEN is required to run the Discord bot")
        return self.discord_token

    def require_projection_channels(self) -> tuple[str, str]:
        if not self.party_inventory_channel_id:
            raise ConfigurationError("QM_PARTY_INVENTORY_CHANNEL_ID is required to run the Discord bot")
        if not self.session_log_channel_id:
            raise ConfigurationError("QM_SESSION_LOG_CHANNEL_ID is required to run the Discord bot")
        return self.party_inventory_channel_id, self.session_log_channel_id


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


def _bind(raw: str) -> str:
    value = raw.strip()
    if not value:
        return "127.0.0.1:8080"
    host, separator, port = value.rpartition(":")
    if not separator or not host or not port.isdigit() or not 0 < int(port) < 65_536:
        raise ConfigurationError("QM_API_BIND must be host:port")
    return value


def _optional_origin(raw: str) -> str | None:
    """The origin Discord's proxy loads the Activity from.

    Required to be https because the client runs inside Discord, which will not
    load a mixed-content frame, and an origin that only works in a local
    browser is a Stage 2 discovery rather than a Stage 6 one.
    """
    value = raw.strip().rstrip("/")
    if not value:
        return None
    if not value.startswith("https://") or len(value) <= len("https://"):
        raise ConfigurationError("QM_ACTIVITY_ORIGIN must be an https:// origin")
    return value


def _optional_path(raw: str) -> Path | None:
    value = raw.strip()
    return Path(value).expanduser() if value else None
