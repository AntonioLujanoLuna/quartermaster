"""Clock abstractions used to make expiry and recovery deterministic in tests."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")
