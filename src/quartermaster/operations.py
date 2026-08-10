"""Operational checks, transient-state maintenance, and backup validation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .clock import iso_now
from .db import SCHEMA_VERSION, SQLiteStore
from .export import render_export
from .handles import HandleRepository
from .loot import expire_due_drops
from .receipts import ReceiptRepository


def _record_maintenance(store: SQLiteStore, *, status: str, error: str | None = None) -> None:
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO maintenance_runs(name, last_run_at, last_status, last_error)
               VALUES ('transient-state', ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   last_run_at = excluded.last_run_at,
                   last_status = excluded.last_status,
                   last_error = excluded.last_error""",
            (iso_now(), status, error),
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


def health_report(store: SQLiteStore) -> dict[str, Any]:
    """Return a small, machine-readable health snapshot without contacting Discord."""
    connection = store._require_connection()
    integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    schema_version = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
    active_sessions = connection.execute("SELECT COUNT(*) FROM sessions WHERE status = 'ACTIVE'").fetchone()[0]
    processing_receipts = connection.execute(
        "SELECT COUNT(*) FROM interaction_receipts WHERE status = 'PROCESSING'"
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

    checks = {
        "database": "OK" if integrity == "ok" else "FAILED",
        "schema": "OK" if schema_version == SCHEMA_VERSION else "FAILED",
        "session_invariant": "OK" if active_sessions <= 1 else "FAILED",
        "processing_receipts": "OK" if processing_receipts == 0 else "DEGRADED",
        "event_outbox": "OK" if pending_events == 0 else "DEGRADED",
        "state_projections": "OK" if dirty_projections == 0 else "DEGRADED",
        "expired_drops": "OK" if due_drops == 0 else "DEGRADED",
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
            "pending_events": pending_events,
            "dirty_projections": dirty_projections,
            "expired_drops": due_drops,
        },
    }


def validate_backup(path: str | Path) -> dict[str, Any]:
    """Validate SQLite integrity, schema, and human-readable exportability."""
    backup_path = Path(path).expanduser()
    if not backup_path.is_file():
        raise FileNotFoundError(backup_path)
    connection = sqlite3.connect(str(backup_path))
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        schema_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        connection.close()
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    if schema_version != SCHEMA_VERSION:
        raise RuntimeError(f"backup schema version {schema_version} is not {SCHEMA_VERSION}")
    with SQLiteStore(backup_path).open() as restored:
        export = render_export(restored)
    return {"path": str(backup_path), "integrity": integrity, "schema_version": schema_version, "export_bytes": len(export.encode("utf-8"))}


def create_backup(store: SQLiteStore, destination: str | Path) -> dict[str, Any]:
    """Create and validate a consistent online SQLite backup."""
    target = store.snapshot(destination)
    return validate_backup(target)


def restore_backup(
    source: str | Path,
    destination: str | Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Restore a validated backup, refusing overwrite unless explicitly requested."""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("restore source and destination must differ")
    validate_backup(source_path)
    if destination_path.exists() and not replace:
        raise FileExistsError(f"restore destination exists: {destination_path}; pass replace=True to overwrite")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteStore(source_path).open() as source_store:
        source_store.snapshot(destination_path)
    return validate_backup(destination_path)


def render_health(report: dict[str, Any]) -> str:
    """Render health in a concise form suitable for an operator terminal."""
    lines = [f"Quartermaster health: {report['status']}", f"Schema: {report['schema_version']}/{report['expected_schema_version']}"]
    lines.extend(f"- {name}: {status}" for name, status in report["checks"].items())
    lines.append("Counts: " + json.dumps(report["counts"], sort_keys=True))
    return "\n".join(lines)
