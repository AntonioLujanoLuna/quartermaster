from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from quartermaster.avrae_handoff import (
    NATIVE_COMMANDS,
    AvraeHandoffError,
    AvraeHandoffService,
    native_command,
)
from quartermaster.db import SQLiteStore
from quartermaster.integration import SUPPORTED_PROVIDER_OPERATIONS
from quartermaster.sessions import SessionService


class AvraeHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.tempdir.name) / "quartermaster.sqlite").open()
        self.handoff = AvraeHandoffService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_handoff_requires_an_active_quartermaster_session(self) -> None:
        card = self.handoff.build("start", channel_id="123")
        self.assertEqual(card.status, "NO_ACTIVE_SESSION")
        self.assertIn("Start a session", card.render())

    def test_handoff_uses_native_avrae_commands_without_creating_provider_state(self) -> None:
        session = SessionService(self.store).start_session()
        card = self.handoff.build("attack", channel_id="456")
        self.assertEqual(card.status, "READY")
        self.assertEqual(card.session_number, session["session_number"])
        self.assertEqual(card.command, "!attack <attack name> -t <target name>")
        rendered = card.render()
        self.assertIn("<#456>", rendered)
        self.assertIn("Avrae remains authoritative", rendered)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM provider_operations").fetchone()[0],
            0,
        )

    def test_handoff_commands_cover_the_supported_native_flow(self) -> None:
        SessionService(self.store).start_session()
        expected = {
            "start": "!i begin",
            "join": "!i join",
            "next": "!i next",
            "cast": "!cast <spell name> -t <target name>",
            "check": "!check <skill>",
            "save": "!save <ability>",
            "end": "!i end",
            "status": None,
        }
        for operation_kind, command in expected.items():
            with self.subTest(operation_kind=operation_kind):
                self.assertEqual(self.handoff.build(operation_kind, channel_id="789").command, command)

    def test_an_unsupported_action_fails_as_a_handoff_error(self) -> None:
        # The command surface only offers fixed choices, so this is reachable
        # by the next person who adds one. It has to arrive as the error the
        # caller already handles rather than as a provider-boundary error or a
        # KeyError on the lookup table.
        SessionService(self.store).start_session()
        with self.assertRaises(AvraeHandoffError):
            self.handoff.build("polymorph", channel_id="789")
        with self.assertRaises(AvraeHandoffError):
            native_command("polymorph")

    def test_a_missing_channel_fails_as_a_handoff_error(self) -> None:
        SessionService(self.store).start_session()
        with self.assertRaises(AvraeHandoffError):
            self.handoff.build("start", channel_id="   ")

    def test_every_provider_operation_has_a_handoff_card(self) -> None:
        # The two modules name the same actions for different reasons. If one
        # gains an action the other has to gain it too, or the hosted fallback
        # silently stops covering something the provider boundary accepts.
        self.assertEqual(set(NATIVE_COMMANDS), set(SUPPORTED_PROVIDER_OPERATIONS))

    def test_native_command_reads_the_table_without_touching_the_database(self) -> None:
        self.assertEqual(native_command("end"), "!i end")
        self.assertIsNone(native_command("status"))
