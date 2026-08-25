"""SQLite connection, migration, and transaction primitives."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from .naming import normalize_name

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 15


class MigrationError(RuntimeError):
    """Raised when the canonical schema cannot be brought to a supported version."""


def _split_statements(script: str) -> list[str]:
    """Split a migration script into individual statements.

    `sqlite3.complete_statement` tracks string literals, so this is safe for
    scripts containing semicolons inside quotes, unlike splitting on ';'.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    tail = buffer.strip()
    if tail:
        statements.append(tail)
    return statements


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
    5: """
    ALTER TABLE sessions ADD COLUMN discord_thread_id TEXT;
    """,
    6: """
    ALTER TABLE maintenance_runs ADD COLUMN last_details TEXT;
    """,
    7: """
    CREATE TABLE currency_balances (
        owner_type TEXT NOT NULL CHECK (owner_type IN ('PARTY', 'CHARACTER')),
        owner_id TEXT NOT NULL,
        cp INTEGER NOT NULL DEFAULT 0 CHECK (cp >= 0),
        sp INTEGER NOT NULL DEFAULT 0 CHECK (sp >= 0),
        ep INTEGER NOT NULL DEFAULT 0 CHECK (ep >= 0),
        gp INTEGER NOT NULL DEFAULT 0 CHECK (gp >= 0),
        pp INTEGER NOT NULL DEFAULT 0 CHECK (pp >= 0),
        version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (owner_type, owner_id)
    );
    INSERT INTO currency_balances(owner_type, owner_id, updated_at)
    VALUES ('PARTY', 'party', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
    """,
    8: """
    CREATE TABLE local_metric_buckets (
        bucket_start TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        dimension TEXT NOT NULL DEFAULT '',
        sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
        total_ms REAL NOT NULL CHECK (total_ms >= 0),
        max_ms REAL NOT NULL CHECK (max_ms >= 0),
        histogram TEXT NOT NULL,
        PRIMARY KEY (bucket_start, metric_name, dimension)
    );
    CREATE INDEX local_metric_buckets_window_idx
        ON local_metric_buckets(metric_name, dimension, bucket_start);
    """,
    9: """
    CREATE TABLE provider_operations (
        operation_id TEXT PRIMARY KEY,
        interaction_id TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        operation_kind TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        provider_reference TEXT,
        provider_version TEXT,
        integration_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('REQUESTED', 'COMMITTED', 'FAILED', 'UNKNOWN')),
        correlation_id TEXT NOT NULL UNIQUE,
        request_payload TEXT NOT NULL,
        result_payload TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX provider_operations_status_idx
        ON provider_operations(provider, status, updated_at);
    CREATE INDEX provider_operations_context_idx
        ON provider_operations(session_id, guild_id, channel_id, updated_at);
    """,
    10: """
    CREATE UNIQUE INDEX one_active_character_per_user_idx
        ON characters(discord_user_id)
     WHERE lifecycle = 'ACTIVE' AND discord_user_id IS NOT NULL;

    ALTER TABLE event_outbox ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE event_outbox ADD COLUMN failed_at TEXT;
    CREATE INDEX event_outbox_failed_idx ON event_outbox(status, destination, id);
    """,
    11: """
    ALTER TABLE projection_targets ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
    """,
    # A combat encounter records that a fight happened, where, and for how long.
    # It deliberately has no column for HP, initiative, conditions, resources, or
    # combatants: Avrae owns every one of those, and a column here would become a
    # second authoritative copy the moment someone wrote to it.
    12: """
    CREATE TABLE combat_encounters (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        channel_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
        opened_by TEXT,
        opened_at TEXT NOT NULL,
        closed_by TEXT,
        closed_at TEXT,
        closed_reason TEXT,
        outcome TEXT
    );
    CREATE UNIQUE INDEX one_open_combat_per_session_idx
        ON combat_encounters(session_id) WHERE status = 'OPEN';
    CREATE INDEX combat_encounters_session_idx
        ON combat_encounters(session_id, status, opened_at);
    """,
    13: """
    CREATE TABLE character_dossiers (
        id TEXT PRIMARY KEY,
        character_id TEXT NOT NULL UNIQUE REFERENCES characters(id),
        snapshot_version INTEGER NOT NULL CHECK (snapshot_version > 0),
        source TEXT NOT NULL,
        source_reference TEXT,
        system TEXT NOT NULL,
        rules_version TEXT NOT NULL,
        level INTEGER CHECK (level IS NULL OR (level >= 1 AND level <= 30)),
        proficiency_bonus INTEGER CHECK (proficiency_bonus IS NULL OR (proficiency_bonus >= 0 AND proficiency_bonus <= 20)),
        ability_scores TEXT NOT NULL,
        ability_modifiers TEXT NOT NULL,
        armor_class INTEGER CHECK (armor_class IS NULL OR armor_class >= 0),
        hit_points INTEGER CHECK (hit_points IS NULL OR hit_points >= 0),
        temporary_hit_points INTEGER NOT NULL DEFAULT 0 CHECK (temporary_hit_points >= 0),
        initiative INTEGER,
        saving_throws TEXT NOT NULL,
        spell_attack_modifier INTEGER,
        spell_save_dc INTEGER,
        spell_resources TEXT NOT NULL,
        equipped TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        source_freshness TEXT NOT NULL CHECK (source_freshness IN ('CURRENT', 'STALE')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX character_dossiers_freshness_idx
        ON character_dossiers(source_freshness, observed_at);
    """,
    14: """
    -- Where the table stopped is one sentence a DM types. The other half of
    -- "where we stopped" is the recording, when the table makes one, and it
    -- was going in a channel message that scrolls away. Nullable and with no
    -- default: a table that records nothing is the normal case.
    ALTER TABLE sessions ADD COLUMN recording_url TEXT;
    """,
    15: """
    -- How far the outbound relay has got. One row, because there is one
    -- ledger and one place it is being copied to.
    --
    -- Deliberately not an outbox: `domain_events` already is one, with a
    -- monotonic cursor assigned inside the transaction that made the change.
    -- A second queue would be a second copy of the same history, and a table
    -- that has configured no webhook would accumulate rows nothing consumes.
    CREATE TABLE webhook_cursor (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        delivered_sequence INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
}


def _renormalize_inventory_names(connection: sqlite3.Connection) -> None:
    """Bring pre-existing stacks onto the runtime's normalization rule.

    Migration 2 backfilled `lower(trim(item_name))`, which is ASCII-only and
    leaves internal whitespace intact, so rows written before it could disagree
    with `normalize_name` and split into duplicate stacks. Recompute the rule in
    Python and merge any stacks that collide once they agree.
    """
    rows = connection.execute(
        """SELECT id, owner_type, owner_id, item_name, normalized_name, variant_metadata,
                  quantity, last_acquired_at
             FROM inventory_stacks
            ORDER BY id"""
    ).fetchall()
    groups: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        try:
            target = normalize_name(row["item_name"])
        except (AttributeError, TypeError):
            continue
        if not target:
            continue
        key = (row["owner_type"], row["owner_id"], target, row["variant_metadata"])
        groups.setdefault(key, []).append(row)

    for (_owner_type, _owner_id, target, _metadata), members in groups.items():
        if len(members) == 1 and members[0]["normalized_name"] == target:
            continue
        # Prefer the stack that already carries the correct identity, then the
        # most recently acquired, so the survivor keeps the freshest metadata.
        winner = max(
            members,
            key=lambda row: (
                row["normalized_name"] == target,
                row["last_acquired_at"] or "",
                row["id"],
            ),
        )
        merged_quantity = sum(int(row["quantity"]) for row in members)
        for row in members:
            if row["id"] != winner["id"]:
                connection.execute("DELETE FROM inventory_stacks WHERE id = ?", (row["id"],))
        connection.execute(
            """UPDATE inventory_stacks
                  SET normalized_name = ?, quantity = ?, version = version + 1
                WHERE id = ?""",
            (target, merged_quantity, winner["id"]),
        )

    for row in connection.execute("SELECT id, item_name FROM loot_drop_items").fetchall():
        try:
            target = normalize_name(row["item_name"])
        except (AttributeError, TypeError):
            continue
        if target:
            connection.execute(
                "UPDATE loot_drop_items SET normalized_name = ? WHERE id = ? AND normalized_name <> ?",
                (target, row["id"], target),
            )


# Data fixes that cannot be expressed in SQLite's dialect, applied in the same
# transaction as the schema migration of the same version.
DATA_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    10: _renormalize_inventory_names,
}


class SQLiteStore:
    def __init__(self, path: str | Path, *, uri: bool = False) -> None:
        self.path = str(path)
        self.uri = uri
        self.connection: sqlite3.Connection | None = None
        self.connection_lock = RLock()
        self._commit_listeners: list[Callable[[], None]] = []

    def add_commit_listener(self, listener: Callable[[], None]) -> None:
        """Be told that a write transaction committed.

        The Activity's live feed needs to know when `domain_events` grew, and
        the alternative is polling the sequence on a timer — one read per tick
        against the single connection the gateway shares, for the whole time
        the table is idle, which is the contention `expire_due_drops` already
        had to stop causing.

        A listener is told *that* something committed and nothing about what:
        the store stays a store. It runs on whichever thread committed, after
        the connection lock is released, so it must not block and must not
        write. A listener that raises is not allowed to fail the commit that
        has already happened.
        """
        with self.connection_lock:
            self._commit_listeners.append(listener)

    def remove_commit_listener(self, listener: Callable[[], None]) -> None:
        with self.connection_lock:
            if listener in self._commit_listeners:
                self._commit_listeners.remove(listener)

    def _announce_commit(self) -> None:
        with self.connection_lock:
            listeners = tuple(self._commit_listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.exception("a commit listener failed")

    def open(self) -> SQLiteStore:
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
        try:
            self.apply_migrations()
        except BaseException:
            # A failed migration can happen before the caller owns the store
            # and therefore cannot call close(). Release the connection here,
            # especially on Windows where the open handle prevents cleanup of
            # the database and its WAL sidecars.
            connection = self.connection
            self.connection = None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise
        return self

    def close(self) -> None:
        with self.connection_lock:
            if self.connection is not None:
                # Checkpoint before closing so Windows can remove WAL sidecars
                # promptly during maintenance, tests, and clean shutdown.
                try:
                    self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self.connection.close()
                self.connection = None

    def __enter__(self) -> SQLiteStore:
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
                self._apply_migration(version)
            current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            if current != SCHEMA_VERSION:
                raise MigrationError(f"schema version {current} is not supported target {SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise MigrationError(f"schema migration failed: {exc}") from exc

    def _apply_migration(self, version: int) -> None:
        """Apply one schema migration, its data fix, and its version row atomically.

        Statements are executed individually rather than through `executescript`
        so that a Python data migration can share the transaction: `executescript`
        commits before it runs, which would leave the schema and the data fix
        separable across a crash.
        """
        data_migration = DATA_MIGRATIONS.get(version)
        try:
            with self.transaction() as connection:
                for statement in _split_statements(MIGRATIONS[version]):
                    connection.execute(statement)
                if data_migration is not None:
                    data_migration(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                    (version,),
                )
        except sqlite3.IntegrityError as exc:
            # A migration that adds an invariant can only fail on data that already
            # breaks it, and the bot will not start until it is resolved. Name the
            # rows in the way rather than leaving the operator with a bare
            # constraint message on their live campaign database.
            raise MigrationError(
                f"schema migration {version} failed: {exc}{self._conflict_hint(version)}"
            ) from exc
        except sqlite3.Error as exc:
            raise MigrationError(f"schema migration {version} failed: {exc}") from exc

    def _conflict_hint(self, version: int) -> str:
        if version != 10:
            return ""
        try:
            rows = self._require_connection().execute(
                """SELECT discord_user_id, GROUP_CONCAT(name, ', ') AS names
                     FROM characters
                    WHERE lifecycle = 'ACTIVE' AND discord_user_id IS NOT NULL
                 GROUP BY discord_user_id
                   HAVING COUNT(*) > 1
                 ORDER BY discord_user_id"""
            ).fetchall()
        except sqlite3.Error:
            return ""
        if not rows:
            return ""
        conflicts = "; ".join(f"Discord user {row[0]} has {row[1]}" for row in rows)
        return (
            ". Quartermaster now allows one active character per Discord user. Mark the"
            f" extras DEAD, RETIRED, or DEPARTED on the previous build, then upgrade: {conflicts}"
        )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Borrow the shared connection for a read that must not interleave with a write."""
        with self.connection_lock:
            yield self._require_connection()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        committed = False
        with self.connection_lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                committed = True
        # Announced outside the lock, and only for a transaction that reached
        # commit: a listener told about a rolled-back write would go looking
        # for events that are not there.
        if committed:
            self._announce_commit()

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
