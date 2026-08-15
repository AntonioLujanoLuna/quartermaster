"""Coverage for the Quartermaster combat record.

The point of these tests is as much what the record refuses to hold as what it
holds. Quartermaster tracks that a fight is happening so the table has one place
to ask "what is going on" and one place to hand spoils into the Loot Drop
workflow; the moment it grows a column for HP or initiative it has become a
second authoritative copy of state Avrae owns.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from quartermaster.combat import CombatError, CombatService, elapsed_seconds, format_duration
from quartermaster.db import SQLiteStore
from quartermaster.handles import HandleRepository
from quartermaster.loot import LootDropService
from quartermaster.receipts import ReceiptRepository
from quartermaster.sessions import SessionService

CHANNEL = "555"
DM = "11"

_interaction_ids = itertools.count(500_000)


def _interaction() -> str:
    return str(next(_interaction_ids))


class CombatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tempdir.name) / "quartermaster.sqlite").open()
        self.receipts = ReceiptRepository(self.store)
        self.handles = HandleRepository(self.store)
        self.loot = LootDropService(self.store, self.receipts, self.handles)
        self.combat = CombatService(self.store, self.receipts)
        self.sessions = SessionService(self.store, self.receipts, self.loot, self.combat)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    # Helpers -------------------------------------------------------------

    def open_combat(self, *, channel_id: str = CHANNEL, actor_id: str = DM) -> dict:
        return self.combat.open_interaction(
            _interaction(), actor_id=actor_id, channel_id=channel_id
        ).logical_response

    def close_combat(self, *, outcome: str | None = None, actor_id: str = DM) -> dict:
        return self.combat.close_interaction(
            _interaction(), actor_id=actor_id, outcome=outcome
        ).logical_response

    def encounter_rows(self) -> list[sqlite3.Row]:
        return self.store.connection.execute(
            "SELECT * FROM combat_encounters ORDER BY opened_at"
        ).fetchall()

    def event_types(self) -> list[str]:
        return [
            row["event_type"]
            for row in self.store.connection.execute(
                "SELECT event_type FROM domain_events ORDER BY sequence"
            )
        ]

    # The boundary --------------------------------------------------------

    def test_the_encounter_record_holds_no_avrae_mechanics(self) -> None:
        columns = {
            row["name"]
            for row in self.store.connection.execute("PRAGMA table_info(combat_encounters)").fetchall()
        }
        self.assertEqual(
            columns,
            {
                "id",
                "session_id",
                "channel_id",
                "status",
                "opened_by",
                "opened_at",
                "closed_by",
                "closed_at",
                "closed_reason",
                "outcome",
            },
            "combat_encounters must not grow a column for state Avrae owns",
        )

    # Opening -------------------------------------------------------------

    def test_opening_combat_without_a_session_records_nothing(self) -> None:
        response = self.open_combat()
        self.assertEqual(response["status"], "NO_ACTIVE_SESSION")
        self.assertEqual(self.encounter_rows(), [])
        self.assertNotIn("COMBAT_OPENED", self.event_types())

    def test_opening_combat_records_the_encounter_and_emits_the_event(self) -> None:
        session = self.sessions.start_session()
        response = self.open_combat()
        self.assertEqual(response["status"], "OPENED")
        self.assertEqual(response["session_number"], session["session_number"])
        self.assertEqual(response["channel_id"], CHANNEL)
        rows = self.encounter_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "OPEN")
        self.assertEqual(rows[0]["session_id"], session["session_id"])
        self.assertEqual(rows[0]["opened_by"], DM)
        self.assertIn("COMBAT_OPENED", self.event_types())
        dirty = self.store.connection.execute(
            "SELECT dirty_since FROM projection_targets WHERE target_id = 'session-surface'"
        ).fetchone()
        self.assertIsNotNone(dirty["dirty_since"])

    def test_opening_a_second_combat_returns_the_open_one_and_adds_no_row(self) -> None:
        self.sessions.start_session()
        first = self.open_combat()
        second = self.open_combat(channel_id="999")
        self.assertEqual(second["status"], "ALREADY_OPEN")
        self.assertEqual(second["encounter_id"], first["encounter_id"])
        self.assertEqual(second["channel_id"], CHANNEL, "the open combat keeps its own channel")
        self.assertEqual(len(self.encounter_rows()), 1)

    def test_the_schema_refuses_two_open_combats_in_one_session(self) -> None:
        session = self.sessions.start_session()
        self.open_combat()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                """INSERT INTO combat_encounters(id, session_id, channel_id, status, opened_at)
                   VALUES ('forced', ?, '777', 'OPEN', '2026-01-01T00:00:00.000Z')""",
                (session["session_id"],),
            )

    def test_opening_combat_requires_a_channel(self) -> None:
        self.sessions.start_session()
        with self.assertRaises(CombatError):
            self.combat.open_interaction(_interaction(), actor_id=DM, channel_id="   ")

    def test_a_replayed_interaction_does_not_open_a_second_combat(self) -> None:
        self.sessions.start_session()
        interaction_id = _interaction()
        first = self.combat.open_interaction(interaction_id, actor_id=DM, channel_id=CHANNEL)
        replay = self.combat.open_interaction(interaction_id, actor_id=DM, channel_id=CHANNEL)
        self.assertEqual(replay.logical_response, first.logical_response)
        self.assertEqual(len(self.encounter_rows()), 1)

    # Closing -------------------------------------------------------------

    def test_closing_combat_records_the_outcome_and_the_duration(self) -> None:
        self.sessions.start_session()
        self.open_combat()
        response = self.close_combat(outcome="  the ogre fled  ")
        self.assertEqual(response["status"], "CLOSED")
        self.assertEqual(response["outcome"], "the ogre fled", "the outcome note is trimmed")
        self.assertEqual(response["closed_reason"], "MANUAL")
        self.assertIsNotNone(response["elapsed_seconds"])
        self.assertGreaterEqual(response["elapsed_seconds"], 0.0)
        row = self.encounter_rows()[0]
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["closed_by"], DM)
        self.assertIn("COMBAT_CLOSED", self.event_types())

    def test_closing_without_an_open_combat_says_so(self) -> None:
        self.sessions.start_session()
        response = self.close_combat()
        self.assertEqual(response["status"], "NO_OPEN_COMBAT")
        self.assertEqual(self.encounter_rows(), [])

    def test_closing_without_a_session_says_so(self) -> None:
        self.assertEqual(self.close_combat()["status"], "NO_ACTIVE_SESSION")

    def test_combat_can_be_opened_again_after_it_closes(self) -> None:
        self.sessions.start_session()
        self.open_combat()
        self.close_combat()
        reopened = self.open_combat(channel_id="999")
        self.assertEqual(reopened["status"], "OPENED")
        self.assertEqual(reopened["channel_id"], "999")
        self.assertEqual(len(self.encounter_rows()), 2)

    def test_closing_combat_leaves_open_loot_for_the_party_to_claim(self) -> None:
        # Combat ending is not loot ending: a drop stays claimable until it
        # expires or its session closes. The closeout only reports it.
        self.sessions.start_session()
        self.loot.create_drop_interaction(_interaction(), actor_id=DM, items=[("Silvered dagger", 2, None)])
        self.open_combat()
        response = self.close_combat()
        self.assertEqual(len(response["open_drops"]), 1)
        self.assertEqual(response["open_drops"][0]["remaining_quantity"], 2)
        self.assertEqual(response["open_drops"][0]["item_count"], 1)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM loot_drops"
            ).fetchone()["status"],
            "OPEN",
        )

    # Session lifecycle ---------------------------------------------------

    def test_ending_the_session_closes_an_open_combat(self) -> None:
        session = self.sessions.start_session()
        self.open_combat()
        result = self.sessions.end_session(session["session_id"], where_ended="The inn")
        self.assertEqual(result["closed_combats"], 1)
        row = self.encounter_rows()[0]
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["closed_reason"], "SESSION_CLOSED")
        self.assertIsNone(row["outcome"])
        payload = json.loads(
            self.store.connection.execute(
                "SELECT payload FROM domain_events WHERE event_type = 'COMBAT_CLOSED'"
            ).fetchone()["payload"]
        )
        self.assertEqual(payload["reason"], "SESSION_CLOSED")

    def test_ending_a_session_without_combat_reports_none_closed(self) -> None:
        session = self.sessions.start_session()
        self.assertEqual(
            self.sessions.end_session(session["session_id"], where_ended="The inn")["closed_combats"], 0
        )

    # Status --------------------------------------------------------------

    def test_status_without_a_session_reports_no_session(self) -> None:
        status = self.combat.status()
        self.assertEqual(status["status"], "NO_ACTIVE_SESSION")
        self.assertIsNone(status["encounter"])

    def test_status_reports_the_open_combat_and_outstanding_loot(self) -> None:
        self.sessions.start_session()
        self.loot.create_drop_interaction(_interaction(), actor_id=DM, items=[("Crown", 1, None)])
        self.open_combat()
        status = self.combat.status()
        self.assertEqual(status["status"], "OPEN")
        self.assertEqual(status["encounter"]["channel_id"], CHANNEL)
        self.assertEqual(status["encounter"]["opened_by"], DM)
        self.assertIsNotNone(status["encounter"]["elapsed_seconds"])
        self.assertEqual(len(status["open_drops"]), 1)

    def test_status_remembers_the_previous_combat_once_it_closes(self) -> None:
        self.sessions.start_session()
        self.open_combat()
        self.close_combat(outcome="the ogre fled")
        status = self.combat.status()
        self.assertEqual(status["status"], "NO_OPEN_COMBAT")
        self.assertIsNone(status["encounter"])
        self.assertEqual(status["last_closed"]["outcome"], "the ogre fled")
        self.assertIsNotNone(status["last_closed"]["closed_seconds_ago"])

    def test_status_does_not_leak_a_previous_session_combat(self) -> None:
        session = self.sessions.start_session()
        self.open_combat()
        self.sessions.end_session(session["session_id"], where_ended="The inn")
        self.sessions.start_session()
        status = self.combat.status()
        self.assertIsNone(status["encounter"])
        self.assertIsNone(status["last_closed"])


class DurationTests(unittest.TestCase):
    def test_elapsed_seconds_reads_stored_timestamps(self) -> None:
        self.assertEqual(
            elapsed_seconds("2026-08-14T12:00:00.000Z", "2026-08-14T12:42:00.000Z"), 2520.0
        )

    def test_elapsed_seconds_never_reports_negative_time(self) -> None:
        self.assertEqual(
            elapsed_seconds("2026-08-14T12:42:00.000Z", "2026-08-14T12:00:00.000Z"), 0.0
        )

    def test_an_unreadable_timestamp_degrades_instead_of_raising(self) -> None:
        # A card that shows a duration must not be taken down by one that
        # cannot be parsed.
        self.assertIsNone(elapsed_seconds("not a timestamp", "2026-08-14T12:00:00.000Z"))
        self.assertIsNone(elapsed_seconds(None, "2026-08-14T12:00:00.000Z"))
        self.assertIsNone(elapsed_seconds("2026-08-14T12:00:00.000Z", None))

    def test_durations_read_the_way_a_person_says_them(self) -> None:
        self.assertIsNone(format_duration(None))
        self.assertEqual(format_duration(5), "5s")
        self.assertEqual(format_duration(59), "59s")
        self.assertEqual(format_duration(60), "1m")
        self.assertEqual(format_duration(2520), "42m")
        self.assertEqual(format_duration(3600), "1h")
        self.assertEqual(format_duration(4500), "1h 15m")


if __name__ == "__main__":
    unittest.main()
