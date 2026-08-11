from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from quartermaster.db import SCHEMA_VERSION, SQLiteStore
from quartermaster.integration import AvraeInteractionContext, ProviderIntegrationError, ProviderIntegrationService, ProviderResult, ProviderTimeout
from quartermaster.operations import health_report
from quartermaster.receipts import ReceiptRepository


class ProviderIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tempdir.name) / "quartermaster.sqlite").open()
        self.receipts = ReceiptRepository(self.store)
        self.integration = ProviderIntegrationService(self.store, self.receipts)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _begin(self, interaction_id: str = "interaction-1"):
        return self.integration.begin(
            interaction_id,
            actor_id="actor-1",
            guild_id="guild-1",
            channel_id="channel-1",
            session_id="session-1",
            operation_kind="attack",
            payload={"action": "Longsword"},
        )

    def test_schema_9_adds_durable_provider_operation_table(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 9)
        columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(provider_operations)").fetchall()
        }
        self.assertIn("provider_reference", columns)
        self.assertIn("result_payload", columns)

    def test_avrae_context_preserves_real_actor_and_native_channel_identity(self) -> None:
        from types import SimpleNamespace

        context = AvraeInteractionContext.from_interaction(
            SimpleNamespace(
                author=SimpleNamespace(id=17),
                guild=SimpleNamespace(id=23),
                channel=SimpleNamespace(id=29),
            ),
            session_id="session-1",
        )
        self.assertEqual(context.begin_kwargs(), {
            "actor_id": "17",
            "guild_id": "23",
            "channel_id": "29",
            "session_id": "session-1",
            "provider_reference": "channel:29",
        })

    def test_avrae_context_rejects_dm_or_missing_session(self) -> None:
        from types import SimpleNamespace

        with self.assertRaises(ProviderIntegrationError):
            AvraeInteractionContext.from_interaction(
                SimpleNamespace(author=SimpleNamespace(id=17), guild=None, channel=SimpleNamespace(id=29)),
                session_id="session-1",
            )

    def test_request_and_receipt_are_created_atomically_and_replayed(self) -> None:
        first = self._begin()
        replay = self._begin()
        self.assertEqual(first.receipt.status, "PROCESSING")
        self.assertEqual(replay.receipt.status, "PROCESSING")
        self.assertEqual(replay.request.operation_id, first.request.operation_id)
        row = self.store.connection.execute(
            "SELECT status, request_payload, correlation_id FROM provider_operations WHERE interaction_id = ?",
            ("interaction-1",),
        ).fetchone()
        self.assertEqual(row["status"], "REQUESTED")
        self.assertEqual(json.loads(row["request_payload"]), {"action": "Longsword"})
        self.assertEqual(row["correlation_id"], f"qm:{first.request.operation_id}")
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM provider_operations").fetchone()[0],
            1,
        )

    def test_commit_updates_provider_and_receipt_in_one_transaction(self) -> None:
        execution = self._begin()
        result = self.integration.committed(
            execution,
            ProviderResult(
                status="COMMITTED",
                provider_reference="combat:channel-1",
                provider_version="avrae-test",
                payload={"description": "provider-owned result"},
            ),
        )
        self.assertEqual(result.status, "COMMITTED")
        row = self.store.connection.execute(
            "SELECT status, provider_reference, provider_version, result_payload FROM provider_operations WHERE operation_id = ?",
            (execution.request.operation_id,),
        ).fetchone()
        self.assertEqual(row["status"], "COMMITTED")
        self.assertEqual(row["provider_reference"], "combat:channel-1")
        self.assertEqual(row["provider_version"], "avrae-test")
        self.assertEqual(json.loads(row["result_payload"])["result"]["description"], "provider-owned result")

    def test_timeout_is_recorded_as_unknown_and_is_not_retried(self) -> None:
        execution = self._begin()

        class Gateway:
            def execute(self, _request):
                raise ProviderTimeout("Avrae did not confirm the action")

        result = self.integration.execute(execution, Gateway())
        replay = self.integration.execute(self._begin(), Gateway())
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.logical_response["status"], "UNKNOWN")
        self.assertEqual(replay.logical_response, result.logical_response)
        row = self.store.connection.execute(
            "SELECT status, result_payload FROM provider_operations WHERE operation_id = ?",
            (execution.request.operation_id,),
        ).fetchone()
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertEqual(json.loads(row["result_payload"])["status"], "UNKNOWN")
        report = health_report(self.store)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertEqual(report["checks"]["provider_operations"], "DEGRADED")
        self.assertEqual(report["counts"]["provider_operations_unknown"], 1)

    def test_start_prepare_rolls_back_receipt_when_provider_record_cannot_be_written(self) -> None:
        self.store.connection.execute("DROP TABLE provider_operations")
        with self.assertRaises(sqlite3.OperationalError):
            self._begin("interaction-rollback")
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM interaction_receipts WHERE interaction_id = 'interaction-rollback'"
            ).fetchone()
        )

    def test_startup_recovery_marks_requested_provider_operations_unknown(self) -> None:
        execution = self._begin("interaction-recovery")
        recovered = self.receipts.recover_processing(reason="process stopped")
        self.assertEqual(recovered, 1)
        row = self.store.connection.execute(
            "SELECT status, result_payload FROM provider_operations WHERE operation_id = ?",
            (execution.request.operation_id,),
        ).fetchone()
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertEqual(json.loads(row["result_payload"])["error"], "DEFERRED_OPERATION_FAILED")
