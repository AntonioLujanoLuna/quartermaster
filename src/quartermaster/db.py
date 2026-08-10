"""SQLite connection, migration, and transaction primitives."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 4


class MigrationError(RuntimeError):
    """Raised when the canonical schema cannot be brought to a supported version."""


MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE interaction_receipts (
        interaction_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        actor_id TEXT,
        execution_class TEXT NOT NULL CHECK (execution_class IN ('FAST', 'DEFERRED', 'IMMEDIATE_UI')),
        status TEXT NOT NULL CHECK (status IN ('PROCESSING', 'COMMITTED', 'FAILED')),
        response_kind TEXT NOT NULL,
        logical_response TEXT NOT NULL,
        serialized_response TEXT,
        created_at TEXT NOT NULL,
        committed_at TEXT,
        failed_at TEXT
    );
    CREATE INDEX interaction_receipts_status_idx ON interaction_receipts(status);
    CREATE INDEX interaction_receipts_created_idx ON interaction_receipts(created_at);

    CREATE TABLE interaction_handles (
        id TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        workflow_type TEXT NOT NULL,
        action TEXT NOT NULL,
        actor_id TEXT,
        payload TEXT NOT NULL,
        read_set_snapshot TEXT NOT NULL,
        single_use INTEGER NOT NULL CHECK (single_use IN (0, 1)),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        consumed_at TEXT
    );
    CREATE INDEX interaction_handles_expiry_idx ON interaction_handles(expires_at);
    CREATE INDEX interaction_handles_consumed_idx ON interaction_handles(consumed_at);

    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        session_number INTEGER NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
        started_at TEXT NOT NULL,
        ended_at TEXT,
        where_ended TEXT
    );
    CREATE UNIQUE INDEX one_active_session_idx ON sessions(status) WHERE status = 'ACTIVE';

    CREATE TABLE inventory_stacks (
        id TEXT PRIMARY KEY,
        item_name TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK (quantity >= 0),
        provenance TEXT,
        owner_type TEXT NOT NULL CHECK (owner_type IN ('PARTY', 'CHARACTER')),
        owner_id TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX inventory_stacks_owner_idx ON inventory_stacks(owner_type, owner_id);

    CREATE TABLE ledger_entries (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL,
        actor_id TEXT,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE domain_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE event_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        destination TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'DELIVERED', 'FAILED')),
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error TEXT,
        delivered_at TEXT
    );
    CREATE INDEX event_outbox_delivery_idx ON event_outbox(destination, status, id);

    CREATE TABLE projection_targets (
        target_id TEXT PRIMARY KEY,
        target_type TEXT NOT NULL CHECK (target_type IN ('STATE', 'EVENT')),
        destination TEXT NOT NULL,
        last_rendered_version INTEGER NOT NULL DEFAULT 0,
        dirty_since TEXT,
        last_error TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE maintenance_runs (
        name TEXT PRIMARY KEY,
        last_run_at TEXT,
        last_status TEXT,
        last_error TEXT
    );
    """,
    2: """
    ALTER TABLE inventory_stacks ADD COLUMN normalized_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE inventory_stacks ADD COLUMN variant_metadata TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE inventory_stacks ADD COLUMN notes TEXT;
    ALTER TABLE inventory_stacks ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE inventory_stacks ADD COLUMN last_acquired_at TEXT;
    UPDATE inventory_stacks
       SET normalized_name = lower(trim(item_name)),
           variant_metadata = '{}',
           version = 1
     WHERE normalized_name = '';
    CREATE UNIQUE INDEX inventory_stack_identity_idx
        ON inventory_stacks(owner_type, owner_id, normalized_name, variant_metadata);

    CREATE TABLE characters (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        discord_user_id TEXT,
        lifecycle TEXT NOT NULL CHECK (lifecycle IN ('ACTIVE', 'DEAD', 'RETIRED', 'DEPARTED')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE loot_drops (
        id TEXT PRIMARY KEY,
        session_id TEXT,
        status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
        expires_at TEXT NOT NULL,
        closed_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX loot_drops_status_idx ON loot_drops(status, expires_at);
    """,
    3: """
    ALTER TABLE projection_targets ADD COLUMN discord_message_id TEXT;
    ALTER TABLE projection_targets ADD COLUMN desired_revision INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projection_targets ADD COLUMN delivered_revision INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projection_targets ADD COLUMN freshness_budget_seconds REAL NOT NULL DEFAULT 5.0;
    ALTER TABLE projection_targets ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projection_targets ADD COLUMN in_flight INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projection_targets ADD COLUMN next_attempt_at TEXT;
    CREATE INDEX projection_targets_schedule_idx
        ON projection_targets(target_type, dirty_since, next_attempt_at, in_flight);
    """,
    4: """
    CREATE TABLE loot_drop_items (
        id TEXT PRIMARY KEY,
        drop_id TEXT NOT NULL REFERENCES loot_drops(id),
        item_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        remaining_quantity INTEGER NOT NULL CHECK (remaining_quantity >= 0 AND remaining_quantity <= quantity),
        provenance TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX loot_drop_items_drop_idx ON loot_drop_items(drop_id, remaining_quantity);
    """,
}


class SQLiteStore:
    def __init__(self, path: str | Path, *, uri: bool = False) -> None:
        self.path = str(path)
        self.uri = uri
        self.connection: sqlite3.Connection | None = None

    def open(self) -> "SQLiteStore":
        if self.connection is not None:
            return self
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            uri=self.uri,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA busy_timeout = 2500")
        self.apply_migrations()
        return self

    def close(self) -> None:
        if self.connection is not None:
            # Checkpoint before closing so Windows can remove WAL sidecars
            # promptly during maintenance, tests, and clean shutdown.
            try:
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.connection.close()
            self.connection = None

    def __enter__(self) -> "SQLiteStore":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("SQLiteStore is not open")
        return self.connection

    def apply_migrations(self) -> None:
        connection = self._require_connection()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            supported = set(MIGRATIONS)
            if applied - supported:
                raise MigrationError(f"unsupported schema versions: {sorted(applied - supported)}")
            for version in sorted(supported - applied):
                script = f"""
                BEGIN IMMEDIATE;
                {MIGRATIONS[version]}
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ({version}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
                COMMIT;
                """
                try:
                    connection.executescript(script)
                except sqlite3.Error:
                    connection.rollback()
                    raise
            current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            if current != SCHEMA_VERSION:
                raise MigrationError(f"schema version {current} is not supported target {SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise MigrationError("schema migration failed") from exc

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def snapshot(self, destination: str | Path) -> Path:
        """Create a consistent SQLite backup without copying a live file."""
        target = Path(destination).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        backup_connection = sqlite3.connect(str(target))
        try:
            self._require_connection().backup(backup_connection)
            backup_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            backup_connection.execute("PRAGMA journal_mode = DELETE")
        finally:
            backup_connection.close()
        check = sqlite3.connect(str(target))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise MigrationError(f"backup integrity check failed: {result}")
        finally:
            check.close()
        return target
