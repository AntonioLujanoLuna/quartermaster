"""Operational checks, transient-state maintenance, and backup validation."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .clock import iso_now
from .db import MIGRATIONS, SCHEMA_VERSION, SQLiteStore
from .export import render_export
from .handles import HandleRepository
from .loot import expire_due_drops
from .receipts import ReceiptRepository


def _record_maintenance(
    store: SQLiteStore,
    *,
    name: str = "transient-state",
    status: str,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    serialized_details = json.dumps(details, sort_keys=True, separators=(",", ":")) if details is not None else None
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO maintenance_runs(name, last_run_at, last_status, last_error, last_details)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   last_run_at = excluded.last_run_at,
                   last_status = excluded.last_status,
                   last_error = excluded.last_error,
                   last_details = excluded.last_details""",
            (name, iso_now(), status, error, serialized_details),
        )


def run_maintenance(
    store: SQLiteStore,
    *,
    receipt_retention_seconds: int = 86_400,
    handle_retention_seconds: int = 600,
) -> dict[str, int]:
    """Expire drops and remove replay state that is past its retention window."""
    if receipt_retention_seconds <= 0 or handle_retention_seconds <= 0:
        raise ValueError("retention periods must be positive")
    try:
        expired_drops = expire_due_drops(store)
        removed_handles = HandleRepository(store).cleanup(replay_retention_seconds=handle_retention_seconds)
        removed_receipts = ReceiptRepository(store).cleanup_terminal(
            retention_seconds=receipt_retention_seconds
        )
        result = {
            "expired_drops": expired_drops,
            "removed_handles": removed_handles,
            "removed_receipts": removed_receipts,
        }
        _record_maintenance(store, status="OK")
        return result
    except Exception as error:
        _record_maintenance(store, status="FAILED", error=str(error))
        raise


def record_discord_surface_health(
    store: SQLiteStore,
    *,
    reachable: bool,
    details: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Record the most recent runtime reachability check for Discord surfaces."""
    _record_maintenance(
        store,
        name="discord-surfaces",
        status="OK" if reachable else "FAILED",
        error=error,
        details=details,
    )


def health_report(
    store: SQLiteStore,
    *,
    backup_max_age_seconds: int = 86_400,
    discord_surface_max_age_seconds: int = 300,
) -> dict[str, Any]:
    """Return a small, machine-readable health snapshot without contacting Discord."""
    if backup_max_age_seconds <= 0:
        raise ValueError("backup max age must be positive")
    if discord_surface_max_age_seconds <= 0:
        raise ValueError("Discord surface max age must be positive")
    with store.read() as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        schema_version = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        active_sessions = connection.execute("SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE'").fetchone()[0]
        processing_receipts = connection.execute(
            "SELECT COUNT(*) FROM interaction_receipts WHERE status = 'PROCESSING'"
        ).fetchone()[0]
        provider_operations_unknown = connection.execute(
            "SELECT COUNT(*) FROM provider_operations WHERE status = 'UNKNOWN'"
        ).fetchone()[0]
        provider_operations_requested = connection.execute(
            "SELECT COUNT(*) FROM provider_operations WHERE status = 'REQUESTED'"
        ).fetchone()[0]
        pending_events = connection.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE status = 'PENDING'"
        ).fetchone()[0]
        dirty_projections = connection.execute(
            "SELECT COUNT(*) FROM projection_targets WHERE dirty_since IS NOT NULL"
        ).fetchone()[0]
        due_drops = connection.execute(
            "SELECT COUNT(*) FROM loot_drops WHERE status = 'OPEN' AND expires_at <= ?", (iso_now(),)
        ).fetchone()[0]
        maintenance = connection.execute(
            "SELECT last_status FROM maintenance_runs WHERE name = 'transient-state'"
        ).fetchone()
        discord_surfaces = connection.execute(
            "SELECT last_run_at, last_status, last_details FROM maintenance_runs WHERE name = 'discord-surfaces'"
        ).fetchone()
        discord_surface_age = None
        if discord_surfaces is not None and discord_surfaces["last_run_at"] is not None:
            discord_surface_age = connection.execute(
                "SELECT (julianday(?) - julianday(?)) * 86400.0",
                (iso_now(), discord_surfaces["last_run_at"]),
            ).fetchone()[0]
        backup = connection.execute(
            "SELECT last_run_at, last_status, last_details FROM maintenance_runs WHERE name = 'backup'"
        ).fetchone()
        backup_age = None
        if backup is not None and backup["last_run_at"] is not None:
            backup_age = connection.execute(
                "SELECT (julianday(?) - julianday(?)) * 86400.0",
                (iso_now(), backup["last_run_at"]),
            ).fetchone()[0]
    discord_surfaces_ok = (
        discord_surfaces is not None
        and discord_surfaces["last_status"] == "OK"
        and discord_surface_age is not None
        and discord_surface_age <= discord_surface_max_age_seconds
    )
    backup_details: dict[str, Any] | None = None
    if backup is not None and backup["last_details"]:
        try:
            parsed_details = json.loads(backup["last_details"])
            if isinstance(parsed_details, dict):
                backup_details = parsed_details
        except json.JSONDecodeError:
            backup_details = None
    primary_backup_path = Path(backup_details["primary_path"]) if backup_details and backup_details.get("primary_path") else None
    off_device_backup_path = Path(backup_details["off_device_path"]) if backup_details and backup_details.get("off_device_path") else None
    primary_backup_exists = primary_backup_path is not None and primary_backup_path.is_file()
    off_device_backup_exists = off_device_backup_path is None or off_device_backup_path.is_file()
    backup_ok = (
        backup is not None
        and backup["last_status"] == "OK"
        and backup_age is not None
        and backup_age <= backup_max_age_seconds
        and primary_backup_exists
        and off_device_backup_exists
    )

    checks = {
        "database": "OK" if integrity == "ok" else "FAILED",
        "schema": "OK" if schema_version == SCHEMA_VERSION else "FAILED",
        "session_invariant": "OK" if active_sessions <= 1 else "FAILED",
        "processing_receipts": "OK" if processing_receipts == 0 else "DEGRADED",
        "provider_operations": "OK"
        if provider_operations_unknown == 0 and provider_operations_requested == 0
        else "DEGRADED",
        "event_outbox": "OK" if pending_events == 0 else "DEGRADED",
        "state_projections": "OK" if dirty_projections == 0 else "DEGRADED",
        "expired_drops": "OK" if due_drops == 0 else "DEGRADED",
        "maintenance": "OK" if maintenance is None or maintenance["last_status"] == "OK" else "DEGRADED",
        "backup": "OK" if backup_ok else "DEGRADED",
        "discord_surfaces": "OK" if discord_surfaces_ok else "DEGRADED",
    }
    status = "HEALTHY"
    if "FAILED" in checks.values():
        status = "FAILED"
    elif "DEGRADED" in checks.values():
        status = "DEGRADED"
    return {
        "status": status,
        "schema_version": schema_version,
        "expected_schema_version": SCHEMA_VERSION,
        "checks": checks,
        "counts": {
            "active_sessions": active_sessions,
            "processing_receipts": processing_receipts,
            "provider_operations_unknown": provider_operations_unknown,
            "provider_operations_requested": provider_operations_requested,
            "pending_events": pending_events,
            "dirty_projections": dirty_projections,
            "expired_drops": due_drops,
            "backup_age_seconds": backup_age,
            "backup_primary_exists": primary_backup_exists,
            "backup_off_device_exists": off_device_backup_exists,
            "discord_surface_age_seconds": discord_surface_age,
        },
    }


def _inspect_backup(path: Path) -> tuple[str, int]:
    """Read integrity and schema version without migrating the file being inspected."""
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(str(path))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        schema_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return integrity, int(schema_version)


def validate_backup(path: str | Path) -> dict[str, Any]:
    """Validate SQLite integrity, schema, and human-readable exportability."""
    backup_path = Path(path).expanduser()
    integrity, schema_version = _inspect_backup(backup_path)
    if schema_version != SCHEMA_VERSION:
        raise RuntimeError(f"backup schema version {schema_version} is not {SCHEMA_VERSION}")
    with SQLiteStore(backup_path).open() as restored:
        export = render_export(restored)
    return {"path": str(backup_path), "integrity": integrity, "schema_version": schema_version, "export_bytes": len(export.encode("utf-8"))}


def _copy_validated_snapshot(source: Path, destination_directory: Path) -> Path:
    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / source.name
    if destination.resolve() == source.resolve():
        raise ValueError("off-device backup directory must differ from the primary backup directory")
    shutil.copy2(source, destination)
    validate_backup(destination)
    return destination


def _apply_backup_retention(directory: Path, *, keep: int, protected: set[Path]) -> list[str]:
    if keep <= 0:
        raise ValueError("backup retention count must be positive")
    resolved_directory = directory.resolve()
    candidates = sorted(
        (path for path in resolved_directory.glob("quartermaster-*.sqlite") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    retained = set(candidates[:keep]) | {path.resolve() for path in protected}
    removed: list[str] = []
    for path in candidates:
        if path.resolve() in retained:
            continue
        path.unlink()
        removed.append(str(path.resolve()))
    return removed


def create_backup(
    store: SQLiteStore,
    destination: str | Path,
    *,
    off_device_directory: str | Path | None = None,
    retention_count: int = 7,
) -> dict[str, Any]:
    """Create, validate, copy, and retain a consistent online SQLite backup."""
    destination_path = Path(destination).expanduser()
    if retention_count <= 0:
        raise ValueError("backup retention count must be positive")
    off_device_path = Path(off_device_directory).expanduser() if off_device_directory is not None else None
    if off_device_path is not None and off_device_path.resolve() == destination_path.parent.resolve():
        raise ValueError("off-device backup directory must differ from the primary backup directory")
    try:
        target = store.snapshot(destination_path)
        result = validate_backup(target)
        copied_target = _copy_validated_snapshot(target, off_device_path) if off_device_path is not None else None
        removed_primary = _apply_backup_retention(
            target.parent,
            keep=retention_count,
            protected={target},
        )
        removed_off_device = (
            _apply_backup_retention(copied_target.parent, keep=retention_count, protected={copied_target})
            if copied_target is not None
            else []
        )
        details = {
            "primary_path": str(target.resolve()),
            "off_device_path": str(copied_target.resolve()) if copied_target is not None else None,
            "retention_count": retention_count,
            "removed_primary_paths": removed_primary,
            "removed_off_device_paths": removed_off_device,
        }
        result.update(details)
        _record_maintenance(store, name="backup", status="OK", details=details)
        return result
    except Exception as error:
        _record_maintenance(
            store,
            name="backup",
            status="FAILED",
            error=str(error),
            details={"primary_path": str(destination_path.resolve())},
        )
        raise


def create_scheduled_backup(
    store: SQLiteStore,
    directory: str | Path,
    *,
    off_device_directory: str | Path | None = None,
    retention_count: int = 7,
) -> dict[str, Any]:
    """Create a timestamped backup using the same validated path as the CLI."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%SZ")
    destination = Path(directory).expanduser() / f"quartermaster-{timestamp}.sqlite"
    return create_backup(
        store,
        destination,
        off_device_directory=off_device_directory,
        retention_count=retention_count,
    )


def restore_backup(
    source: str | Path,
    destination: str | Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Restore a backup, migrating an older snapshot forward and never mutating the source.

    A backup taken before a later migration is still restorable: it is copied first
    and brought up to the current schema in the copy, so the archived file on disk
    keeps the schema it was taken at.
    """
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("restore source and destination must differ")
    _, source_schema_version = _inspect_backup(source_path)
    if source_schema_version not in MIGRATIONS:
        raise RuntimeError(
            f"backup schema version {source_schema_version} is not a supported version "
            f"(this build understands up to {SCHEMA_VERSION})"
        )
    if destination_path.exists() and not replace:
        raise FileExistsError(f"restore destination exists: {destination_path}; pass replace=True to overwrite")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path = destination_path.with_name(destination_path.name + ".restore-staging")
    if staging_path.exists():
        staging_path.unlink()
    shutil.copy2(source_path, staging_path)
    try:
        # Opening the copy applies any migrations the snapshot predates.
        with SQLiteStore(staging_path).open() as staged:
            staged.snapshot(destination_path)
    finally:
        staging_path.unlink(missing_ok=True)
    result = validate_backup(destination_path)
    result["source_schema_version"] = source_schema_version
    return result


def render_health(report: dict[str, Any]) -> str:
    """Render health in a concise form suitable for an operator terminal."""
    lines = [f"Quartermaster health: {report['status']}", f"Schema: {report['schema_version']}/{report['expected_schema_version']}"]
    lines.extend(f"- {name}: {status}" for name, status in report["checks"].items())
    lines.append("Counts: " + json.dumps(report["counts"], sort_keys=True))
    return "\n".join(lines)
