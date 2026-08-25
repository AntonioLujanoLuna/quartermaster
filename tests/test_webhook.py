"""The outbound relay, without a socket.

What is worth proving here is not that a POST is made. It is what happens
around one: that a receiver which refuses does not cost the table an evening of
its history, that the cursor is what makes redelivery possible, and that a
signature is over the bytes that were sent rather than over something rebuilt.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from quartermaster.config import ConfigurationError, Settings
from quartermaster.db import SQLiteStore
from quartermaster.events import append_event
from quartermaster.webhook import WebhookRelay, read_batch, read_cursor, sign, write_cursor


class Receiver:
    """A webhook receiver that answers however the test needs it to."""

    def __init__(self, *, refuse_first: int = 0) -> None:
        self.deliveries: list[tuple[bytes, dict[str, str]]] = []
        self.refuse_first = refuse_first

    async def __call__(self, body: bytes, headers: dict[str, str]) -> None:
        if self.refuse_first > 0:
            self.refuse_first -= 1
            raise RuntimeError("the receiver answered 503")
        self.deliveries.append((body, headers))

    def documents(self) -> list[dict]:
        return [json.loads(body) for body, _ in self.deliveries]


class WebhookRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = SQLiteStore(Path(self.tempdir.name) / "campaign.sqlite").open()
        self.addCleanup(self.store.close)

    def write(self, event_type: str, payload: dict) -> int:
        with self.store.transaction() as connection:
            return append_event(
                connection,
                operation_id="operation",
                actor_id="actor",
                event_type=event_type,
                payload=payload,
                destination="session:one",
            )

    def relay(self, receiver: Receiver, **kwargs: object) -> WebhookRelay:
        return WebhookRelay(self.store, receiver, **kwargs)  # type: ignore[arg-type]

    # -- The cursor -----------------------------------------------------------

    def test_a_campaign_that_has_delivered_nothing_starts_at_the_beginning(self) -> None:
        """Zero rather than the head of the ledger.

        A webhook configured mid-campaign replays what is already recorded,
        which is what a wiki being populated for the first time wants.
        """
        self.assertEqual(read_cursor(self.store), 0)
        self.write("SESSION_STARTED", {"session_id": "s", "session_number": 1})
        self.assertEqual(read_cursor(self.store), 0)

    def test_the_cursor_moves_only_once_a_receiver_has_accepted(self) -> None:
        self.write("SESSION_STARTED", {"session_id": "s", "session_number": 1})
        receiver = Receiver()
        self.assertTrue(asyncio.run(self.relay(receiver).deliver_once()))
        self.assertEqual(read_cursor(self.store), 1)

    def test_a_refused_delivery_leaves_the_cursor_where_it_was(self) -> None:
        """The property the whole design is for.

        A receiver that was down for an evening gets the evening when it comes
        back, rather than a hole where the evening was.
        """
        self.write("SESSION_STARTED", {"session_id": "s", "session_number": 1})
        receiver = Receiver(refuse_first=1)
        with self.assertRaises(RuntimeError):
            asyncio.run(self.relay(receiver).deliver_once())
        self.assertEqual(read_cursor(self.store), 0)

        # And the same change is delivered on the next attempt.
        self.assertTrue(asyncio.run(self.relay(receiver).deliver_once()))
        self.assertEqual(receiver.documents()[0]["changes"][0]["sequence"], 1)

    def test_nothing_to_deliver_is_not_a_delivery(self) -> None:
        receiver = Receiver()
        self.assertFalse(asyncio.run(self.relay(receiver).deliver_once()))
        self.assertEqual(receiver.deliveries, [])

    def test_delivery_resumes_from_the_cursor_rather_than_from_the_start(self) -> None:
        for number in range(1, 4):
            self.write("SESSION_STARTED", {"session_id": "s", "session_number": number})
        write_cursor(self.store, 2)
        receiver = Receiver()
        asyncio.run(self.relay(receiver).deliver_once())
        sequences = [change["sequence"] for change in receiver.documents()[0]["changes"]]
        self.assertEqual(sequences, [3])

    # -- What a receiver is handed -------------------------------------------

    def test_a_delivery_carries_the_line_the_session_log_would_have_printed(self) -> None:
        """One renderer, so a relayed evening cannot describe itself twice."""
        self.write("SESSION_CLOSED", {"session_id": "s", "session_number": 4})
        receiver = Receiver()
        asyncio.run(self.relay(receiver).deliver_once())
        change = receiver.documents()[0]["changes"][0]
        self.assertEqual(change["line"], "Session 4 closed.")
        self.assertEqual(change["event_type"], "SESSION_CLOSED")
        # The payload goes as well: handing the campaign to something else is
        # the whole job, and prose alone would not be enough to do anything with.
        self.assertEqual(change["payload"]["session_number"], 4)

    def test_a_batch_names_the_span_it_covers(self) -> None:
        for number in range(1, 4):
            self.write("SESSION_STARTED", {"session_id": "s", "session_number": number})
        receiver = Receiver()
        asyncio.run(self.relay(receiver).deliver_once())
        document = receiver.documents()[0]
        self.assertEqual(document["first_sequence"], 1)
        self.assertEqual(document["last_sequence"], 3)
        self.assertEqual(document["source"], "quartermaster")

    def test_a_batch_is_bounded_and_the_rest_follows(self) -> None:
        for number in range(1, 6):
            self.write("SESSION_STARTED", {"session_id": "s", "session_number": number})
        receiver = Receiver()
        relay = self.relay(receiver, batch_limit=2)
        for _ in range(3):
            asyncio.run(relay.deliver_once())
        delivered = [
            change["sequence"] for document in receiver.documents() for change in document["changes"]
        ]
        self.assertEqual(delivered, [1, 2, 3, 4, 5])

    # -- The signature --------------------------------------------------------

    def test_a_signed_delivery_can_be_checked_against_the_bytes_that_were_sent(self) -> None:
        self.write("SESSION_STARTED", {"session_id": "s", "session_number": 1})
        receiver = Receiver()
        asyncio.run(self.relay(receiver, secret="a-shared-secret").deliver_once())
        body, headers = receiver.deliveries[0]
        expected = hmac.new(b"a-shared-secret", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Quartermaster-Signature"], f"sha256={expected}")
        self.assertEqual(headers["X-Quartermaster-Sequence"], "1")

    def test_an_unsigned_delivery_carries_no_signature_rather_than_an_empty_one(self) -> None:
        self.write("SESSION_STARTED", {"session_id": "s", "session_number": 1})
        receiver = Receiver()
        asyncio.run(self.relay(receiver).deliver_once())
        self.assertNotIn("X-Quartermaster-Signature", receiver.deliveries[0][1])

    def test_the_signature_is_over_bytes_a_receiver_can_reproduce(self) -> None:
        """Sorted keys, so re-serializing to check arrives at the same bytes."""
        self.write("SESSION_STARTED", {"session_id": "s", "session_number": 1})
        receiver = Receiver()
        relay = self.relay(receiver, secret="s")
        changes = read_batch(self.store, 0)
        body = relay.body(changes)
        self.assertEqual(
            body, json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
        )
        self.assertEqual(sign(body, "s"), hmac.new(b"s", body, hashlib.sha256).hexdigest())


class WebhookConfigurationTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {"QM_GUILD_ID": "1", "QM_DATABASE_PATH": "campaign.sqlite"}

    def settings(self, **overrides: str) -> Settings:
        return Settings.from_env({**self.BASE, **overrides})

    def test_a_table_with_no_webhook_configured_has_none(self) -> None:
        self.assertIsNone(self.settings().webhook_url)

    def test_a_webhook_must_be_somewhere_this_process_can_post_to(self) -> None:
        for bad in ("ftp://example.test/hook", "example.test/hook", "not a url"):
            with self.subTest(url=bad), self.assertRaises(ConfigurationError):
                self.settings(QM_WEBHOOK_URL=bad)

    def test_credentials_in_the_url_are_refused_because_a_failure_logs_it(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.settings(QM_WEBHOOK_URL="https://user:password@example.test/hook")

    def test_a_secret_without_somewhere_to_send_it_is_a_misconfiguration(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.settings(QM_WEBHOOK_SECRET="a-shared-secret")

    def test_a_configured_webhook_is_carried_with_its_secret_and_timeout(self) -> None:
        settings = self.settings(
            QM_WEBHOOK_URL="https://example.test/hook",
            QM_WEBHOOK_SECRET="a-shared-secret",
            QM_WEBHOOK_TIMEOUT_SECONDS="2",
        )
        self.assertEqual(settings.webhook_url, "https://example.test/hook")
        self.assertEqual(settings.webhook_secret, "a-shared-secret")
        self.assertEqual(settings.webhook_timeout_seconds, 2.0)


if __name__ == "__main__":
    unittest.main()
