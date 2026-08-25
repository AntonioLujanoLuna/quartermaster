"""An outbound relay: the campaign's history, posted where the table wants it.

The one integration Quartermaster can offer without learning about anything.
The table's tools are its own — an Obsidian vault, a wiki, a spreadsheet of who
owes whom what — and every one of them would otherwise be a provider adapter
here, with its own credential, its own failure mode, and its own reason to be
in a runtime that owns a campaign's only copy of its inventory.

Three things follow from the architecture rather than from taste.

**It is a second reader of `domain_events`, not a second outbox.** That table
already is one, with a monotonic cursor assigned inside the transaction that
made the change, and `api_live` already reads it this way. A second queue would
be a second copy of the same history — and, for a table that has configured no
webhook, rows nothing would ever consume.

**It is at-least-once, and the cursor moves only after a delivery is accepted.**
A receiver that answers anything but success is retried from where the relay
stopped, so a wiki that was down for an evening gets the evening when it comes
back rather than a hole. The consequence is the one every at-least-once
delivery has: a receiver may see the same sequence twice, which is why every
delivery names its sequence.

**It reads what the session log reads.** The lines come from
`narrative.render_entry` — the table the export and the session log both go
through — so a relayed evening cannot describe itself differently from the
evening the table watched.

No call here happens inside a transaction. The cursor read, the change read,
and the cursor write are each their own, and the POST is between them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .narrative import render_entry

logger = logging.getLogger(__name__)

#: How many changes one delivery carries. A session close writes several events
#: in one transaction and they belong in one request; an evening replayed after
#: an outage should not be one request either.
BATCH_LIMIT = 50

#: The safety net for a wake that never arrived — a listener that raised, or a
#: write from a connection this process does not own.
IDLE_POLL_SECONDS = 30.0

#: Backoff bounds for a receiver that is not answering. The ceiling is low
#: enough that a wiki coming back up is noticed within a turn of play.
RETRY_MIN_SECONDS = 2.0
RETRY_MAX_SECONDS = 120.0


def read_cursor(store: SQLiteStore) -> int:
    """How far the relay has been accepted to.

    Zero for a campaign that has never delivered, which means a webhook
    configured mid-campaign replays what the ledger already holds. That is the
    right default for a wiki being populated for the first time, and the reason
    the first delivery of an old campaign can be large.
    """
    with store.read() as connection:
        row = connection.execute(
            "SELECT delivered_sequence FROM webhook_cursor WHERE id = 1"
        ).fetchone()
    return int(row["delivered_sequence"]) if row is not None else 0


def write_cursor(store: SQLiteStore, sequence: int) -> None:
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO webhook_cursor(id, delivered_sequence, updated_at) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 delivered_sequence = excluded.delivered_sequence,
                 updated_at = excluded.updated_at""",
            (sequence, iso_now()),
        )


def read_batch(store: SQLiteStore, after: int, limit: int = BATCH_LIMIT) -> list[dict[str, Any]]:
    """Changes above the cursor, rendered as the session log renders them.

    The payload goes too. A relay whose whole job is to hand the campaign to
    something else would be useless carrying only prose — this is the one place
    a payload crossing a boundary is the point rather than a second projection.
    """
    with store.read() as connection:
        rows = connection.execute(
            """SELECT sequence, event_type, payload, created_at
                 FROM domain_events WHERE sequence > ? ORDER BY sequence LIMIT ?""",
            (after, limit),
        ).fetchall()
    return [
        {
            "sequence": int(row["sequence"]),
            "event_type": str(row["event_type"]),
            "occurred_at": str(row["created_at"]),
            "line": render_entry(row["event_type"], row["payload"]),
            "payload": json.loads(row["payload"]),
        }
        for row in rows
    ]


def sign(body: bytes, secret: str) -> str:
    """What the receiver checks before believing this came from the table.

    A webhook URL is a secret only until it is in somebody's browser history,
    a proxy log, or a screenshot of a config file. The signature is what makes
    the receiver's check about the body rather than about the address.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class WebhookRelay:
    """Deliver the ledger onwards, for as long as somebody is running.

    `deliver` is injected so the relay can be tested without a socket. The real
    one is `aiohttp_delivery`, which is in this module and is the only thing in
    it that touches a network.
    """

    def __init__(
        self,
        store: SQLiteStore,
        deliver: Callable[[bytes, dict[str, str]], Any],
        *,
        secret: str | None = None,
        batch_limit: int = BATCH_LIMIT,
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
    ) -> None:
        self.store = store
        self.deliver = deliver
        self.secret = secret
        self.batch_limit = batch_limit
        self.idle_poll_seconds = idle_poll_seconds
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._backoff = RETRY_MIN_SECONDS

    def wake(self) -> None:
        """Told that a write committed, from whichever thread committed it.

        The store's listener contract is that this must not block and must not
        write, and that a listener which raises may not fail a commit that has
        already happened.
        """
        loop = self._loop
        if loop is None or self._closed:
            return
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            # The loop is closing. There is nothing to deliver to.
            pass

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    def body(self, changes: list[dict[str, Any]]) -> bytes:
        """One request, named by what it carries.

        `sort_keys` because the signature is over these bytes and a receiver
        that re-serializes to check it has to be able to arrive at the same
        ones.
        """
        document = {
            "source": "quartermaster",
            "delivered_at": iso_now(),
            "first_sequence": changes[0]["sequence"],
            "last_sequence": changes[-1]["sequence"],
            "changes": changes,
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")

    async def deliver_once(self) -> bool:
        """One batch. True if anything was delivered and accepted.

        A failure leaves the cursor where it was, which is what makes this
        at-least-once rather than at-most-once: the alternative is a receiver
        that was briefly down having a hole in its copy of the evening.
        """
        after = await asyncio.to_thread(read_cursor, self.store)
        changes = await asyncio.to_thread(read_batch, self.store, after, self.batch_limit)
        if not changes:
            return False
        body = self.body(changes)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Quartermaster",
            "X-Quartermaster-Sequence": str(changes[-1]["sequence"]),
        }
        if self.secret:
            headers["X-Quartermaster-Signature"] = f"sha256={sign(body, self.secret)}"
        result = self.deliver(body, headers)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result
        await asyncio.to_thread(write_cursor, self.store, changes[-1]["sequence"])
        return True

    async def run(self) -> None:
        """Deliver until closed, waking on a commit and checking on a timer."""
        self._loop = asyncio.get_running_loop()
        while not self._closed:
            try:
                delivered = await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Never fatal. The relay is the least important thing running:
                # a receiver that is down must not take the evening's bot with
                # it, and the cursor has not moved, so nothing is lost.
                logger.warning(
                    "Quartermaster could not deliver to the webhook, retrying in %.0fs: %s",
                    self._backoff,
                    error,
                )
                await self._sleep(self._backoff)
                self._backoff = min(RETRY_MAX_SECONDS, self._backoff * 2)
                continue
            self._backoff = RETRY_MIN_SECONDS
            if delivered:
                # More may be waiting; go round without sleeping.
                continue
            await self._sleep(self.idle_poll_seconds)

    async def _sleep(self, seconds: float) -> None:
        """Wait, unless a commit says not to."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            pass


def aiohttp_delivery(url: str, timeout_seconds: float) -> Callable[[bytes, dict[str, str]], Any]:
    """The real transport, and the only network call in this module.

    A non-2xx answer raises, which is what leaves the cursor where it was.
    """

    async def deliver(body: bytes, headers: dict[str, str]) -> None:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body, headers=headers) as response:
                if response.status >= 300:
                    raise RuntimeError(f"the receiver answered {response.status}")

    return deliver
