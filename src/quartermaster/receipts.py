"""FAST and DEFERRED interaction receipt protocols."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .clock import iso_now
from .db import SQLiteStore


class ReceiptError(RuntimeError):
    """Raised for invalid receipt state transitions."""


@dataclass(frozen=True)
class ReceiptResult:
    interaction_id: str
    operation_id: str
    status: str
    response_kind: str
    logical_response: Any


class ReceiptRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def execute_fast(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        response_kind: str,
        mutation: Callable[[Any, str], Any],
    ) -> ReceiptResult:
        """Run mutation and COMMITTED receipt in one transaction."""
        operation_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM interaction_receipts WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if existing is not None:
                if existing["status"] != "COMMITTED":
                    raise ReceiptError(
                        f"FAST interaction {interaction_id} has unexpected receipt state {existing['status']}"
                    )
                return self._result(existing)
            logical_response = mutation(connection, operation_id)
            serialized = json.dumps(logical_response, sort_keys=True, separators=(",", ":"))
            now = iso_now()
            connection.execute(
                """INSERT INTO interaction_receipts(
                    interaction_id, operation_id, actor_id, execution_class, status,
                    response_kind, logical_response, serialized_response, created_at, committed_at
                ) VALUES (?, ?, ?, 'FAST', 'COMMITTED', ?, ?, ?, ?, ?)""",
                (interaction_id, operation_id, actor_id, response_kind, serialized, serialized, now, now),
            )
            return ReceiptResult(interaction_id, operation_id, "COMMITTED", response_kind, logical_response)

    def begin_deferred(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        response_kind: str,
        prepare: Callable[[Any, str], None] | None = None,
    ) -> ReceiptResult:
        """Durably reserve a DEFERRED operation before acknowledgement."""
        operation_id = str(uuid.uuid4())
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM interaction_receipts WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if existing is not None:
                return self._result(existing)
            now = iso_now()
            pending = {"status": "PROCESSING", "operation_id": operation_id}
            connection.execute(
                """INSERT INTO interaction_receipts(
                    interaction_id, operation_id, actor_id, execution_class, status,
                    response_kind, logical_response, created_at
                ) VALUES (?, ?, ?, 'DEFERRED', 'PROCESSING', ?, ?, ?)""",
                (interaction_id, operation_id, actor_id, response_kind, json.dumps(pending), now),
            )
            if prepare is not None:
                prepare(connection, operation_id)
            return ReceiptResult(interaction_id, operation_id, "PROCESSING", response_kind, pending)

    def commit_deferred(
        self,
        interaction_id: str,
        logical_response: Any,
        *,
        finalize: Callable[[Any, str, str, Any], None] | None = None,
    ) -> ReceiptResult:
        return self._finish_deferred(interaction_id, "COMMITTED", logical_response, finalize=finalize)

    def fail_deferred(
        self,
        interaction_id: str,
        logical_response: Any,
        *,
        finalize: Callable[[Any, str, str, Any], None] | None = None,
    ) -> ReceiptResult:
        return self._finish_deferred(interaction_id, "FAILED", logical_response, finalize=finalize)

    def _finish_deferred(
        self,
        interaction_id: str,
        status: str,
        logical_response: Any,
        *,
        finalize: Callable[[Any, str, str, Any], None] | None = None,
    ) -> ReceiptResult:
        serialized = json.dumps(logical_response, sort_keys=True, separators=(",", ":"))
        now = iso_now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_receipts WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise ReceiptError(f"unknown DEFERRED interaction {interaction_id}")
            if row["status"] != "PROCESSING":
                return self._result(row)
            if finalize is not None:
                finalize(connection, row["operation_id"], status, logical_response)
            column = "committed_at" if status == "COMMITTED" else "failed_at"
            connection.execute(
                f"UPDATE interaction_receipts SET status = ?, logical_response = ?, serialized_response = ?, {column} = ? WHERE interaction_id = ?",
                (status, serialized, serialized, now, interaction_id),
            )
            updated = connection.execute(
                "SELECT * FROM interaction_receipts WHERE interaction_id = ?", (interaction_id,)
            ).fetchone()
            return self._result(updated)

    def recover_processing(self, *, reason: str = "Interrupted operation recovered during startup") -> int:
        now = iso_now()
        payload = {"error": "DEFERRED_OPERATION_FAILED", "message": reason, "retryable": True}
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.store.transaction() as connection:
            unknown = json.dumps(
                {"status": "UNKNOWN", "error": "DEFERRED_OPERATION_FAILED", "message": reason, "retryable": True},
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """UPDATE provider_operations
                   SET status = 'UNKNOWN', result_payload = ?, updated_at = ?
                   WHERE status = 'REQUESTED'
                     AND operation_id IN (
                         SELECT operation_id FROM interaction_receipts
                         WHERE execution_class = 'DEFERRED' AND status = 'PROCESSING'
                     )""",
                (unknown, now),
            )
            cursor = connection.execute(
                """UPDATE interaction_receipts
                   SET status = 'FAILED', logical_response = ?, serialized_response = ?, failed_at = ?
                   WHERE execution_class = 'DEFERRED' AND status = 'PROCESSING'""",
                (serialized, serialized, now),
            )
            return cursor.rowcount

    def cleanup_terminal(self, *, retention_seconds: int = 86_400) -> int:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """DELETE FROM interaction_receipts
                   WHERE status IN ('COMMITTED', 'FAILED')
                     AND julianday(created_at) < julianday('now', ?)""",
                (f"-{retention_seconds} seconds",),
            )
            return cursor.rowcount

    @staticmethod
    def _result(row: Any) -> ReceiptResult:
        return ReceiptResult(
            interaction_id=row["interaction_id"],
            operation_id=row["operation_id"],
            status=row["status"],
            response_kind=row["response_kind"],
            logical_response=json.loads(row["logical_response"]),
        )
