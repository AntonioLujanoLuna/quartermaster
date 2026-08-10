"""Opaque workflow handles with atomic single-use consumption."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .clock import iso_now
from .db import SQLiteStore


class HandleError(RuntimeError):
    """Raised when a handle cannot be used."""


@dataclass(frozen=True)
class Handle:
    id: str
    workflow_type: str
    action: str
    actor_id: str | None
    payload: Any
    read_set_snapshot: Any
    single_use: bool
    expires_at: str | None


class HandleRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create(
        self,
        *,
        workflow_type: str,
        action: str,
        actor_id: str | None,
        payload: Any,
        read_set_snapshot: Any,
        single_use: bool = True,
        ttl_seconds: int | None = None,
    ) -> str:
        with self.store.transaction() as connection:
            return self.create_in_transaction(
                connection,
                workflow_type=workflow_type,
                action=action,
                actor_id=actor_id,
                payload=payload,
                read_set_snapshot=read_set_snapshot,
                single_use=single_use,
                ttl_seconds=ttl_seconds,
            )

    def create_in_transaction(
        self,
        connection: Any,
        *,
        workflow_type: str,
        action: str,
        actor_id: str | None,
        payload: Any,
        read_set_snapshot: Any,
        single_use: bool = True,
        ttl_seconds: int | None = None,
    ) -> str:
        handle_id = secrets.token_urlsafe(12)
        expires_at = None
        if ttl_seconds is not None:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
        connection.execute(
            """INSERT INTO interaction_handles(
                id, schema_version, workflow_type, action, actor_id, payload,
                read_set_snapshot, single_use, created_at, expires_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                handle_id,
                workflow_type,
                action,
                actor_id,
                json.dumps(payload, sort_keys=True),
                json.dumps(read_set_snapshot, sort_keys=True),
                int(single_use),
                iso_now(),
                expires_at,
            ),
        )
        return handle_id

    def consume_and_mutate(
        self,
        handle_id: str,
        *,
        actor_id: str | None,
        mutation: Callable[[Any, Handle], Any],
    ) -> Any:
        with self.store.transaction() as connection:
            return self.consume_and_mutate_in_transaction(connection, handle_id, actor_id=actor_id, mutation=mutation)

    def consume_and_mutate_in_transaction(
        self,
        connection: Any,
        handle_id: str,
        *,
        actor_id: str | None,
        mutation: Callable[[Any, Handle], Any],
    ) -> Any:
        row = connection.execute("SELECT * FROM interaction_handles WHERE id = ?", (handle_id,)).fetchone()
        if row is None:
            raise HandleError("HANDLE_INVALID")
        if row["expires_at"] is not None and row["expires_at"] <= iso_now():
            raise HandleError("INTERACTION_EXPIRED")
        if row["actor_id"] is not None and row["actor_id"] != actor_id:
            raise HandleError("AUTHORIZATION_ERROR")
        if row["single_use"] and row["consumed_at"] is not None:
            raise HandleError("HANDLE_CONSUMED")
        handle = Handle(
            id=row["id"],
            workflow_type=row["workflow_type"],
            action=row["action"],
            actor_id=row["actor_id"],
            payload=json.loads(row["payload"]),
            read_set_snapshot=json.loads(row["read_set_snapshot"]),
            single_use=bool(row["single_use"]),
            expires_at=row["expires_at"],
        )
        result = mutation(connection, handle)
        if handle.single_use:
            connection.execute(
                "UPDATE interaction_handles SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                (iso_now(), handle_id),
            )
        return result

    def cleanup(self, *, now: str | None = None, expiry_margin_seconds: int = 0, replay_retention_seconds: int = 600) -> int:
        current = now or iso_now()
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """DELETE FROM interaction_handles
                   WHERE (expires_at IS NOT NULL AND expires_at < ?)
                      OR (consumed_at IS NOT NULL AND julianday(consumed_at) < julianday(?, ?))""",
                (current, current, f"-{replay_retention_seconds} seconds"),
            )
            return cursor.rowcount
