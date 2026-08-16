"""The live feed: `domain_events.sequence` as a change notification stream.

Stage 3 of `docs/activity-migration-plan.md`. Six players hold six copies of the
same screen, and the only thing that makes them one table is that a change on
any of them arrives on all of them. There is already a monotonic cursor for
that — `domain_events.sequence`, assigned inside the transaction that made the
change — so this is a second *reader* of that table, not a second writer to
anything.

Three properties are deliberate.

**It carries notifications, not state.** A change names its sequence, its event
type, and when it landed. It does not carry the payload. A client that is told
"something granted" refetches the read it has on screen, which is the read it
would have fetched anyway, and there is exactly one renderer of any given fact.
Putting payloads on the socket would make this a second projection of the
domain, with the same "two chances to disagree" the README argues against.

**It is woken, not polled.** `SQLiteStore.add_commit_listener` says when a write
transaction committed, so an idle table costs no reads at all — the pump is
parked on an `asyncio.Event` — and a busy one costs one indexed read per commit
rather than one per tick. A timer instead would contend with the gateway's own
writes for the single connection all evening, which is the cost the drop-expiry
read was changed to stop paying.

**A slow client is dropped to a reset, never allowed to hold the feed.** Each
subscriber has a bounded queue. A client that cannot keep up loses its backlog
and is told to read everything again, which is the same answer it would get
from reconnecting, and costs the pump nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .db import SQLiteStore

logger = logging.getLogger(__name__)

__all__ = [
    "CLOSED",
    "EVENTS",
    "IDLE",
    "RESET",
    "Change",
    "EventFeed",
    "Subscription",
    "latest_sequence",
    "read_changes",
]

# What a subscriber's next wait resolved to.
EVENTS = "events"
RESET = "reset"
IDLE = "idle"
CLOSED = "closed"

#: How many changes one pump read takes at a time.
BATCH_LIMIT = 200

#: How many a socket will replay to a client resuming from a cursor. Past this
#: the client is told to read everything again: replaying an evening one row at
#: a time to a client that will refetch anyway is slower than the refetch.
REPLAY_LIMIT = 500

#: How many batches a subscriber may fall behind before it is reset.
QUEUE_DEPTH = 32

#: The safety net for a wake that never arrived — a listener that raised, or a
#: write from a connection this process does not own. Only ticks while somebody
#: is listening.
IDLE_POLL_SECONDS = 30.0


@dataclass(frozen=True)
class Change:
    """One row of `domain_events`, as much of it as a client is told."""

    sequence: int
    event_type: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {"sequence": self.sequence, "event_type": self.event_type, "created_at": self.created_at}


def latest_sequence(store: SQLiteStore) -> int:
    """The head of the ledger. Zero for a campaign that has done nothing yet."""
    with store.read() as connection:
        row = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM domain_events").fetchone()
    return int(row[0])


def read_changes(store: SQLiteStore, after: int, limit: int) -> list[Change]:
    """Changes above a cursor, oldest first.

    `sequence` is the table's primary key, so this is an index scan from a
    position rather than a search.
    """
    with store.read() as connection:
        rows = connection.execute(
            "SELECT sequence, event_type, created_at FROM domain_events WHERE sequence > ? ORDER BY sequence LIMIT ?",
            (after, limit),
        ).fetchall()
    return [Change(int(row["sequence"]), str(row["event_type"]), str(row["created_at"])) for row in rows]


class Subscription:
    """One client's view of the feed.

    Everything here runs on the event loop. `offer` is called by the pump and
    never blocks; `next` is awaited by the socket and is the only consumer.
    """

    def __init__(self, *, depth: int = QUEUE_DEPTH) -> None:
        self._queue: asyncio.Queue[tuple[Change, ...] | None] = asyncio.Queue(maxsize=depth)
        self._missed = False
        self._closed = False

    def offer(self, changes: tuple[Change, ...]) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(changes)
        except asyncio.QueueFull:
            # The backlog is worthless to a client that is going to refetch, so
            # it goes rather than the pump waiting on the slowest socket.
            self._reset()

    def disconnect(self) -> None:
        """Wake the consumer because the client has gone."""
        self._closed = True
        self._wake()

    def _reset(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._missed = True
        self._wake()

    def _wake(self) -> None:
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # Something is already queued, so the consumer is already about to
            # run and will see the flag it was set for.
            pass

    async def next(self, *, timeout: float) -> tuple[str, tuple[Change, ...]]:
        """What the socket should send next, or why it should not wait longer."""
        if self._closed:
            return CLOSED, ()
        if self._missed:
            self._missed = False
            return RESET, ()
        try:
            batch = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return IDLE, ()
        if self._closed:
            return CLOSED, ()
        if batch is None:
            if self._missed:
                self._missed = False
                return RESET, ()
            return IDLE, ()
        return EVENTS, batch


class EventFeed:
    """The single reader of `domain_events` that every socket shares.

    One pump for the whole process rather than one per client: six players at
    one table would otherwise be six queries per change against the one
    connection the bot is also writing through.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        batch_limit: int = BATCH_LIMIT,
        idle_poll_seconds: float = IDLE_POLL_SECONDS,
    ) -> None:
        self._store = store
        self._batch_limit = batch_limit
        self._idle_poll_seconds = idle_poll_seconds
        self._subscribers: set[Subscription] = set()
        self._wake = asyncio.Event()
        self._cursor = 0
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def sequence(self) -> int:
        """How far the pump has published. Informational; a client's cursor
        comes from what it was actually sent."""
        return self._cursor

    async def start(self) -> None:
        if self.running:
            return
        self._loop = asyncio.get_running_loop()
        # Primed before the listener is registered, so a commit that lands
        # during startup is read by the first drain rather than missed between
        # the two.
        self._cursor = await asyncio.to_thread(latest_sequence, self._store)
        self._store.add_commit_listener(self._on_commit)
        self._task = asyncio.create_task(self._pump())
        logger.info("live feed open at sequence %s", self._cursor)

    async def stop(self) -> None:
        self._store.remove_commit_listener(self._on_commit)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for subscriber in tuple(self._subscribers):
            subscriber.disconnect()
        self._subscribers.clear()

    def subscribe(self) -> Subscription:
        subscription = Subscription()
        self._subscribers.add(subscription)
        return subscription

    def release(self, subscription: Subscription) -> None:
        subscription.disconnect()
        self._subscribers.discard(subscription)

    def _on_commit(self) -> None:
        """Called on whatever thread committed, including the event loop's own.

        `call_soon_threadsafe` is correct from either, and the only failure it
        has is a loop that has already closed — which is shutdown, where there
        is nothing left to notify.
        """
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            pass

    async def _pump(self) -> None:
        while True:
            # No subscribers means no deadline: an idle table with nobody in
            # the Activity waits on the commit listener and reads nothing. The
            # poll exists for a wake that went missing, which only matters to
            # somebody who is listening.
            timeout = self._idle_poll_seconds if self._subscribers else None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The same discipline as the projection runner: a bad iteration
                # costs a second, not the feed.
                logger.exception("the live feed could not read new events")
                await asyncio.sleep(1.0)

    async def _drain(self) -> None:
        while True:
            changes = await asyncio.to_thread(read_changes, self._store, self._cursor, self._batch_limit)
            if not changes:
                return
            self._cursor = changes[-1].sequence
            batch = tuple(changes)
            for subscriber in tuple(self._subscribers):
                subscriber.offer(batch)
            if len(changes) < self._batch_limit:
                return
