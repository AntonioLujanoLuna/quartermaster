from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from quartermaster.characters import CharacterError, CharacterService
from quartermaster.combat import CombatService
from quartermaster.config import ConfigurationError, Settings
from quartermaster.currency import CurrencyError, CurrencySemanticStaleness, CurrencyService
from quartermaster.db import DATA_MIGRATIONS, MIGRATIONS, SCHEMA_VERSION, MigrationError, SQLiteStore
from quartermaster.discord_projection import DiscordProjectionTransport
from quartermaster.export import render_export
from quartermaster.handles import HandleError, HandleRepository
from quartermaster.inventory import InventoryError, InventoryService, SemanticStaleness
from quartermaster.loot import LootDropError, LootDropService
from quartermaster.operations import (
    create_backup,
    create_scheduled_backup,
    health_report,
    record_discord_surface_health,
    render_health,
    requeue_dead_letter_events,
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

# Migration 10 added the one-active-character-per-user index and renormalized
# stack names, so the databases that exercise it have to be built at 9. Naming
# the version keeps these tests pointed at the migration they are about instead
# of drifting onto whichever migration happens to be newest.
_BEFORE_ACTIVE_CHARACTER_RULE = 9


@contextmanager
def _schema_version(version: int):
    """Build and open databases as a build that only knows migrations up to `version`."""
    with (
        mock.patch.dict(
            MIGRATIONS,
            {number: script for number, script in MIGRATIONS.items() if number <= version},
            clear=True,
        ),
        mock.patch.dict(
            DATA_MIGRATIONS,
            {number: fix for number, fix in DATA_MIGRATIONS.items() if number <= version},
            clear=True,
        ),
        mock.patch("quartermaster.db.SCHEMA_VERSION", version),
    ):
        yield


class _StepClock:
    """A clock the outbox tests can push past a retry backoff deliberately."""

    def __init__(self) -> None:
        # Whole seconds so the millisecond-truncated ISO round-trip is exact, and
        # a second ahead so events queued at real `iso_now()` are already due.
        self.moment = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)

    def __call__(self) -> str:
        return self.moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


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

    def _holding(self, owner_type: str, owner_id: str, normalized_name: str) -> int:
        row = self.store.connection.execute(
            """SELECT COALESCE(SUM(quantity), 0) AS quantity FROM inventory_stacks
                WHERE owner_type = ? AND owner_id = ? AND normalized_name = ?""",
            (owner_type, owner_id, normalized_name),
        ).fetchone()
        return int(row["quantity"])

    def _currency_total(self) -> dict[str, int]:
        """Sum every denomination across every owner, party and characters alike."""
        row = self.store.connection.execute(
            """SELECT COALESCE(SUM(cp), 0) AS cp, COALESCE(SUM(sp), 0) AS sp,
                      COALESCE(SUM(ep), 0) AS ep, COALESCE(SUM(gp), 0) AS gp,
                      COALESCE(SUM(pp), 0) AS pp
                 FROM currency_balances"""
        ).fetchone()
        return {denomination: int(row[denomination]) for denomination in ("cp", "sp", "ep", "gp", "pp")}

    def _item_total(self) -> dict[str, int]:
        """Sum every item by name across stacks and the open drops holding them."""
        totals: dict[str, int] = {}
        rows = self.store.connection.execute(
            """SELECT normalized_name, SUM(quantity) AS quantity
                 FROM inventory_stacks GROUP BY normalized_name
               UNION ALL
               SELECT item.normalized_name, SUM(item.remaining_quantity) AS quantity
                 FROM loot_drop_items item
                 JOIN loot_drops open_drop ON open_drop.id = item.drop_id
                WHERE open_drop.status = 'OPEN'
                GROUP BY item.normalized_name"""
        ).fetchall()
        for row in rows:
            quantity = int(row["quantity"] or 0)
            if quantity:
                totals[str(row["normalized_name"])] = totals.get(str(row["normalized_name"]), 0) + quantity
        return totals

    def test_currency_operations_conserve_every_denomination(self) -> None:
        """Currency only moves between owners; no operation may create or destroy it."""
        self._insert_character("conserve-1", "Aria")
        self._insert_character("conserve-2", "Borin")
        self.currency.adjust_treasury_interaction(
            "conserve-seed", actor_id="dm", deltas={"cp": 7, "sp": 13, "gp": 100, "pp": 3}
        )
        seeded = self._currency_total()
        self.assertEqual(seeded, {"cp": 7, "sp": 13, "ep": 0, "gp": 100, "pp": 3})

        self.currency.give_to_character_interaction(
            "conserve-give", actor_id="dm", character_id="conserve-1", amounts={"gp": 1}
        )
        self.assertEqual(self._currency_total(), seeded)

        handle_id = self.currency.create_relative_split_handle(actor_id="dm", amounts={"gp": 30})
        self.currency.split_relative_interaction("conserve-relative", handle_id=handle_id, actor_id="dm")
        self.assertEqual(self._currency_total(), seeded)

        # An absolute split across three active characters leaves indivisible remainders.
        self.currency.split_treasury_interaction(
            "conserve-split", actor_id="dm", amounts={"cp": 7, "sp": 13, "gp": 60, "pp": 3}
        )
        self.assertEqual(self._currency_total(), seeded)

        self.characters.transition_interaction(
            "conserve-lifecycle", actor_id="dm", character_id="conserve-2", lifecycle="DEAD"
        )
        self.characters.resolve_belongings_interaction(
            "conserve-resolve", actor_id="dm", character_id="conserve-2", destination="party"
        )
        self.assertEqual(self._currency_total(), seeded)

        self.currency.give_from_character_interaction(
            "conserve-return", actor_id="player", amounts={"gp": 1}, destination="party"
        )
        self.assertEqual(self._currency_total(), seeded)

    def test_a_living_character_can_send_coin_back_to_the_treasury(self) -> None:
        """A mistyped give is repairable by returning the coin, not by minting more.

        Currency only ever moved towards a living character: a split credits
        every active one, Give to… credits one, and the only debit refuses an
        active character on purpose. The nearest repair a DM had — adjusting the
        treasury back up — creates the difference instead of returning it, so
        the campaign ended the evening richer than it started.
        """
        self.currency.adjust_treasury_interaction("mistype-seed", actor_id="dm", deltas={"gp": 100})
        seeded = self._currency_total()
        self.currency.give_to_character_interaction(
            "mistype-give", actor_id="dm", character_id="player-character", amounts={"gp": 90}
        )
        self.assertEqual(self.currency.view_treasury()["gp"], 10)

        returned = self.currency.give_from_character_interaction(
            "mistype-return", actor_id="player", amounts={"gp": 81}, destination="party"
        )
        response = returned.logical_response
        self.assertEqual(response["status"], "GIVEN")
        self.assertEqual(response["destination_name"], "the treasury")
        self.assertEqual(response["character_after"]["gp"], 9)
        self.assertEqual(self.currency.view_treasury()["gp"], 91)
        self.assertEqual(self._currency_total(), seeded)

    def test_coin_moves_between_active_characters_without_the_dm(self) -> None:
        self._insert_character("payee", "Payee")
        self.currency.adjust_treasury_interaction("peer-seed", actor_id="dm", deltas={"sp": 40})
        seeded = self._currency_total()
        self.currency.give_to_character_interaction(
            "peer-give", actor_id="dm", character_id="player-character", amounts={"sp": 40}
        )
        result = self.currency.give_from_character_interaction(
            "peer-hand", actor_id="player", amounts={"sp": 15}, destination="payee"
        )
        self.assertEqual(result.logical_response["destination_name"], "Payee")
        self.assertEqual(result.logical_response["character_after"]["sp"], 25)
        self.assertEqual(result.logical_response["destination_after"]["sp"], 15)
        self.assertEqual(self._currency_total(), seeded)
        # The pinned surface renders the treasury, which this transfer did not touch.
        self.assertEqual(self.currency.view_treasury()["sp"], 0)

    def test_giving_coin_refuses_what_the_character_is_not_carrying(self) -> None:
        self._insert_character("dead-payee", "Dead Payee", lifecycle="DEAD")
        self.currency.adjust_treasury_interaction("refuse-seed", actor_id="dm", deltas={"gp": 5})
        self.currency.give_to_character_interaction(
            "refuse-give", actor_id="dm", character_id="player-character", amounts={"gp": 5}
        )
        seeded = self._currency_total()

        for interaction_id, kwargs, expected in (
            ("refuse-overdraw", {"actor_id": "player", "amounts": {"gp": 6}}, "carrying only"),
            ("refuse-nobody", {"actor_id": "stranger", "amounts": {"gp": 1}}, "active registered character"),
            (
                "refuse-dead",
                {"actor_id": "player", "amounts": {"gp": 1}, "destination": "dead-payee"},
                "only active characters",
            ),
            (
                "refuse-self",
                {"actor_id": "player", "amounts": {"gp": 1}, "destination": "player-character"},
                "must differ",
            ),
            (
                "refuse-missing",
                {"actor_id": "player", "amounts": {"gp": 1}, "destination": "no-such-character"},
                "not found",
            ),
            ("refuse-nothing", {"actor_id": "player", "amounts": {"gp": 0}}, "at least one"),
            ("refuse-negative", {"actor_id": "player", "amounts": {"gp": -1}}, "non-negative"),
        ):
            with self.subTest(interaction_id):
                with self.assertRaisesRegex(CurrencyError, expected):
                    self.currency.give_from_character_interaction(interaction_id, **kwargs)
                self.assertIsNone(
                    self.store.connection.execute(
                        "SELECT 1 FROM interaction_receipts WHERE interaction_id = ?", (interaction_id,)
                    ).fetchone()
                )
        self.assertEqual(self._currency_total(), seeded)

    def test_take_transfers_ownership_and_requires_a_registered_character(self) -> None:
        self.inventory.grant_interaction(
            "ownership-grant", actor_id="dm", item_name="Owned Relic", quantity=2
        )
        prepared = self.inventory.prepare_take_view(actor_id="player")
        stack_id = prepared["items"][0]["id"]
        taken = self.inventory.take_interaction(
            "ownership-take", handle_id=prepared["handles"][stack_id], actor_id="player"
        )
        self.assertEqual(taken.logical_response["character_id"], "player-character")
        held = self.store.connection.execute(
            """SELECT quantity FROM inventory_stacks
                WHERE owner_type = 'CHARACTER' AND owner_id = 'player-character'
                  AND normalized_name = 'owned relic'"""
        ).fetchone()
        self.assertEqual(held["quantity"], 1)

        unregistered = self.inventory.create_take_handle(
            stack_id=stack_id, actor_id="unregistered", amount=1
        )
        with self.assertRaisesRegex(InventoryError, "active registered character"):
            self.inventory.take_interaction(
                "ownership-unregistered", handle_id=unregistered, actor_id="unregistered"
            )

    def test_give_moves_held_items_back_to_the_party_and_between_characters(self) -> None:
        """Possession has to move both ways, and only ever move.

        Before Give, a take was final: nothing could return an item to the
        stash, and `/grant` is not that path — it mints a new item, so undoing a
        misread `Take all` with it inflates the campaign's inventory. Every
        assertion here is about the total staying put while ownership changes.
        """
        self._insert_character("give-recipient", "Berrian")
        self.inventory.grant_interaction(
            "give-grant", actor_id="dm", item_name="Silvered Dagger", quantity=5
        )
        prepared = self.inventory.prepare_take_view(actor_id="player")
        take_all = prepared["take_all_handles"][prepared["items"][0]["id"]]
        self.inventory.take_interaction("give-take-all", handle_id=take_all, actor_id="player")
        total = self._item_total()
        self.assertEqual(total, {"silvered dagger": 5})
        self.assertEqual(self._holding("PARTY", "party", "silvered dagger"), 0)

        returned = self.inventory.give_interaction(
            "give-back", actor_id="player", item_name="Silvered Dagger", quantity=3
        )
        self.assertEqual(returned.logical_response["destination_name"], "the Party Stash")
        self.assertEqual(returned.logical_response["remaining"], 2)
        self.assertEqual(self._item_total(), total)
        self.assertEqual(self._holding("PARTY", "party", "silvered dagger"), 3)
        self.assertEqual(self._holding("CHARACTER", "player-character", "silvered dagger"), 2)

        handed_on = self.inventory.give_interaction(
            "give-onward",
            actor_id="player",
            item_name="silvered  DAGGER",
            quantity=2,
            destination="give-recipient",
        )
        self.assertEqual(handed_on.logical_response["destination_name"], "Berrian")
        self.assertEqual(self._item_total(), total)
        self.assertEqual(self._holding("CHARACTER", "give-recipient", "silvered dagger"), 2)
        # The emptied stack is gone rather than left at zero, so browse and the
        # export do not carry a row for something nobody holds.
        self.assertEqual(self._holding("CHARACTER", "player-character", "silvered dagger"), 0)

    def test_give_refuses_what_the_giver_cannot_actually_hand_over(self) -> None:
        self._insert_character("give-dead", "Wraith", lifecycle="DEAD")
        self.inventory.grant_interaction(
            "give-refuse-grant", actor_id="dm", item_name="Brass Key", quantity=1
        )
        prepared = self.inventory.prepare_take_view(actor_id="player")
        self.inventory.take_interaction(
            "give-refuse-take",
            handle_id=prepared["handles"][prepared["items"][0]["id"]],
            actor_id="player",
        )
        before = self._item_total()

        with self.assertRaisesRegex(InventoryError, "not holding"):
            self.inventory.give_interaction(
                "give-missing", actor_id="player", item_name="Iron Key", quantity=1
            )
        with self.assertRaisesRegex(InventoryError, "holds only 1"):
            self.inventory.give_interaction(
                "give-too-many", actor_id="player", item_name="Brass Key", quantity=2
            )
        with self.assertRaisesRegex(InventoryError, "active registered character"):
            self.inventory.give_interaction(
                "give-unregistered", actor_id="stranger", item_name="Brass Key", quantity=1
            )
        # Specification 32.1: a non-active character cannot ordinarily receive.
        with self.assertRaisesRegex(InventoryError, "only active characters"):
            self.inventory.give_interaction(
                "give-to-dead",
                actor_id="player",
                item_name="Brass Key",
                quantity=1,
                destination="give-dead",
            )
        with self.assertRaisesRegex(InventoryError, "must differ"):
            self.inventory.give_interaction(
                "give-to-self",
                actor_id="player",
                item_name="Brass Key",
                quantity=1,
                destination="player-character",
            )
        self.assertEqual(self._item_total(), before)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM interaction_receipts"
            ).fetchone()[0],
            2,
            "a refused give must leave no receipt behind",
        )

    def test_every_domain_event_type_has_a_renderer(self) -> None:
        """An event with no renderer still delivers — as raw JSON, at the table.

        The fallback exists so an unrenderable event cannot block its
        destination, which means nothing fails when a new event type ships
        without a line of its own: the session log simply starts printing
        internal UUIDs and Discord user IDs. Four event types had already
        arrived that way. This reads the event types the package actually
        appends, so the next one cannot.
        """
        from quartermaster.discord_projection import _EVENT_RENDERERS

        source_root = Path(__file__).parents[1] / "src" / "quartermaster"
        appended = {
            match
            for path in source_root.glob("*.py")
            for match in re.findall(r'event_type="([A-Z_]+)"', path.read_text(encoding="utf-8"))
        }
        self.assertTrue(appended, "no domain events were found to check")
        self.assertEqual(
            appended - set(_EVENT_RENDERERS),
            set(),
            "these event types would be delivered to Discord as raw JSON",
        )

    def test_browse_handles_fit_the_control_budget_and_cover_a_leading_run(self) -> None:
        """Handles must describe controls that can actually exist, in reading order.

        A stack above one needs two buttons and one view holds twenty-five, so
        minting a handle per stack promised controls the view could never
        render. Minting them for a leading run instead keeps the controls lined
        up with the top of the list rather than skipping a stack in the middle,
        which reads as an item that cannot be taken at all.
        """
        # Granted newest first so the browse order — most recently acquired,
        # then by name — is the same either way the tie breaks.
        for index in reversed(range(6)):
            self.inventory.grant_interaction(
                f"budget-grant-{index}",
                actor_id="dm",
                item_name=f"Budget Relic {index}",
                # Alternating, so the fill runs out of room on a two-control
                # stack with one slot still free and a one-control stack behind it.
                quantity=1 if index % 2 == 0 else 2,
            )
        prepared = self.inventory.prepare_take_view(actor_id="player", limit=6, control_budget=5)
        self.assertEqual([item["item_name"] for item in prepared["items"]][:5], [f"Budget Relic {index}" for index in range(5)])
        controls = len(prepared["handles"]) + len(prepared["take_all_handles"])
        self.assertEqual(controls, 4)
        controlled = [item["id"] in prepared["handles"] for item in prepared["items"]]
        self.assertEqual(controlled, [True, True, True, False, False, False])
        self.assertTrue(
            set(prepared["take_all_handles"]) <= set(prepared["handles"]),
            "a stack was offered Take all without Take 1",
        )

    def test_item_operations_conserve_quantities_across_owners_and_drops(self) -> None:
        """Taking, claiming, and closing move items between holders without loss."""
        self.inventory.grant_interaction(
            "conserve-grant", actor_id="dm", item_name="Conservation Token", quantity=6
        )
        granted = self._item_total()
        self.assertEqual(granted, {"conservation token": 6})

        prepared = self.inventory.prepare_take_view(actor_id="player")
        handle_id = prepared["handles"][prepared["items"][0]["id"]]
        self.inventory.take_interaction("conserve-take", handle_id=handle_id, actor_id="player")
        self.assertEqual(self._item_total(), granted)

        drop = self.loot.create_drop_interaction(
            "conserve-drop", actor_id="dm", items=[("Drop Token", 5, None)]
        )
        with_drop = self._item_total()
        self.assertEqual(with_drop["drop token"], 5)

        claim = self.loot.prepare_claim_view(actor_id="player")
        claim_handle = claim["handles"][claim["drops"][0]["items"][0]["id"]]
        self.loot.claim_interaction("conserve-claim", handle_id=claim_handle, actor_id="player")
        self.assertEqual(self._item_total(), with_drop)

        self.loot.close_drop_interaction(
            "conserve-close", drop_id=drop.logical_response["drop_id"], actor_id="dm"
        )
        self.assertEqual(self._item_total(), with_drop)

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

        from quartermaster.discord_common import _is_dm

        settings = Settings(guild_id="42", database_path=self.db_path)
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=42), user=SimpleNamespace(id=42))
        self.assertTrue(asyncio.run(_is_dm(interaction, settings)))

    def test_dm_authorization_accepts_configured_role_and_rejects_other_members(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import discord

        member = MagicMock(spec=discord.Member)
        member.id = 7
        member.guild_permissions.manage_guild = False
        member.roles = [SimpleNamespace(id=44)]
        interaction = SimpleNamespace(guild=SimpleNamespace(owner_id=99), user=member)
        allowed = Settings(guild_id="123", database_path=self.db_path, dm_role_ids=("44",))
        denied = Settings(guild_id="123", database_path=self.db_path, dm_role_ids=("55",))
        from quartermaster.discord_common import _is_dm

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
        self.assertEqual(
            recover_startup(self.store, self.receipts, self.handles)["failed_deferred_receipts"], 1
        )
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
        self.assertTrue(controller.should_fallback_to_deferred(can_defer=True, now=101.2))
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
        from quartermaster.discord_common import _run_fast

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

        result = asyncio.run(_run_fast(interaction, settings, operation))
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

    def test_export_holds_the_record_every_truncated_surface_points_at(self) -> None:
        """Each list surface tells the reader the export holds the full record.

        It did not. An open Loot Drop's items live nowhere but `loot_drop_items`
        until the drop closes, so the document had no trace of loot the party
        could still claim. Ownership moves to a character on every take, and the
        holder was rendered as a bare UUID. The roster only appeared for
        characters that happened to hold currency.
        """
        self.inventory.grant_interaction(
            "export-grant", actor_id="dm", item_name="Moonlit Blade", quantity=1
        )
        prepared = self.inventory.prepare_take_view(actor_id="player")
        self.inventory.take_interaction(
            "export-take",
            handle_id=prepared["handles"][prepared["items"][0]["id"]],
            actor_id="player",
        )
        self.loot.create_drop_interaction(
            "export-drop", actor_id="dm", items=[("Unclaimed Idol", 3, "Temple hoard")]
        )
        output = render_export(self.store)

        self.assertIn("Moonlit Blade x1 (held by Player Character)", output)
        self.assertNotIn("(CHARACTER:player-character)", output)
        self.assertIn("Unclaimed Idol: 3 unclaimed of 3", output)
        self.assertIn("Player Character [ACTIVE]", output)

    def test_export_keeps_the_combat_record_after_the_session_closes(self) -> None:
        """The fight has to still be in the record once the session ends.

        Encounters were read against the active session only, so `/session-end`
        — which closes the encounter and the session together — emptied the
        combat section of the one document that is supposed to survive an
        outage, at exactly the moment the DM writes up what happened.
        """
        combat = CombatService(self.store, self.receipts)
        sessions = SessionService(self.store, self.receipts, self.loot, combat)
        started = sessions.start_interaction("combat-export-start", actor_id="dm")
        combat.open_interaction("combat-export-open", actor_id="dm", channel_id="9001")
        combat.close_interaction("combat-export-close", actor_id="dm", outcome="Owlbear routed")

        during = render_export(self.store)
        self.assertIn(
            f"### Combat encounters in session {started.logical_response['session_number']}", during
        )
        self.assertIn("Owlbear routed", during)

        sessions.end_interaction("combat-export-end", actor_id="dm", where_ended="The ridge")
        after = render_export(self.store)
        self.assertIn("Owlbear routed", after)
        self.assertIn("in channel 9001", after)

    def test_export_says_so_when_there_is_no_open_loot_or_roster(self) -> None:
        with self.store.transaction() as connection:
            connection.execute("DELETE FROM characters")
        output = render_export(self.store)
        self.assertIn("- No open Loot Drops.", output)
        self.assertIn("- No characters registered.", output)

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
        self.assertEqual(result.logical_response["distributed"]["gp"], 80)
        self.assertEqual(len(result.logical_response["recipients"]), 4)
        # Specification 33.1: the indivisible remainder stays with the source.
        self.assertEqual(self.currency.view_treasury()["gp"], 1)
        self.assertEqual(result.logical_response["after"]["gp"], 1)
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
        # 81 gp held, 78 gp distributed three ways, 1 gp unsplit plus a 2 gp remainder.
        self.assertEqual(self.currency.view_treasury()["gp"], 3)

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

    def test_restore_migrates_an_older_backup_without_mutating_it(self) -> None:
        """A snapshot taken before the newest migration must still be restorable.

        The older database is built by running only the migrations up to the
        previous version, rather than by hand-undoing whichever artifacts the
        newest migration happens to create. That kept needing an edit per
        migration, and silently stopped simulating anything when it fell behind.
        """
        older_version = SCHEMA_VERSION - 1
        older_migrations = {
            version: script for version, script in MIGRATIONS.items() if version <= older_version
        }
        older_data_migrations = {
            version: fix for version, fix in DATA_MIGRATIONS.items() if version <= older_version
        }
        backup = Path(self.tempdir.name) / "older-backup.sqlite"
        with (
            mock.patch.dict(MIGRATIONS, older_migrations, clear=True),
            mock.patch.dict(DATA_MIGRATIONS, older_data_migrations, clear=True),
            mock.patch("quartermaster.db.SCHEMA_VERSION", older_version),
        ):
            older_path = Path(self.tempdir.name) / "older-source.sqlite"
            with SQLiteStore(older_path).open() as older_store:
                InventoryService(
                    older_store, ReceiptRepository(older_store), HandleRepository(older_store)
                ).grant_interaction("older-grant", actor_id="dm", item_name="Older Token", quantity=1)
                older_store.snapshot(backup)

        restored = restore_backup(backup, Path(self.tempdir.name) / "restored-older.sqlite")
        self.assertEqual(restored["source_schema_version"], SCHEMA_VERSION - 1)
        self.assertEqual(restored["schema_version"], SCHEMA_VERSION)
        with SQLiteStore(Path(self.tempdir.name) / "restored-older.sqlite").open() as restored_store:
            self.assertIn("Older Token x1", render_export(restored_store))
        # The archived snapshot keeps the schema it was taken at.
        aged = sqlite3.connect(backup)
        try:
            self.assertEqual(
                aged.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                SCHEMA_VERSION - 1,
            )
        finally:
            aged.close()
        self.assertFalse(
            (Path(self.tempdir.name) / "restored-older.sqlite.restore-staging").exists()
        )

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

    def test_discord_adapter_registers_one_entry_point(self) -> None:
        """The surface is a panel, so there is exactly one thing to type.

        Every capability used to be its own command with its own arguments, and
        the table had to remember all of them. Anything that reappears in this
        set is a capability that has slipped back out of the panel.
        """
        import discord

        from quartermaster.discord_adapter import BotServices, create_bot

        services = BotServices(self.store, self.receipts, self.inventory, self.sessions)
        bot = create_bot(Settings(guild_id="123", database_path=self.db_path), services)
        commands = bot.tree.get_commands(guild=discord.Object(id=123))
        self.assertEqual({command.name for command in commands}, {"quartermaster"})

    def test_home_panel_summarizes_what_the_caller_can_act_on(self) -> None:
        from quartermaster.avrae_handoff import AvraeHandoffService
        from quartermaster.discord_common import BotServices, Quartermaster
        from quartermaster.discord_panels import _home_snapshot, _render_home

        self.inventory.grant_interaction(
            "home-grant",
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
        context = Quartermaster(
            services=services,
            settings=Settings(guild_id="123", database_path=self.db_path),
            characters=self.characters,
            currency=self.currency,
            loot=self.loot,
            combat=CombatService(self.store, self.receipts),
            handoff=AvraeHandoffService(self.store),
        )

        snapshot = _home_snapshot(context, "player")
        self.assertEqual(snapshot["stash_count"], 1)
        self.assertEqual(snapshot["active_session_number"], 1)
        self.assertEqual(snapshot["character"], {"id": "player-character", "name": "Player Character"})

        rendered = _render_home(snapshot, is_dm=False)
        self.assertIn("Party Stash · 1 stack", rendered)
        self.assertIn("Session 1 · in progress", rendered)
        self.assertIn("You are playing **Player Character**", rendered)

    def test_discord_response_helper_omits_empty_view(self) -> None:
        from quartermaster.discord_common import _send_execution

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

        from quartermaster.discord_common import _send_deferred_backup

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
        from types import SimpleNamespace

        import discord

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

    def test_party_stash_delivery_survives_a_pin_permission_failure(self) -> None:
        """An unpinnable channel must not turn the projection into a message spammer.

        Pinning needs Manage Messages. When the pin failed the delivery, the
        message id was never recorded, so the next attempt sent another Party
        Stash instead of editing the one already posted — one duplicate per
        retry, forever, with the projection never converging. The pin is where
        the surface sits; the message is the surface.
        """
        from types import SimpleNamespace

        import discord

        class Message:
            def __init__(self, message_id: int) -> None:
                self.id = message_id
                self.pinned = False
                self.edits: list[str] = []

            async def pin(self, *, reason: str) -> None:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "pin denied")

            async def edit(self, *, content: str) -> None:
                self.edits.append(content)

        class Channel:
            def __init__(self) -> None:
                self.sent: list[Message] = []
                self.message = Message(456)

            async def send(self, _content: str) -> Message:
                self.sent.append(self.message)
                return self.message

            async def fetch_message(self, message_id: int) -> Message:
                if message_id != self.message.id:
                    raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
                return self.message

        channel = Channel()

        class Bot:
            async def fetch_channel(self, _channel_id: int) -> Channel:
                return channel

        transport = DiscordProjectionTransport(
            Bot(),
            Settings(guild_id="123", database_path=self.db_path, party_inventory_channel_id="789"),
            self.store,
        )
        payload = {"items": [], "loot_drops": []}
        with self.assertLogs("quartermaster.discord_projection", level="WARNING") as logs:
            first = asyncio.run(transport.upsert_state("party-stash", "party-inventory", payload, None))
            second = asyncio.run(transport.upsert_state("party-stash", "party-inventory", payload, first))
        self.assertEqual(first, "456")
        self.assertEqual(second, "456")
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(len(channel.message.edits), 1)
        # Not silently ignored: the operator is told what to repair, every time
        # the surface tries, so the log says so for as long as it is true.
        self.assertEqual(len(logs.output), 2)
        self.assertIn("Manage Messages", logs.output[0])

    def test_projection_runner_performs_scheduled_backup_and_surface_check(self) -> None:
        from quartermaster.discord_projection import ProjectionRunner

        class Transport(FakeDiscordTransport):
            async def check_surface_reachability(self) -> dict[str, object]:
                return {"surfaces": {"party-inventory": {"reachable": True}}}

        primary = Path(self.tempdir.name) / "runner-backups"
        self.inventory.grant_interaction(
            "runner-projection-grant",
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
        from types import SimpleNamespace

        import discord

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
        self.sessions.start_session()
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
        self.loot.create_drop_interaction(
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

    def test_state_projection_keeps_work_committed_during_delivery(self) -> None:
        """A mutation that lands mid-delivery must still be rendered afterwards.

        Retiring the revision read *after* the Discord round-trip credited the
        in-flight payload with that mutation and cleared dirty_since with it, so
        the change was never drawn and health still reported a clean projection.
        """
        self.inventory.grant_interaction("race-1", actor_id="dm", item_name="Rope", quantity=1)
        inventory = self.inventory

        class RacingTransport(FakeDiscordTransport):
            """A grant lands while the Discord call for the previous state is open."""

            def upsert_state(self, target_id, destination, payload, message_id):
                inventory.grant_interaction("race-2", actor_id="dm", item_name="Torch", quantity=5)
                return super().upsert_state(target_id, destination, payload, message_id)

        scheduler = StateProjectionScheduler(self.store, RacingTransport())
        self.assertTrue(scheduler.run_once())
        target = self.store.connection.execute(
            "SELECT dirty_since, desired_revision, delivered_revision FROM projection_targets WHERE target_id = 'party-stash'"
        ).fetchone()
        self.assertIsNotNone(target["dirty_since"])
        self.assertGreater(target["desired_revision"], target["delivered_revision"])
        self.assertEqual(health_report(self.store)["checks"]["state_projections"], "DEGRADED")

        transport = FakeDiscordTransport()
        scheduler = StateProjectionScheduler(self.store, transport)
        self.assertTrue(scheduler.run_once())
        rendered = {item["item_name"] for item in transport.state_payloads["party-stash"]["items"]}
        self.assertEqual(rendered, {"Rope", "Torch"})
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT dirty_since FROM projection_targets WHERE target_id = 'party-stash'"
            ).fetchone()["dirty_since"]
        )

    def test_projection_claim_abandoned_by_a_crash_is_released_at_startup(self) -> None:
        """A crash between claiming a target and recording the delivery is survivable.

        The claim is only ever cleared by the process that took it, and the
        scheduler skips claimed targets, so without a release on the startup path
        the Party Stash stops updating permanently — restarting does not help.
        """
        self.inventory.grant_interaction("claim-crash-1", actor_id="dm", item_name="Chalk", quantity=1)
        scheduler = StateProjectionScheduler(self.store, FakeDiscordTransport())
        self.assertIsNotNone(scheduler._claim_next_target())
        self.assertEqual(
            self.store.connection.execute(
                "SELECT in_flight FROM projection_targets WHERE target_id = 'party-stash'"
            ).fetchone()["in_flight"],
            1,
        )

        recovery = recover_startup(self.store, self.receipts, self.handles)
        self.assertEqual(recovery["released_projection_claims"], 1)

        transport = FakeDiscordTransport()
        self.assertTrue(StateProjectionScheduler(self.store, transport).run_once())
        self.assertEqual(transport.state_payloads["party-stash"]["items"][0]["item_name"], "Chalk")

    def test_projection_claim_left_behind_mid_run_is_reclaimed_after_its_lease(self) -> None:
        """Recording a delivery outcome can fail; the claim must not outlive the run.

        Startup release covers a restart. This covers the same target being
        stranded while the process stays up, which no restart is coming to fix.
        """
        self.inventory.grant_interaction("claim-lease-1", actor_id="dm", item_name="Whetstone", quantity=1)
        clock = _StepClock()
        scheduler = StateProjectionScheduler(self.store, FakeDiscordTransport(), now=clock)
        self.assertIsNotNone(scheduler._claim_next_target())
        # The delivery outcome never gets recorded: in_flight stays set.
        self.assertIsNone(scheduler._claim_next_target())

        clock.advance(301)
        transport = FakeDiscordTransport()
        recovered = StateProjectionScheduler(self.store, transport, now=clock)
        self.assertTrue(recovered.run_once())
        self.assertEqual(transport.state_payloads["party-stash"]["items"][0]["item_name"], "Whetstone")
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT dirty_since FROM projection_targets WHERE target_id = 'party-stash'"
            ).fetchone()["dirty_since"]
        )

    def test_projection_runner_survives_a_failed_delivery_iteration(self) -> None:
        """One transient database error must cost an iteration, not the runner.

        Delivery was the only unguarded step in the loop, so an error there ended
        the task while the bot stayed online: no projection and no event reached
        Discord again until someone restarted the process.
        """
        from quartermaster.discord_projection import ProjectionRunner

        self.inventory.grant_interaction("runner-guard-1", actor_id="dm", item_name="Spade", quantity=1)

        class Transport(FakeDiscordTransport):
            async def check_surface_reachability(self) -> dict[str, object]:
                return {"surfaces": {}}

            async def upsert_state(self, target_id, destination, payload, message_id):
                return super().upsert_state(target_id, destination, payload, message_id)

            async def deliver_event(self, destination, event_type, payload):
                super().deliver_event(destination, event_type, payload)

        transport = Transport()
        runner = ProjectionRunner(
            self.store,
            transport,
            maintenance_interval_seconds=3600,
            backup_interval_seconds=3600,
        )
        failures = {"remaining": 1}
        original_claim = StateProjectionScheduler._claim_next_target

        def flaky_claim(scheduler_self):
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return original_claim(scheduler_self)

        async def run_runner() -> None:
            stop_event = asyncio.Event()
            task = asyncio.create_task(runner.run(stop_event))
            await asyncio.sleep(0.2)
            self.assertFalse(task.done(), "the runner must outlive a failed iteration")
            stop_event.set()
            await task

        with mock.patch.object(StateProjectionScheduler, "_claim_next_target", flaky_claim):
            with self.assertLogs("quartermaster.discord_projection", level="ERROR"):
                asyncio.run(run_runner())

        self.assertEqual(failures["remaining"], 0)
        self.assertEqual(transport.state_payloads["party-stash"]["items"][0]["item_name"], "Spade")
        self.assertTrue(transport.event_deliveries)

    def test_session_thread_binding_keeps_database_work_off_the_event_loop(self) -> None:
        """The transport shares one connection with every interaction thread.

        Reading or writing it directly from the event loop lets an ordinary
        interaction's write block discord.py's heartbeat for as long as it holds
        the store, so every database call this transport makes belongs in a
        worker thread.
        """
        session = self.sessions.start_session()
        loop_thread = threading.get_ident()
        touched_from: list[int] = []

        class Thread:
            id = 4242

        class Channel:
            async def create_thread(self, **_kwargs):
                return Thread()

            async def send(self, _content):
                return None

        class Bot:
            async def fetch_channel(self, _channel_id):
                return Channel()

        settings = Settings(
            guild_id="1",
            database_path=self.db_path,
            party_inventory_channel_id="10",
            session_log_channel_id="11",
        )
        transport = DiscordProjectionTransport(Bot(), settings, self.store)
        original_read, original_transaction = SQLiteStore.read, SQLiteStore.transaction

        def traced_read(store_self):
            touched_from.append(threading.get_ident())
            return original_read(store_self)

        def traced_transaction(store_self, *, immediate: bool = True):
            touched_from.append(threading.get_ident())
            return original_transaction(store_self, immediate=immediate)

        async def deliver() -> None:
            with mock.patch.object(SQLiteStore, "read", traced_read):
                with mock.patch.object(SQLiteStore, "transaction", traced_transaction):
                    await transport.deliver_event(
                        f"session:{session['session_id']}", "SESSION_STARTED", {"session_number": 1}
                    )

        asyncio.run(deliver())
        self.assertTrue(touched_from, "the delivery should have read the session thread binding")
        self.assertNotIn(loop_thread, touched_from)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT discord_thread_id FROM sessions WHERE id = ?", (session["session_id"],)
            ).fetchone()["discord_thread_id"],
            "4242",
        )

    def test_one_active_character_per_discord_user_is_enforced(self) -> None:
        """Two active characters for one player would take two shares of a split."""
        self.characters.create_interaction("dup-1", actor_id="dm", name="Aria", discord_user_id="user-1")
        with self.assertRaisesRegex(CharacterError, "Aria is already active"):
            self.characters.create_interaction("dup-2", actor_id="dm", name="Borin", discord_user_id="user-1")

        # The database holds the rule even if a caller bypasses the service.
        with self.assertRaises(sqlite3.IntegrityError), self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO characters(id, name, discord_user_id, lifecycle, created_at, updated_at)
                   VALUES ('sneaky', 'Sneaky', 'user-1', 'ACTIVE', '2026-08-11T00:00:00Z', '2026-08-11T00:00:00Z')"""
            )

        # Characters with no Discord user attached are unconstrained.
        self.characters.create_interaction("dup-3", actor_id="dm", name="Unbound", discord_user_id=None)
        self.characters.create_interaction("dup-4", actor_id="dm", name="Also Unbound", discord_user_id=None)

    def test_reactivating_a_character_cannot_create_a_second_active_one(self) -> None:
        first = self.characters.create_interaction(
            "revive-1", actor_id="dm", name="Aria", discord_user_id="user-1"
        ).logical_response
        self.characters.transition_interaction(
            "revive-2", actor_id="dm", character_id=first["character_id"], lifecycle="DEAD"
        )
        replacement = self.characters.create_interaction(
            "revive-3", actor_id="dm", name="Borin", discord_user_id="user-1"
        ).logical_response
        self.assertEqual(replacement["lifecycle"], "ACTIVE")

        with self.assertRaisesRegex(CharacterError, "Borin is already active"):
            self.characters.transition_interaction(
                "revive-4", actor_id="dm", character_id=first["character_id"], lifecycle="ACTIVE"
            )

    def test_treasury_split_gives_each_player_exactly_one_share(self) -> None:
        """The split is per active character, so the one-per-player rule is what makes it fair."""
        self.characters.create_interaction("split-a", actor_id="dm", name="Aria", discord_user_id="user-1")
        self.characters.create_interaction("split-b", actor_id="dm", name="Cade", discord_user_id="user-2")
        self.currency.adjust_treasury_interaction("split-fund", actor_id="dm", deltas={"gp": 300})
        result = self.currency.split_treasury_interaction(
            "split-run", actor_id="dm", amounts={"gp": 300}
        ).logical_response

        shares: dict[str, int] = {}
        with self.store.read() as connection:
            for row in connection.execute(
                """SELECT characters.discord_user_id AS player, balances.gp AS gp
                     FROM currency_balances AS balances
                     JOIN characters ON characters.id = balances.owner_id
                    WHERE balances.owner_type = 'CHARACTER' AND characters.discord_user_id IS NOT NULL"""
            ):
                shares[row["player"]] = shares.get(row["player"], 0) + int(row["gp"])
        # setUp seeds one more player, so three players share the 300gp.
        per_share = result["per_recipient"]["gp"]
        self.assertEqual(sorted(shares), ["player", "user-1", "user-2"])
        self.assertEqual(set(shares.values()), {per_share})
        self.assertEqual(per_share * len(shares), 300)

    def test_undeliverable_event_is_dead_lettered_instead_of_blocking_its_destination(self) -> None:
        """One poisoned event must not hold the per-destination FIFO gate shut forever."""
        self.inventory.grant_interaction("poison-1", actor_id="dm", item_name="Gem", quantity=1)
        self.inventory.grant_interaction("poison-2", actor_id="dm", item_name="Map", quantity=1)

        class PoisonTransport(FakeDiscordTransport):
            def deliver_event(self, destination, event_type, payload):
                if payload.get("item_name") == "Gem":
                    raise RuntimeError("thread was deleted")
                return super().deliver_event(destination, event_type, payload)

        clock = _StepClock()
        worker = EventOutboxWorker(self.store, PoisonTransport(), now=clock, max_failures=3)
        for _ in range(3):
            self.assertFalse(worker.run_once())
            clock.advance(600)  # past whatever backoff the retry scheduled
        poisoned = self.store.connection.execute(
            "SELECT status, failure_count, failed_at, last_error FROM event_outbox ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertEqual(poisoned["status"], "FAILED")
        self.assertEqual(poisoned["failure_count"], 3)
        self.assertIsNotNone(poisoned["failed_at"])
        self.assertIn("thread was deleted", poisoned["last_error"])

        # The event behind it is no longer stuck.
        self.assertTrue(worker.run_once())
        self.assertEqual(
            [entry[2]["item_name"] for entry in worker.transport.event_deliveries], ["Map"]
        )

        report = health_report(self.store)
        self.assertEqual(report["checks"]["event_dead_letters"], "FAILED")
        self.assertEqual(report["counts"]["dead_lettered_events"], 1)
        self.assertEqual(report["status"], "FAILED")

    def test_dead_letters_return_to_the_queue_only_when_an_operator_requeues_them(self) -> None:
        self.inventory.grant_interaction("requeue-1", actor_id="dm", item_name="Gem", quantity=1)

        class BrokenTransport(FakeDiscordTransport):
            def deliver_event(self, destination, event_type, payload):
                raise RuntimeError("permission revoked")

        worker = EventOutboxWorker(self.store, BrokenTransport(), max_failures=1)
        self.assertFalse(worker.run_once())
        self.assertFalse(worker.run_once())  # dead-lettered, so nothing left to attempt

        working = FakeDiscordTransport()
        self.assertFalse(EventOutboxWorker(self.store, working).run_once())
        self.assertEqual(requeue_dead_letter_events(self.store), 1)
        self.assertTrue(EventOutboxWorker(self.store, working).run_once())
        self.assertEqual(len(working.event_deliveries), 1)
        self.assertEqual(health_report(self.store)["counts"]["dead_lettered_events"], 0)

    def test_rate_limits_do_not_spend_the_dead_letter_budget(self) -> None:
        """Being rate limited is the transport working, not the event being poison."""
        self.inventory.grant_interaction("limited-1", actor_id="dm", item_name="Gem", quantity=1)

        class LimitedTransport(FakeDiscordTransport):
            def deliver_event(self, destination, event_type, payload):
                raise RateLimitedError(5)

        clock = _StepClock()
        worker = EventOutboxWorker(self.store, LimitedTransport(), now=clock, max_failures=2)
        for _ in range(5):
            self.assertFalse(worker.run_once())
            clock.advance(600)
        row = self.store.connection.execute(
            "SELECT status, attempt_count, failure_count FROM event_outbox ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["failure_count"], 0)
        self.assertEqual(row["attempt_count"], 5)

    def test_hard_failures_back_off_exponentially(self) -> None:
        self.inventory.grant_interaction("backoff-1", actor_id="dm", item_name="Gem", quantity=1)

        class BrokenTransport(FakeDiscordTransport):
            def deliver_event(self, destination, event_type, payload):
                raise RuntimeError("nope")

        clock = _StepClock()
        worker = EventOutboxWorker(self.store, BrokenTransport(), now=clock, max_failures=10)
        delays = []
        for _ in range(4):
            before = clock.moment
            worker.run_once()
            row = self.store.connection.execute(
                "SELECT next_attempt_at FROM event_outbox ORDER BY id LIMIT 1"
            ).fetchone()
            scheduled = datetime.fromisoformat(row["next_attempt_at"].replace("Z", "+00:00"))
            delays.append((scheduled - before).total_seconds())
            clock.advance(600)
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0])

    def test_party_stash_projection_stays_within_discord_message_limit(self) -> None:
        """The permanent surface must keep rendering as the campaign accumulates.

        Discord refuses content over 2000 characters, and it refuses it the same
        way every time. Unbounded rendering therefore does not degrade the Party
        Stash, it retires it: every later delivery fails identically while the
        bot stays online and answers commands.
        """
        from quartermaster.discord_projection import _content_for_state
        from quartermaster.projections import render_state
        from quartermaster.rendering import DISCORD_MESSAGE_LIMIT

        for index in range(200):
            self.inventory.grant_interaction(
                f"overflow-grant-{index}",
                actor_id="dm",
                item_name=f"Curiosity of the Deep Vault number {index}",
                quantity=index + 1,
            )
        self.loot.create_drop_interaction(
            "overflow-drop", actor_id="dm", items=[("Fresh Loot Token", 1, None)]
        )

        content = _content_for_state("party-stash", render_state(self.store, "party-stash"))
        self.assertLessEqual(len(content), DISCORD_MESSAGE_LIMIT)
        # The heading and the expiring Loot Drop survive; the stash tail is what
        # gives way, and the reader is told how much of it did.
        self.assertIn("**PARTY STASH**", content)
        self.assertIn("Fresh Loot Token", content)
        self.assertIn("not shown here", content)
        self.assertIn("Quartermaster export", content)

    def test_state_projection_backs_off_and_health_names_a_stuck_surface(self) -> None:
        """A permanently failing surface must not retry once a second forever.

        A deleted channel or a revoked permission fails identically on every
        attempt. At a fixed one-second retry that is one Discord call per second
        for the life of the process, and health reports the same DEGRADED it
        reports for a surface that is one second behind.
        """
        self.inventory.grant_interaction("stuck-grant", actor_id="dm", item_name="Sextant", quantity=1)

        class BrokenTransport(FakeDiscordTransport):
            def upsert_state(self, target_id, destination, payload, message_id):
                raise RuntimeError("Missing Permissions")

        clock = _StepClock()
        scheduler = StateProjectionScheduler(self.store, BrokenTransport(), now=clock)
        delays = []
        for _ in range(4):
            before = clock.moment
            self.assertFalse(scheduler.run_once())
            row = self.store.connection.execute(
                "SELECT next_attempt_at FROM projection_targets WHERE target_id = 'party-stash'"
            ).fetchone()
            scheduled = datetime.fromisoformat(row["next_attempt_at"].replace("Z", "+00:00"))
            delays.append((scheduled - before).total_seconds())
            clock.advance(600)
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0])

        for _ in range(4):
            self.assertFalse(scheduler.run_once())
            clock.advance(600)
        report = health_report(self.store)
        self.assertEqual(report["checks"]["state_projections"], "FAILED")
        self.assertEqual(report["counts"]["stuck_projections"], 1)
        self.assertIn("Missing Permissions", report["projection_errors"]["party-stash"])
        self.assertIn("party-stash", render_health(report))

        # One delivery that works clears it: nothing has to be requeued by hand.
        working = FakeDiscordTransport()
        clock.advance(600)
        self.assertTrue(StateProjectionScheduler(self.store, working, now=clock).run_once())
        recovered = health_report(self.store)
        self.assertEqual(recovered["checks"]["state_projections"], "OK")
        self.assertEqual(recovered["counts"]["stuck_projections"], 0)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT failure_count FROM projection_targets WHERE target_id = 'party-stash'"
            ).fetchone()[0],
            0,
        )

    def test_projection_rate_limits_do_not_spend_the_backoff_budget(self) -> None:
        """A rate limit is the transport working, so it waits exactly as asked."""
        self.inventory.grant_interaction("limited-grant", actor_id="dm", item_name="Astrolabe", quantity=1)

        class LimitedTransport(FakeDiscordTransport):
            def upsert_state(self, target_id, destination, payload, message_id):
                raise RateLimitedError(30)

        clock = _StepClock()
        scheduler = StateProjectionScheduler(self.store, LimitedTransport(), now=clock)
        for _ in range(3):
            before = clock.moment
            self.assertFalse(scheduler.run_once())
            row = self.store.connection.execute(
                "SELECT failure_count, next_attempt_at FROM projection_targets WHERE target_id = 'party-stash'"
            ).fetchone()
            scheduled = datetime.fromisoformat(row["next_attempt_at"].replace("Z", "+00:00"))
            self.assertEqual((scheduled - before).total_seconds(), 30.0)
            self.assertEqual(row["failure_count"], 0)
            clock.advance(600)
        self.assertEqual(health_report(self.store)["checks"]["state_projections"], "DEGRADED")

    def test_fast_path_defers_while_another_write_transaction_is_open(self) -> None:
        """A write in flight is the slow case, so it must not suppress deferral.

        The adapter used to skip the deferral whenever any write transaction was
        open, which is exactly when the work is most likely to outrun Discord's
        three-second window and leave a committed mutation with no reply.
        """
        from quartermaster.discord_common import _run_fast

        holding = threading.Event()
        release = threading.Event()

        def hold_the_write_lock() -> None:
            with self.store.transaction():
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold_the_write_lock, daemon=True)
        holder.start()
        self.assertTrue(holding.wait(5))

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
        settings = Settings(guild_id="123", database_path=self.db_path, soft_deadline_seconds=0.05)
        result = asyncio.run(
            _run_fast(
                interaction,
                settings,
                lambda: self.inventory.grant_interaction(
                    "defer-under-write", actor_id="dm", item_name="Rope", quantity=1
                ),
            )
        )
        holder.join(5)
        self.assertTrue(interaction.response.deferred)
        self.assertTrue(result.deferred)
        self.assertEqual(result.value.logical_response["status"], "GRANTED")

    def test_expire_due_drops_takes_no_write_lock_when_nothing_is_due(self) -> None:
        """The projection runner calls this every second; idling must stay a read."""
        self.loot.create_drop_interaction(
            "idle-drop", actor_id="dm", items=[("Gem", 1, None)], expiry_hours=72
        )
        with mock.patch.object(
            self.store, "transaction", side_effect=AssertionError("took the write lock while idle")
        ):
            self.assertEqual(self.loot.expire_due_drops(), 0)

    def _build_previous_version_database(self, path: Path) -> None:
        """Build a database from before the one-active-character rule existed."""
        with _schema_version(_BEFORE_ACTIVE_CHARACTER_RULE):
            with SQLiteStore(path).open() as store, store.transaction() as connection:
                for index, (name, user) in enumerate(
                    [("Aria", "user-1"), ("Borin", "user-1"), ("Cade", "user-2")]
                ):
                    connection.execute(
                        """INSERT INTO characters(id, name, discord_user_id, lifecycle, created_at, updated_at)
                           VALUES (?, ?, ?, 'ACTIVE', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
                        (f"legacy-character-{index}", name, user),
                    )

    def test_upgrading_a_database_with_two_active_characters_names_the_conflict(self) -> None:
        """The bot will not start until this is resolved, so say which rows are at fault."""
        legacy_path = Path(self.tempdir.name) / "conflicting.sqlite"
        self._build_previous_version_database(legacy_path)
        with self.assertRaises(MigrationError) as raised:
            SQLiteStore(legacy_path).open()
        message = str(raised.exception)
        self.assertIn("one active character per Discord user", message)
        self.assertIn("Discord user user-1 has Aria, Borin", message)
        self.assertNotIn("user-2", message)

        # Resolving the conflict on the previous build lets the upgrade through.
        with _schema_version(_BEFORE_ACTIVE_CHARACTER_RULE):
            with SQLiteStore(legacy_path).open() as store, store.transaction() as connection:
                connection.execute(
                    "UPDATE characters SET lifecycle = 'RETIRED' WHERE id = 'legacy-character-1'"
                )
        with SQLiteStore(legacy_path).open() as upgraded:
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0],
                SCHEMA_VERSION,
            )

    def test_migration_renormalizes_and_merges_legacy_stack_names(self) -> None:
        """Rows written before the current rule must converge, not split into duplicates.

        Migration 2 backfilled `lower(trim(item_name))`: ASCII-only, and blind to
        internal whitespace. Stacks written under that rule disagree with
        `normalize_name`, so the same item can occupy two stacks for one owner.
        """
        legacy_path = Path(self.tempdir.name) / "legacy.sqlite"
        with _schema_version(_BEFORE_ACTIVE_CHARACTER_RULE):
            with SQLiteStore(legacy_path).open() as legacy:
                with legacy.transaction() as connection:
                    for stack_id, item_name, legacy_normalized, quantity in [
                        ("legacy-1", "Potion  of  Healing", "potion  of  healing", 2),
                        ("legacy-2", "Potion of Healing", "potion of healing", 3),
                        ("legacy-3", "Élixir", "Élixir", 1),
                    ]:
                        connection.execute(
                            """INSERT INTO inventory_stacks(
                                id, item_name, normalized_name, variant_metadata, quantity,
                                owner_type, owner_id, version, last_acquired_at, updated_at
                            ) VALUES (?, ?, ?, '{}', ?, 'PARTY', 'party', 1, ?, ?)""",
                            (stack_id, item_name, legacy_normalized, quantity, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
                        )

        with SQLiteStore(legacy_path).open() as migrated:
            rows = {
                row["normalized_name"]: (row["item_name"], row["quantity"])
                for row in migrated.connection.execute(
                    "SELECT item_name, normalized_name, quantity FROM inventory_stacks ORDER BY normalized_name"
                )
            }
            self.assertEqual(
                rows,
                {"potion of healing": ("Potion of Healing", 5), "élixir": ("Élixir", 1)},
            )
            # The merged stack is now reachable through the ordinary grant path.
            InventoryService(
                migrated, ReceiptRepository(migrated), HandleRepository(migrated)
            ).grant_interaction(
                "post-migration", actor_id="dm", item_name="potion   of healing", quantity=1
            )
            self.assertEqual(
                migrated.connection.execute(
                    "SELECT quantity FROM inventory_stacks WHERE normalized_name = 'potion of healing'"
                ).fetchone()["quantity"],
                6,
            )


if __name__ == "__main__":
    unittest.main()
