from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from quartermaster.config import ConfigurationError, Settings
from quartermaster.characters import CharacterError, CharacterService
from quartermaster.currency import CurrencyError, CurrencySemanticStaleness, CurrencyService
from quartermaster.db import SCHEMA_VERSION, SQLiteStore
from quartermaster.export import render_export
from quartermaster.handles import HandleError, HandleRepository
from quartermaster.inventory import InventoryService, SemanticStaleness
from quartermaster.loot import LootDropError, LootDropService
from quartermaster.metrics import metric_report, record_metric, render_metrics
from quartermaster.operations import (
    create_backup,
    create_scheduled_backup,
    health_report,
    record_discord_surface_health,
    restore_backup,
    run_maintenance,
    validate_backup,
)
from quartermaster.projections import EventOutboxWorker, StateProjectionScheduler
from quartermaster.receipts import ReceiptRepository
from quartermaster.recovery import recover_startup
from quartermaster.response import (
    DeferredExecutionError,
    FastExecutionResult,
    ResponseController,
    ResponseState,
    ResponseStateError,
    execute_deferred,
    execute_fast,
)
from quartermaster.sessions import SessionService
from quartermaster.transport import FakeDiscordTransport, RateLimitedError
from quartermaster.discord_projection import DiscordProjectionTransport


class QuartermasterCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "quartermaster.sqlite"
        self.store = SQLiteStore(self.db_path).open()
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO characters(id, name, discord_user_id, lifecycle, created_at, updated_at)
                   VALUES ('player-character', 'Player Character', 'player', 'ACTIVE', '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z')"""
            )
        self.receipts = ReceiptRepository(self.store)
        self.handles = HandleRepository(self.store)
        self.inventory = InventoryService(self.store, self.receipts, self.handles)
        self.loot = LootDropService(self.store, self.receipts, self.handles)
        self.sessions = SessionService(self.store, self.receipts, self.loot)
        self.characters = CharacterService(self.store, self.receipts)
        self.currency = CurrencyService(self.store, self.receipts)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _insert_character(self, character_id: str, name: str, lifecycle: str = "ACTIVE") -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO characters(id, name, discord_user_id, lifecycle, created_at, updated_at)
                   VALUES (?, ?, NULL, ?, '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z')""",
                (character_id, name, lifecycle),
            )

    def test_settings_require_guild_and_database(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env({})
        settings = Settings.from_env({"QM_GUILD_ID": "123", "QM_DATABASE_PATH": "db.sqlite"})
        self.assertEqual(settings.guild_id, "123")
        with self.assertRaises(ConfigurationError):
            settings.require_discord_token()

    def test_settings_load_backup_and_surface_health_configuration(self) -> None:
        settings = Settings.from_env(
            {
                "QM_GUILD_ID": "123",
                "QM_DATABASE_PATH": "db.sqlite",
                "QM_BACKUP_DIRECTORY": "backup-store",
                "QM_BACKUP_OFF_DEVICE_DIRECTORY": "D:/off-device",
                "QM_BACKUP_RETENTION_COUNT": "3",
                "QM_BACKUP_INTERVAL_SECONDS": "3600",
                "QM_DISCORD_SURFACE_HEALTH_MAX_AGE_SECONDS": "120",
            }
        )
        self.assertEqual(settings.backup_directory, Path("backup-store"))
        self.assertEqual(settings.backup_off_device_directory, Path("D:/off-device"))
        self.assertEqual(settings.backup_retention_count, 3)
        self.assertEqual(settings.backup_interval_seconds, 3600)
        self.assertEqual(settings.discord_surface_health_max_age_seconds, 120)

    def test_server_owner_is_a_dm_administrator(self) -> None:
        from types import SimpleNamespace

        from quartermaster.discord_adapter import _is_dm

        settings = Settings(guild_id="42", database_path=self.db_path)
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=42), user=SimpleNamespace(id=42))
        self.assertTrue(asyncio.run(_is_dm(interaction, settings)))

    def test_dm_authorization_accepts_configured_role_and_rejects_other_members(self) -> None:
        import discord
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        member = MagicMock(spec=discord.Member)
        member.id = 7
        member.guild_permissions.manage_guild = False
        member.roles = [SimpleNamespace(id=44)]
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=99), user=member)
        allowed = Settings(guild_id="123", database_path=self.db_path, dm_role_ids=("44",))
        denied = Settings(guild_id="123", database_path=self.db_path, dm_role_ids=("55",))
        from quartermaster.discord_adapter import _is_dm

        self.assertTrue(asyncio.run(_is_dm(interaction, allowed)))
        self.assertFalse(asyncio.run(_is_dm(interaction, denied)))
        member.guild_permissions.manage_guild = True
        self.assertTrue(asyncio.run(_is_dm(interaction, denied)))

    def test_fast_mutation_and_receipt_are_atomic_and_replayed(self) -> None:
        calls = 0

        def mutation(connection, operation_id):
            nonlocal calls
            calls += 1
            connection.execute(
                "INSERT INTO ledger_entries(id, operation_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (operation_id, operation_id, "TEST", json.dumps({"value": 1})),
            )
            return {"ok": True, "operation_id": operation_id}

        first = self.receipts.execute_fast("interaction-1", actor_id="actor-1", response_kind="message", mutation=mutation)
        second = self.receipts.execute_fast("interaction-1", actor_id="actor-1", response_kind="message", mutation=mutation)
        self.assertEqual(first.status, "COMMITTED")
        self.assertEqual(second.logical_response, first.logical_response)
        self.assertEqual(calls, 1)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], 1)

    def test_fast_exception_rolls_back_without_receipt(self) -> None:
        def mutation(connection, operation_id):
            connection.execute(
                "INSERT INTO ledger_entries(id, operation_id, event_type, payload, created_at) VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (operation_id, operation_id, "TEST", "{}"),
            )
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.receipts.execute_fast("interaction-2", actor_id=None, response_kind="message", mutation=mutation)
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM interaction_receipts WHERE interaction_id = 'interaction-2'"
            ).fetchone()
        )
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], 0)

    def test_deferred_processing_is_recovered_as_failed(self) -> None:
        started = self.receipts.begin_deferred("interaction-3", actor_id="actor-1", response_kind="export")
        self.assertEqual(started.status, "PROCESSING")
        self.assertEqual(recover_startup(self.receipts, self.handles)["failed_deferred_receipts"], 1)
        recovered = self.receipts.begin_deferred("interaction-3", actor_id="actor-1", response_kind="export")
        self.assertEqual(recovered.status, "FAILED")
        self.assertTrue(recovered.logical_response["retryable"])

    def test_deferred_completion_is_idempotent(self) -> None:
        self.receipts.begin_deferred("interaction-4", actor_id=None, response_kind="export")
        committed = self.receipts.commit_deferred("interaction-4", {"file": "export.md"})
        replay = self.receipts.commit_deferred("interaction-4", {"file": "different.md"})
        self.assertEqual(committed.logical_response, {"file": "export.md"})
        self.assertEqual(replay.logical_response, committed.logical_response)

    def test_single_use_handle_consumption_and_mutation_are_atomic(self) -> None:
        handle_id = self.handles.create(
            workflow_type="stash", action="take", actor_id="actor-1", payload={"item": "Potion"}, read_set_snapshot={"quantity": 1}
        )
        result = self.handles.consume_and_mutate(handle_id, actor_id="actor-1", mutation=lambda connection, handle: {"ok": True})
        self.assertEqual(result, {"ok": True})
        with self.assertRaisesRegex(HandleError, "HANDLE_CONSUMED"):
            self.handles.consume_and_mutate(handle_id, actor_id="actor-1", mutation=lambda connection, handle: {"ok": True})

    def test_response_state_has_one_initial_ack(self) -> None:
        controller = ResponseController(started_at=100.0, soft_deadline_seconds=1.2)
        self.assertTrue(controller.should_fallback_to_deferred(can_defer=True, write_active=False, now=101.2))
        controller.defer()
        self.assertEqual(controller.state, ResponseState.DEFERRED)
        with self.assertRaises(ResponseStateError):
            controller.respond({"late": True})

    def test_execute_fast_falls_back_to_deferred_before_long_local_work_finishes(self) -> None:
        release = threading.Event()

        class Response:
            def __init__(self) -> None:
                self.deferred = False

            def is_done(self) -> bool:
                return self.deferred

            async def defer(self, *, ephemeral: bool = False) -> None:
                self.deferred = True
                release.set()

        class Interaction:
            def __init__(self) -> None:
                self.response = Response()

        interaction = Interaction()

        def operation() -> dict[str, bool]:
            release.wait(1)
            return {"ok": True}

        async def run() -> object:
            return await execute_fast(
                interaction,
                operation,
                soft_deadline_seconds=0.01,
            )

        result = asyncio.run(run())
        self.assertTrue(result.deferred)
        self.assertEqual(result.value, {"ok": True})
        self.assertTrue(interaction.response.deferred)

    def test_adapter_fast_path_uses_deadline_fallback(self) -> None:
        from quartermaster.discord_adapter import _run_fast

        release = threading.Event()

        class Response:
            def __init__(self) -> None:
                self.deferred = False

            async def defer(self, *, ephemeral: bool = False) -> None:
                self.deferred = True
                release.set()

        class Interaction:
            def __init__(self) -> None:
                self.response = Response()

        interaction = Interaction()
        settings = Settings(guild_id="123", database_path=self.db_path, soft_deadline_seconds=0.01)

        def operation() -> dict[str, bool]:
            release.wait(1)
            return {"ok": True}

        result = asyncio.run(_run_fast(interaction, self.store, settings, operation))
        self.assertTrue(result.deferred)
        self.assertEqual(result.value, {"ok": True})
        self.assertTrue(interaction.response.deferred)

    def test_execute_deferred_commits_replays_and_records_failure(self) -> None:
        class Response:
            def __init__(self) -> None:
                self.deferred = False

            def is_done(self) -> bool:
                return self.deferred

            async def defer(self, *, ephemeral: bool = False) -> None:
                self.deferred = True

        class Interaction:
            def __init__(self, interaction_id: str) -> None:
                self.id = interaction_id
                self.response = Response()

        calls = 0

        def operation() -> dict[str, str]:
            nonlocal calls
            calls += 1
            return {"value": "export"}

        first = asyncio.run(
            execute_deferred(
                Interaction("deferred-1"),
                self.receipts,
                operation,
                actor_id="actor-1",
                response_kind="export",
                ephemeral=True,
            )
        )
        replay = asyncio.run(
            execute_deferred(
                Interaction("deferred-1"),
                self.receipts,
                operation,
                actor_id="actor-1",
                response_kind="export",
                ephemeral=True,
            )
        )
        self.assertEqual(first.receipt.status, "COMMITTED")
        self.assertTrue(first.deferred)
        self.assertFalse(replay.deferred)
        self.assertEqual(replay.receipt.logical_response, {"value": "export"})
        self.assertEqual(calls, 1)

        def failing_operation() -> object:
            raise RuntimeError("export failed")

        with self.assertRaises(DeferredExecutionError) as raised:
            asyncio.run(
                execute_deferred(
                    Interaction("deferred-2"),
                    self.receipts,
                    failing_operation,
                    actor_id="actor-1",
                    response_kind="export",
                    ephemeral=True,
                )
            )
        self.assertEqual(raised.exception.receipt.status, "FAILED")
        self.assertTrue(raised.exception.receipt.logical_response["retryable"])

    def test_export_is_human_readable_and_uses_canonical_state(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions(id, session_number, status, started_at) VALUES ('s1', 1, 'ACTIVE', '2026-08-09T20:00:00Z')"
            )
            connection.execute(
                "INSERT INTO inventory_stacks(id, item_name, quantity, owner_type, owner_id, updated_at) VALUES ('i1', 'Silvered Dagger', 1, 'PARTY', 'party', '2026-08-09T20:00:00Z')"
            )
        output = render_export(self.store)
        self.assertIn("Party Stash", output)
        self.assertIn("Silvered Dagger x1", output)
        self.assertIn("Active session: 1", output)

    def test_treasury_adjustment_is_integer_atomic_and_replayed(self) -> None:
        first = self.currency.adjust_treasury_interaction(
            "treasury-adjust-1",
            actor_id="dm",
            deltas={"gp": 80, "pp": 1},
            reason="Found in the vault",
        )
        replay = self.currency.adjust_treasury_interaction(
            "treasury-adjust-1",
            actor_id="dm",
            deltas={"gp": -80, "pp": -1},
            reason="different retry",
        )
        self.assertEqual(first.logical_response, replay.logical_response)
        self.assertEqual(self.currency.view_treasury(), {"cp": 0, "sp": 0, "ep": 0, "gp": 80, "pp": 1})
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE event_type = 'TREASURY_ADJUSTED'"
            ).fetchone()[0],
            1,
        )
        projection = self.store.connection.execute(
            "SELECT dirty_since FROM projection_targets WHERE target_id = 'party-stash'"
        ).fetchone()
        self.assertIsNotNone(projection["dirty_since"])

    def test_treasury_rejects_fractional_electrum_and_negative_result(self) -> None:
        with self.assertRaisesRegex(CurrencyError, "must be an integer"):
            self.currency.adjust_treasury_interaction(
                "treasury-fractional",
                actor_id="dm",
                deltas={"gp": 1.5},
            )
        with self.assertRaisesRegex(CurrencyError, "electrum is disabled"):
            self.currency.adjust_treasury_interaction(
                "treasury-electrum",
                actor_id="dm",
                deltas={"ep": 1},
            )
        with self.assertRaisesRegex(CurrencyError, "cannot become negative"):
            self.currency.adjust_treasury_interaction(
                "treasury-negative",
                actor_id="dm",
                deltas={"gp": -1},
            )
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM interaction_receipts WHERE interaction_id = 'treasury-negative'"
            ).fetchone()
        )

    def test_treasury_split_preserves_remainders_and_active_recipient_set(self) -> None:
        self._insert_character("c1", "Aria")
        self._insert_character("c2", "Borin")
        self._insert_character("c3", "Cleo")
        self._insert_character("c4", "Departed", lifecycle="DEPARTED")
        self.currency.adjust_treasury_interaction("split-seed", actor_id="dm", deltas={"gp": 81})

        result = self.currency.split_treasury_interaction(
            "split-1",
            actor_id="dm",
            amounts={"gp": 81},
        )
        self.assertEqual(result.logical_response["remainder"]["gp"], 1)
        self.assertEqual(len(result.logical_response["recipients"]), 4)
        self.assertEqual(self.currency.view_treasury()["gp"], 0)
        balances = self.store.connection.execute(
            "SELECT owner_id, gp FROM currency_balances WHERE owner_type = 'CHARACTER' ORDER BY owner_id"
        ).fetchall()
        self.assertEqual(
            [(row["owner_id"], row["gp"]) for row in balances],
            [("c1", 20), ("c2", 20), ("c3", 20), ("player-character", 20)],
        )

    def test_treasury_give_is_atomic_and_rejects_non_active_characters(self) -> None:
        self._insert_character("active", "Active Hero")
        self._insert_character("dead", "Dead Hero", lifecycle="DEAD")
        self.currency.adjust_treasury_interaction("give-seed", actor_id="dm", deltas={"gp": 50})

        result = self.currency.give_to_character_interaction(
            "give-1",
            actor_id="dm",
            character_id="active",
            amounts={"gp": 17},
        )
        self.assertEqual(result.logical_response["character_after"]["gp"], 17)
        self.assertEqual(self.currency.view_treasury()["gp"], 33)
        with self.assertRaisesRegex(CurrencyError, "only active"):
            self.currency.give_to_character_interaction(
                "give-dead",
                actor_id="dm",
                character_id="dead",
                amounts={"gp": 1},
            )
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT 1 FROM interaction_receipts WHERE interaction_id = 'give-dead'"
            ).fetchone()
        )
        self.assertIn("Active Hero [ACTIVE]", render_export(self.store))

    def test_character_lifecycle_is_explicit_and_does_not_move_possessions(self) -> None:
        created = self.characters.create_interaction(
            "character-create-1",
            actor_id="dm",
            name="Edrin",
            discord_user_id="player-1",
        )
        character_id = created.logical_response["character_id"]
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO inventory_stacks(
                    id, item_name, normalized_name, variant_metadata, quantity, owner_type, owner_id,
                    version, last_acquired_at, updated_at
                ) VALUES ('character-item', 'Herb', 'herb', '{}', 2, 'CHARACTER', ?, 1, '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z')""",
                (character_id,),
            )
        self.currency.adjust_treasury_interaction("character-currency-seed", actor_id="dm", deltas={"gp": 10})
        self.currency.give_to_character_interaction(
            "character-currency-give",
            actor_id="dm",
            character_id=character_id,
            amounts={"gp": 10},
        )
        changed = self.characters.transition_interaction(
            "character-dead-1",
            actor_id="dm",
            character_id=character_id,
            lifecycle="DEAD",
        )
        self.assertEqual(changed.logical_response["from"], "ACTIVE")
        self.assertEqual(changed.logical_response["to"], "DEAD")
        item = self.store.connection.execute(
            "SELECT quantity FROM inventory_stacks WHERE id = 'character-item'"
        ).fetchone()
        balance = self.store.connection.execute(
            "SELECT gp FROM currency_balances WHERE owner_type = 'CHARACTER' AND owner_id = ?",
            (character_id,),
        ).fetchone()
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(balance["gp"], 10)
        with self.assertRaisesRegex(CharacterError, "cannot transition DEAD to RETIRED"):
            self.characters.transition_interaction(
                "character-retired-invalid",
                actor_id="dm",
                character_id=character_id,
                lifecycle="RETIRED",
            )
        edrin = next(row for row in self.characters.list_characters() if row["id"] == character_id)
        self.assertEqual(edrin["lifecycle"], "DEAD")

    def test_non_active_belongings_resolution_moves_items_and_currency_atomically(self) -> None:
        created = self.characters.create_interaction("resolve-create", actor_id="dm", name="Fallen Hero")
        character_id = created.logical_response["character_id"]
        self.currency.adjust_treasury_interaction("resolve-seed", actor_id="dm", deltas={"gp": 10})
        self.currency.give_to_character_interaction(
            "resolve-give",
            actor_id="dm",
            character_id=character_id,
            amounts={"gp": 10},
        )
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO inventory_stacks(
                    id, item_name, normalized_name, variant_metadata, quantity, owner_type, owner_id,
                    version, last_acquired_at, updated_at
                ) VALUES ('resolve-item', 'Relic', 'relic', '{}', 2, 'CHARACTER', ?, 1, '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z')""",
                (character_id,),
            )
        self.characters.transition_interaction(
            "resolve-dead",
            actor_id="dm",
            character_id=character_id,
            lifecycle="DEAD",
        )
        resolved = self.characters.resolve_belongings_interaction(
            "resolve-all",
            actor_id="dm",
            character_id=character_id,
            destination="party",
        )
        self.assertEqual(resolved.logical_response["items_moved"], 1)
        self.assertEqual(resolved.logical_response["currency_moved"]["gp"], 10)
        source_item = self.store.connection.execute(
            "SELECT 1 FROM inventory_stacks WHERE owner_type = 'CHARACTER' AND owner_id = ?",
            (character_id,),
        ).fetchone()
        party_item = self.store.connection.execute(
            "SELECT quantity FROM inventory_stacks WHERE owner_type = 'PARTY' AND normalized_name = 'relic'"
        ).fetchone()
        source_currency = self.store.connection.execute(
            "SELECT gp FROM currency_balances WHERE owner_type = 'CHARACTER' AND owner_id = ?",
            (character_id,),
        ).fetchone()
        self.assertIsNone(source_item)
        self.assertEqual(party_item["quantity"], 2)
        self.assertEqual(source_currency["gp"], 0)
        self.assertEqual(self.currency.view_treasury()["gp"], 10)
        lifecycle = self.store.connection.execute(
            "SELECT lifecycle FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        self.assertEqual(lifecycle["lifecycle"], "DEAD")

    def test_relative_treasury_split_requires_confirmation_after_read_set_changes(self) -> None:
        self._insert_character("split-c1", "Split One")
        self._insert_character("split-c2", "Split Two")
        self.currency.adjust_treasury_interaction("relative-seed", actor_id="dm", deltas={"gp": 80})
        handle_id = self.currency.create_relative_split_handle(actor_id="dm", amounts={"gp": 80})
        self.currency.adjust_treasury_interaction("relative-change", actor_id="dm", deltas={"gp": 1})
        with self.assertRaises(CurrencySemanticStaleness):
            self.currency.split_relative_interaction(
                "relative-stale",
                handle_id=handle_id,
                actor_id="dm",
            )
        confirmed = self.currency.split_relative_interaction(
            "relative-confirmed",
            handle_id=handle_id,
            actor_id="dm",
            confirm_current=True,
        )
        self.assertEqual(confirmed.logical_response["split"]["gp"], 80)
        self.assertEqual(confirmed.logical_response["remainder"]["gp"], 2)
        self.assertEqual(self.currency.view_treasury()["gp"], 1)

    def test_export_and_party_stash_projection_include_treasury(self) -> None:
        self.currency.adjust_treasury_interaction("treasury-export", actor_id="dm", deltas={"gp": 80})
        output = render_export(self.store)
        self.assertIn("## Treasury", output)
        self.assertIn("0 cp · 0 sp · 0 ep · 80 gp · 0 pp", output)
        from quartermaster.projections import render_state

        state = render_state(self.store, "party-stash")
        self.assertEqual(state["treasury"]["gp"], 80)

    def test_backup_creates_valid_snapshot(self) -> None:
        backup = self.store.snapshot(Path(self.tempdir.name) / "backup.sqlite")
        self.assertTrue(backup.exists())
        with SQLiteStore(backup).open() as restored:
            self.assertEqual(restored.connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)

    def test_backup_validation_and_restore_are_equivalent(self) -> None:
        self.inventory.grant_interaction("backup-grant", actor_id="dm", item_name="Backup Token", quantity=2)
        backup = self.store.snapshot(Path(self.tempdir.name) / "backup.sqlite")
        self.assertEqual(validate_backup(backup)["schema_version"], SCHEMA_VERSION)
        restored = restore_backup(backup, Path(self.tempdir.name) / "restored.sqlite")
        self.assertEqual(restored["schema_version"], SCHEMA_VERSION)
        with SQLiteStore(Path(self.tempdir.name) / "restored.sqlite").open() as restored_store:
            self.assertIn("Backup Token x2", render_export(restored_store))

    def test_backup_copies_off_device_applies_retention_and_affects_health(self) -> None:
        primary = Path(self.tempdir.name) / "backups"
        off_device = Path(self.tempdir.name) / "off-device"
        primary.mkdir()
        old_backup = primary / "quartermaster-older.sqlite"
        old_backup.write_text("retained only until the next backup", encoding="utf-8")
        old_timestamp = old_backup.stat().st_mtime - 3600
        os.utime(old_backup, (old_timestamp, old_timestamp))

        result = create_backup(
            self.store,
            primary / "quartermaster-current.sqlite",
            off_device_directory=off_device,
            retention_count=1,
        )
        self.assertTrue(Path(result["primary_path"]).is_file())
        self.assertTrue(Path(result["off_device_path"]).is_file())
        self.assertFalse(old_backup.exists())
        self.assertEqual(result["retention_count"], 1)
        details = self.store.connection.execute(
            "SELECT last_details FROM maintenance_runs WHERE name = 'backup'"
        ).fetchone()
        self.assertEqual(json.loads(details["last_details"])["off_device_path"], result["off_device_path"])
        self.assertEqual(health_report(self.store)["checks"]["backup"], "OK")

        Path(result["off_device_path"]).unlink()
        degraded = health_report(self.store)
        self.assertEqual(degraded["status"], "DEGRADED")
        self.assertEqual(degraded["checks"]["backup"], "DEGRADED")

    def test_scheduled_backup_uses_timestamped_validated_path(self) -> None:
        primary = Path(self.tempdir.name) / "scheduled"
        off_device = Path(self.tempdir.name) / "scheduled-off-device"
        result = create_scheduled_backup(
            self.store,
            primary,
            off_device_directory=off_device,
            retention_count=1,
        )
        self.assertRegex(Path(result["primary_path"]).name, r"^quartermaster-\d{8}-\d{6}Z\.sqlite$")
        self.assertTrue(Path(result["primary_path"]).is_file())
        self.assertTrue(Path(result["off_device_path"]).is_file())
        self.assertEqual(json.loads(
            self.store.connection.execute(
                "SELECT last_details FROM maintenance_runs WHERE name = 'backup'"
            ).fetchone()["last_details"]
        )["retention_count"], 1)

    def test_health_report_is_healthy_for_clean_database(self) -> None:
        create_backup(self.store, Path(self.tempdir.name) / "health-backup.sqlite")
        record_discord_surface_health(
            self.store,
            reachable=True,
            details={"surfaces": {"party-inventory": {"reachable": True}}},
        )
        report = health_report(self.store)
        self.assertEqual(report["status"], "HEALTHY")
        self.assertEqual(report["checks"]["database"], "OK")
        self.assertEqual(report["checks"]["schema"], "OK")
        self.assertEqual(report["checks"]["backup"], "OK")

    def test_health_report_degrades_for_failed_discord_surface_check(self) -> None:
        record_discord_surface_health(self.store, reachable=False, error="channel unavailable")
        report = health_report(self.store)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertEqual(report["checks"]["discord_surfaces"], "DEGRADED")

    def test_maintenance_expires_drops_and_removes_retained_state(self) -> None:
        receipt = self.inventory.grant_interaction("maintenance-receipt", actor_id="dm", item_name="Old Token", quantity=1)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE interaction_receipts SET created_at = '2000-01-01T00:00:00Z' WHERE interaction_id = ?",
                (receipt.interaction_id,),
            )
        handle_id = self.handles.create(
            workflow_type="stash", action="take", actor_id="player", payload={"item": "Old Token"}, read_set_snapshot={"quantity": 1}
        )
        self.handles.consume_and_mutate(handle_id, actor_id="player", mutation=lambda _connection, _handle: {"ok": True})
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE interaction_handles SET consumed_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (handle_id,),
            )
        drop = self.loot.create_drop_interaction("maintenance-drop", actor_id="dm", items=[("Expired Token", 1, None)])
        with self.store.transaction() as connection:
            connection.execute("UPDATE loot_drops SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (drop.logical_response["drop_id"],))
        result = run_maintenance(self.store, receipt_retention_seconds=1, handle_retention_seconds=1)
        self.assertEqual(result, {"expired_drops": 1, "removed_handles": 1, "removed_receipts": 1})
        status = self.store.connection.execute("SELECT last_status FROM maintenance_runs WHERE name = 'transient-state'").fetchone()
        self.assertEqual(status["last_status"], "OK")

    def test_discord_adapter_registers_guild_scoped_commands(self) -> None:
        import discord

        from quartermaster.discord_adapter import BotServices, create_bot

        services = BotServices(self.store, self.receipts, self.inventory, self.sessions)
        bot = create_bot(Settings(guild_id="123", database_path=self.db_path), services)
        commands = bot.tree.get_commands(guild=discord.Object(id=123))
        self.assertEqual(
            {command.name for command in commands},
            {
                "quartermaster",
                "stash",
                "grant",
                "loot",
                "loot-drop",
                "loot-close",
                "export",
                "backup",
                "treasury",
                "treasury-adjust",
                "treasury-split",
                "treasury-give",
                "characters",
                "character-add",
                "character-lifecycle",
                "character-resolve",
                "session-start",
                "session-end",
            },
        )

    def test_quartermaster_launcher_summarizes_state_and_exposes_actions(self) -> None:
        from quartermaster.discord_adapter import (
            BotServices,
            LauncherMoreView,
            QuartermasterLauncherView,
            _launcher_snapshot,
            _render_launcher,
        )

        self.inventory.grant_interaction(
            "launcher-grant",
            actor_id="dm",
            item_name="Launcher Potion",
            quantity=2,
        )
        self.sessions.start_session()
        services = BotServices(
            self.store,
            self.receipts,
            self.inventory,
            self.sessions,
            characters=self.characters,
            currency=self.currency,
            loot=self.loot,
        )
        settings = Settings(guild_id="123", database_path=self.db_path)

        snapshot = _launcher_snapshot(services, self.characters)
        rendered = _render_launcher(snapshot)
        self.assertEqual(snapshot, {"stash_count": 1, "active_session_number": 1, "unresolved_estates": 0})
        self.assertIn("Party Stash · 1 entries", rendered)
        self.assertIn("Session 1 active", rendered)

        view = QuartermasterLauncherView(services, settings, self.characters, self.currency, self.loot)
        self.assertEqual({item.label for item in view.children}, {"Grant loot", "Session", "More…"})
        more = LauncherMoreView(services, settings, self.characters, self.currency, self.loot)
        self.assertEqual(
            {item.label for item in more.children},
            {"Stash", "Open Loot", "Treasury", "Characters", "Export", "Backup", "Health", "Metrics"},
        )

    def test_local_metrics_report_aggregates_histogram_percentiles(self) -> None:
        for duration in (4, 20, 40, 300, 600, 2500):
            record_metric(self.store, "interaction_ack_latency_ms", duration, dimension="FAST")

        report = metric_report(self.store)
        values = report["metrics"]["interaction_ack_latency_ms"]["FAST"]
        self.assertEqual(values["count"], 6)
        self.assertEqual(values["p50_ms"], 50.0)
        self.assertEqual(values["p95_ms"], 5000.0)
        self.assertEqual(values["max_ms"], 2500.0)
        self.assertIn("interaction_ack_latency_ms [FAST]", render_metrics(report))

    def test_discord_response_helper_omits_empty_view(self) -> None:
        from quartermaster.discord_adapter import _send_execution

        class Response:
            def __init__(self) -> None:
                self.kwargs: dict[str, object] | None = None

            def is_done(self) -> bool:
                return False

            async def send_message(self, _message: str, **kwargs: object) -> None:
                self.kwargs = kwargs

        class Interaction:
            def __init__(self) -> None:
                self.response = Response()

        interaction = Interaction()
        asyncio.run(_send_execution(interaction, FastExecutionResult({}, False), "ok"))
        self.assertEqual(interaction.response.kwargs, {"ephemeral": False})

    def test_discord_backup_response_reports_validated_filename(self) -> None:
        from types import SimpleNamespace

        from quartermaster.discord_adapter import _send_deferred_backup

        class Response:
            def __init__(self) -> None:
                self.message: str | None = None
                self.kwargs: dict[str, object] | None = None

            async def send_message(self, message: str, **kwargs: object) -> None:
                self.message = message
                self.kwargs = kwargs

        class Interaction:
            def __init__(self) -> None:
                self.response = Response()

        interaction = Interaction()
        receipt = SimpleNamespace(
            status="COMMITTED",
            logical_response={
                "primary_path": "C:/backups/quartermaster-20260811-090000Z.sqlite",
                "schema_version": SCHEMA_VERSION,
                "off_device_path": None,
            },
        )
        execution = SimpleNamespace(receipt=receipt, deferred=False)
        asyncio.run(_send_deferred_backup(interaction, execution))
        self.assertEqual(
            interaction.response.message,
            f"Backup completed: `quartermaster-20260811-090000Z.sqlite`. Integrity and schema {SCHEMA_VERSION} validation passed.",
        )
        self.assertEqual(interaction.response.kwargs, {"ephemeral": True})

    def test_session_projection_binds_and_reuses_a_discord_thread(self) -> None:
        session = self.sessions.start_session()

        class Thread:
            id = 987

        class Channel:
            async def create_thread(self, **_kwargs):
                return Thread()

        class Bot:
            def __init__(self) -> None:
                self.channel = Channel()
                self.fetches: list[int] = []

            async def fetch_channel(self, channel_id: int):
                self.fetches.append(channel_id)
                return self.channel if channel_id == 123 else Thread()

        bot = Bot()
        transport = DiscordProjectionTransport(
            bot,
            Settings(guild_id="123", database_path=self.db_path, session_log_channel_id="123"),
            self.store,
        )

        async def resolve() -> object:
            first = await transport._fetch_channel(f"session:{session['session_id']}")
            second = await transport._fetch_channel(f"session:{session['session_id']}")
            return first, second

        first, second = asyncio.run(resolve())
        self.assertIsInstance(first, Thread)
        self.assertIsInstance(second, Thread)
        self.assertEqual(bot.fetches, [123, 987, 987])
        stored = self.store.connection.execute(
            "SELECT discord_thread_id FROM sessions WHERE id = ?", (session["session_id"],)
        ).fetchone()
        self.assertEqual(stored["discord_thread_id"], "987")

    def test_discord_surface_reachability_checks_configured_channels(self) -> None:
        class Bot:
            def __init__(self) -> None:
                self.fetched: list[int] = []

            async def fetch_channel(self, channel_id: int) -> object:
                self.fetched.append(channel_id)
                return object()

        bot = Bot()
        transport = DiscordProjectionTransport(
            bot,
            Settings(
                guild_id="123",
                database_path=self.db_path,
                party_inventory_channel_id="456",
                session_log_channel_id="789",
                dm_channel_id="101112",
            ),
            self.store,
        )
        result = asyncio.run(transport.check_surface_reachability())
        self.assertEqual(bot.fetched, [456, 789, 101112])
        self.assertTrue(all(entry["reachable"] for entry in result["surfaces"].values()))

    def test_discord_surface_reachability_converts_rate_limits(self) -> None:
        import discord
        from types import SimpleNamespace

        class RateLimitHTTPException(discord.HTTPException):
            def __init__(self) -> None:
                super().__init__(SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited")
                self.retry_after = 4.5

        class Bot:
            async def fetch_channel(self, _channel_id: int) -> object:
                raise RateLimitHTTPException()

        transport = DiscordProjectionTransport(
            Bot(),
            Settings(guild_id="123", database_path=self.db_path, party_inventory_channel_id="456", session_log_channel_id="789"),
            self.store,
        )
        with self.assertRaises(RateLimitedError) as raised:
            asyncio.run(transport.check_surface_reachability())
        self.assertEqual(raised.exception.retry_after_seconds, 4.5)

    def test_party_stash_pin_permission_failure_is_not_silently_ignored(self) -> None:
        import discord
        from types import SimpleNamespace

        class Message:
            id = 456
            pinned = False

            async def pin(self, *, reason: str) -> None:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "pin denied")

        class Channel:
            async def send(self, _content: str) -> Message:
                return Message()

        class Bot:
            async def fetch_channel(self, _channel_id: int) -> Channel:
                return Channel()

        transport = DiscordProjectionTransport(
            Bot(),
            Settings(guild_id="123", database_path=self.db_path, party_inventory_channel_id="789"),
            self.store,
        )
        with self.assertRaises(discord.Forbidden):
            asyncio.run(transport.upsert_state("party-stash", "party-inventory", {"items": []}, None))

    def test_projection_runner_performs_scheduled_backup_and_surface_check(self) -> None:
        from quartermaster.discord_projection import ProjectionRunner

        class Transport(FakeDiscordTransport):
            async def check_surface_reachability(self) -> dict[str, object]:
                return {"surfaces": {"party-inventory": {"reachable": True}}}

        primary = Path(self.tempdir.name) / "runner-backups"
        self.inventory.grant_interaction(
            "runner-metric-grant",
            actor_id="dm",
            item_name="Runner Metric Token",
            quantity=1,
        )
        runner = ProjectionRunner(
            self.store,
            Transport(),
            maintenance_interval_seconds=0.01,
            backup_directory=str(primary),
            backup_interval_seconds=60,
        )

        async def run_runner() -> None:
            stop_event = asyncio.Event()
            task = asyncio.create_task(runner.run(stop_event))
            await asyncio.sleep(0.05)
            stop_event.set()
            await task

        asyncio.run(run_runner())
        self.assertEqual(len(list(primary.glob("quartermaster-*.sqlite"))), 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT last_status FROM maintenance_runs WHERE name = 'discord-surfaces'"
            ).fetchone()["last_status"],
            "OK",
        )
        metric_values = metric_report(self.store)["metrics"]["projection_dirty_duration_ms"]
        self.assertIn("party-stash", metric_values)
        self.assertGreaterEqual(metric_values["party-stash"]["count"], 1)

    def test_session_projection_destination_moves_to_the_new_session(self) -> None:
        first = self.sessions.start_session()
        self.sessions.end_session(first["session_id"], where_ended="First endpoint")
        second = self.sessions.start_session()

        destination = self.store.connection.execute(
            "SELECT destination FROM projection_targets WHERE target_id = 'session-surface'"
        ).fetchone()["destination"]
        self.assertEqual(destination, f"session:{second['session_id']}")

    def test_inventory_events_bind_to_the_session_at_mutation_time(self) -> None:
        first = self.sessions.start_session()
        self.inventory.grant_interaction("session-bound-grant-1", actor_id="dm", item_name="Session Token", quantity=1)
        self.sessions.end_session(first["session_id"], where_ended="First endpoint")
        second = self.sessions.start_session()
        self.inventory.grant_interaction("session-bound-grant-2", actor_id="dm", item_name="Session Token", quantity=1)

        destinations = [
            row["destination"]
            for row in self.store.connection.execute(
                "SELECT destination FROM event_outbox WHERE event_type = 'ITEM_GRANTED' ORDER BY id"
            ).fetchall()
        ]
        self.assertEqual(destinations, [f"session:{first['session_id']}", f"session:{second['session_id']}"])

    def test_deleted_party_stash_projection_is_recreated_and_pinned(self) -> None:
        import discord
        from types import SimpleNamespace

        class Message:
            id = 456
            pinned = False

            async def pin(self, *, reason: str) -> None:
                self.pinned = True

        class Channel:
            def __init__(self) -> None:
                self.message = Message()

            async def fetch_message(self, _message_id: int) -> object:
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")

            async def send(self, _content: str) -> Message:
                return self.message

        class Bot:
            async def fetch_channel(self, _channel_id: int) -> Channel:
                return channel

        channel = Channel()
        transport = DiscordProjectionTransport(
            Bot(),
            Settings(guild_id="123", database_path=self.db_path, party_inventory_channel_id="789"),
            self.store,
        )

        message_id = asyncio.run(
            transport.upsert_state("party-stash", "party-inventory", {"items": []}, "123")
        )
        self.assertEqual(message_id, "456")
        self.assertTrue(channel.message.pinned)

    def test_session_start_is_explicit_when_an_active_session_exists(self) -> None:
        first = self.sessions.start_session()
        second = self.sessions.start_session()
        self.assertEqual(first["status"], "STARTED")
        self.assertEqual(second["status"], "ACTIVE_EXISTS")
        ended = self.sessions.end_session(first["session_id"], where_ended="At the lower gate")
        self.assertEqual(ended["status"], "CLOSED")

    def test_session_end_interaction_replays_without_closing_twice(self) -> None:
        started = self.sessions.start_session()
        first = self.sessions.end_interaction("session-end-1", actor_id="dm", where_ended="At the gate")
        replay = self.sessions.end_interaction("session-end-1", actor_id="dm", where_ended="Elsewhere")
        self.assertEqual(first.logical_response["status"], "CLOSED")
        self.assertEqual(replay.logical_response, first.logical_response)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM domain_events WHERE event_type = 'SESSION_CLOSED'").fetchone()[0], 1)

    def test_loot_drop_claim_and_close_return_remaining_items_to_party(self) -> None:
        session = self.sessions.start_session()
        created = self.loot.create_drop_interaction(
            "loot-create-1",
            actor_id="dm",
            session_id=session["session_id"],
            items=[("Potion", 2, "found in the crypt"), ("Gem", 1, None)],
        )
        drop = self.loot.list_open()[0]
        potion = next(item for item in drop["items"] if item["item_name"] == "Potion")
        handle = self.loot.create_claim_handle(drop_item_id=potion["id"], actor_id="player", amount=1)
        claimed = self.loot.claim_interaction("loot-claim-1", handle_id=handle, actor_id="player")
        self.assertEqual(claimed.logical_response["status"], "CLAIMED")
        closed = self.loot.close_drop_interaction("loot-close-1", drop_id=created.logical_response["drop_id"], actor_id="dm")
        self.assertEqual(closed.logical_response["status"], "CLOSED")
        self.assertEqual({item["item_name"]: item["quantity"] for item in self.inventory.browse()}, {"Potion": 1, "Gem": 1})
        character = self.store.connection.execute(
            "SELECT quantity FROM inventory_stacks WHERE owner_type = 'CHARACTER' AND owner_id = 'player-character' AND normalized_name = 'potion'"
        ).fetchone()
        self.assertEqual(character["quantity"], 1)

    def test_loot_claim_requires_an_active_registered_character(self) -> None:
        created = self.loot.create_drop_interaction(
            "unregistered-drop",
            actor_id="dm",
            items=[("Unregistered Token", 1, None)],
        )
        drop = self.loot.list_open()[0]
        item = drop["items"][0]
        handle = self.loot.create_claim_handle(drop_item_id=item["id"], actor_id="unregistered", amount=1)
        with self.assertRaisesRegex(LootDropError, "active registered character"):
            self.loot.claim_interaction("unregistered-claim", handle_id=handle, actor_id="unregistered")
        remaining = self.store.connection.execute(
            "SELECT remaining_quantity FROM loot_drop_items WHERE id = ?", (item["id"],)
        ).fetchone()
        self.assertEqual(remaining["remaining_quantity"], 1)

    def test_expired_loot_drop_closes_and_returns_items(self) -> None:
        created = self.loot.create_drop_interaction("loot-create-2", actor_id="dm", items=[("Arrow", 3, None)])
        with self.store.transaction() as connection:
            connection.execute("UPDATE loot_drops SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (created.logical_response["drop_id"],))
        self.assertEqual(self.loot.list_open(), [])
        self.assertEqual(self.inventory.browse()[0]["quantity"], 3)

    def test_session_close_closes_associated_loot_drop(self) -> None:
        session = self.sessions.start_session()
        created = self.loot.create_drop_interaction(
            "loot-create-3", actor_id="dm", session_id=session["session_id"], items=[("Scroll", 1, None)]
        )
        ended = self.sessions.end_session(session["session_id"], where_ended="At the shrine")
        self.assertEqual(ended["closed_drops"], 1)
        status = self.store.connection.execute("SELECT status FROM loot_drops WHERE id = ?", (created.logical_response["drop_id"],)).fetchone()
        self.assertEqual(status["status"], "CLOSED")
        self.assertEqual(self.inventory.browse()[0]["item_name"], "Scroll")

    def test_grant_and_two_independent_take_handles_both_succeed(self) -> None:
        granted = self.inventory.grant_interaction("grant-1", actor_id="dm", item_name="Potion of Healing", quantity=3)
        stack_id = granted.logical_response["stack_id"]
        first_handle = self.inventory.create_take_handle(stack_id=stack_id, actor_id="player", amount=1)
        second_handle = self.inventory.create_take_handle(stack_id=stack_id, actor_id="player", amount=1)
        first = self.inventory.take_interaction("take-1", handle_id=first_handle, actor_id="player")
        second = self.inventory.take_interaction("take-2", handle_id=second_handle, actor_id="player")
        self.assertEqual(first.logical_response["remaining"], 2)
        self.assertEqual(second.logical_response["remaining"], 1)
        self.assertEqual(self.inventory.browse()[0]["quantity"], 1)

    def test_same_handle_replay_does_not_mutate_twice(self) -> None:
        granted = self.inventory.grant_interaction("grant-2", actor_id="dm", item_name="Rope", quantity=2)
        handle_id = self.inventory.create_take_handle(stack_id=granted.logical_response["stack_id"], actor_id="player", amount=1)
        self.inventory.take_interaction("take-3", handle_id=handle_id, actor_id="player")
        with self.assertRaises(HandleError):
            self.inventory.take_interaction("take-4", handle_id=handle_id, actor_id="player")
        self.assertEqual(self.inventory.browse()[0]["quantity"], 1)

    def test_relative_take_requires_confirmation_after_quantity_changes(self) -> None:
        granted = self.inventory.grant_interaction("grant-3", actor_id="dm", item_name="Arrow", quantity=3)
        stack_id = granted.logical_response["stack_id"]
        handle_id = self.inventory.create_take_handle(stack_id=stack_id, actor_id="player", amount="all")
        self.inventory.grant_interaction("grant-4", actor_id="dm", item_name="Arrow", quantity=1)
        with self.assertRaises(SemanticStaleness):
            self.inventory.take_interaction("take-5", handle_id=handle_id, actor_id="player")
        confirmed = self.inventory.confirm_take_interaction("take-6", handle_id=handle_id, actor_id="player")
        self.assertEqual(confirmed.logical_response["quantity"], 4)
        self.assertEqual(self.inventory.browse(), [])

    def test_state_projection_coalesces_to_latest_canonical_state(self) -> None:
        self.inventory.grant_interaction("grant-projection-1", actor_id="dm", item_name="Torch", quantity=1)
        self.inventory.grant_interaction("grant-projection-2", actor_id="dm", item_name="Torch", quantity=2)
        transport = FakeDiscordTransport()
        scheduler = StateProjectionScheduler(self.store, transport)
        self.assertTrue(scheduler.run_once())
        self.assertEqual(transport.state_payloads["party-stash"]["items"][0]["quantity"], 3)
        target = self.store.connection.execute("SELECT dirty_since, delivered_revision, desired_revision FROM projection_targets WHERE target_id = 'party-stash'").fetchone()
        self.assertIsNone(target["dirty_since"])
        self.assertEqual(target["delivered_revision"], target["desired_revision"])

    def test_state_projection_failure_is_retryable(self) -> None:
        self.inventory.grant_interaction("grant-projection-3", actor_id="dm", item_name="Lantern", quantity=1)

        class FailingTransport(FakeDiscordTransport):
            def upsert_state(self, target_id, destination, payload, message_id):
                raise RateLimitedError(30)

        scheduler = StateProjectionScheduler(self.store, FailingTransport())
        self.assertFalse(scheduler.run_once())
        target = self.store.connection.execute("SELECT dirty_since, next_attempt_at, last_error FROM projection_targets WHERE target_id = 'party-stash'").fetchone()
        self.assertIsNotNone(target["dirty_since"])
        self.assertIsNotNone(target["next_attempt_at"])
        self.assertIn("rate limited", target["last_error"])

    def test_async_projection_workers_use_async_transport(self) -> None:
        self.inventory.grant_interaction("grant-async-projection", actor_id="dm", item_name="Bell", quantity=1)

        class AsyncTransport(FakeDiscordTransport):
            async def upsert_state(self, target_id, destination, payload, message_id):
                return super().upsert_state(target_id, destination, payload, message_id)

            async def deliver_event(self, destination, event_type, payload):
                super().deliver_event(destination, event_type, payload)

        transport = AsyncTransport()
        state_scheduler = StateProjectionScheduler(self.store, transport)
        event_worker = EventOutboxWorker(self.store, transport)

        async def deliver() -> tuple[bool, bool]:
            return await state_scheduler.run_once_async(), await event_worker.run_once_async()

        state_delivered, event_delivered = asyncio.run(deliver())
        self.assertTrue(state_delivered)
        self.assertTrue(event_delivered)
        self.assertEqual(len(transport.event_deliveries), 1)

    def test_event_outbox_preserves_fifo_and_retries_rate_limits(self) -> None:
        self.inventory.grant_interaction("grant-event-1", actor_id="dm", item_name="Gem", quantity=1)
        self.inventory.grant_interaction("grant-event-2", actor_id="dm", item_name="Map", quantity=1)
        transport = FakeDiscordTransport()
        worker = EventOutboxWorker(self.store, transport)
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        self.assertEqual([entry[2]["sequence"] for entry in transport.event_deliveries], [1, 2])

        class LimitedTransport(FakeDiscordTransport):
            def deliver_event(self, destination, event_type, payload):
                raise RateLimitedError(20)

        self.inventory.grant_interaction("grant-event-3", actor_id="dm", item_name="Key", quantity=1)
        limited_worker = EventOutboxWorker(self.store, LimitedTransport())
        self.assertFalse(limited_worker.run_once())
        pending = self.store.connection.execute("SELECT attempt_count, next_attempt_at FROM event_outbox WHERE status = 'PENDING' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(pending["attempt_count"], 1)
        self.assertIsNotNone(pending["next_attempt_at"])


if __name__ == "__main__":
    unittest.main()
