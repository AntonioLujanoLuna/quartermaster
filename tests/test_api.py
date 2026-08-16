"""Behavioural coverage for the Activity API.

The panel tests drive the surface the way the table does. These drive it the
way a browser does, which means the interesting cases are not the reads — those
are the same service calls the panels make — but everything around them: a
caller who presents no token, a forged one, an expired one, a token that claims
DM authority it was not issued with, and a request that tries to name an actor
other than the one it proved.

The live feed is driven the same way: a socket is opened, the domain is changed
through the service layer the way the bot changes it, and the test waits for the
socket to say so. That is the stage's exit criterion — a grant issued from the
bot appears on an open Activity screen — expressed as far as a test can express
it, which is up to the point where a browser would refetch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quartermaster.api_app import create_app
from quartermaster.api_auth import (
    Actor,
    Identity,
    IdentityError,
    SessionTokens,
    TokenError,
    is_dm,
)
from quartermaster.api_live import (
    CLOSED,
    EVENTS,
    IDLE,
    RESET,
    Change,
    EventFeed,
    Subscription,
)
from quartermaster.characters import CharacterService
from quartermaster.combat import CombatService
from quartermaster.config import ConfigurationError, Settings
from quartermaster.currency import CurrencyService
from quartermaster.db import SQLiteStore
from quartermaster.discord_common import BotServices, Quartermaster
from quartermaster.handles import HandleRepository
from quartermaster.inventory import InventoryService
from quartermaster.loot import LootDropService
from quartermaster.receipts import ReceiptRepository
from quartermaster.sessions import SessionService

GUILD_ID = "4242"
DM_ROLE_ID = "99"
PLAYER_ID = "22"
DM_ID = "11"
CLIENT_SECRET = "a-client-secret"


class FakeIdentityProvider:
    """Discord's half of the handshake, without Discord.

    Stage 1's exit criterion is that the read surface is provable without a
    network, so the one component that must talk to Discord is the one
    component the tests replace.
    """

    def __init__(self, identities: dict[str, Identity] | None = None) -> None:
        self.identities = identities or {}
        self.codes_seen: list[str] = []

    async def exchange_code(self, code: str) -> Identity:
        self.codes_seen.append(code)
        if code not in self.identities:
            raise IdentityError("Discord rejected the authorization code")
        return self.identities[code]


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "quartermaster.sqlite"
        self.store = SQLiteStore(path).open()
        self.addCleanup(self.store.close)

        receipts = ReceiptRepository(self.store)
        handles = HandleRepository(self.store)
        inventory = InventoryService(self.store, receipts, handles)
        sessions = SessionService(self.store, receipts)
        characters = CharacterService(self.store, receipts)
        currency = CurrencyService(self.store, receipts, handles=handles)
        loot = LootDropService(self.store, receipts, handles)
        combat = CombatService(self.store, receipts)

        self.settings = Settings(
            guild_id=GUILD_ID,
            database_path=path,
            dm_role_ids=(DM_ROLE_ID,),
            discord_client_id="1234",
            discord_client_secret=CLIENT_SECRET,
        )
        services = BotServices(
            store=self.store,
            receipts=receipts,
            inventory=inventory,
            sessions=sessions,
            characters=characters,
            currency=currency,
            loot=loot,
            combat=combat,
        )
        self.context = Quartermaster(
            services=services,
            settings=self.settings,
            characters=characters,
            currency=currency,
            loot=loot,
            combat=combat,
            handoff=None,
        )
        self.identity = FakeIdentityProvider(
            {
                "player-code": Identity(
                    user_id=PLAYER_ID, guild_roles=("1",), access_token="discord-player-token"
                ),
                "dm-code": Identity(
                    user_id=DM_ID, guild_roles=("1", DM_ROLE_ID), access_token="discord-dm-token"
                ),
            }
        )
        self.tokens = SessionTokens(CLIENT_SECRET, ttl_seconds=3600)
        self.app = create_app(self.context, self.identity, tokens=self.tokens)
        self.client = TestClient(self.app)

    # Helpers ----------------------------------------------------------------

    def authenticate(self, code: str) -> dict:
        response = self.client.post("/api/token", json={"code": code, "instance_id": "instance-1"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def headers(self, code: str = "player-code") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.authenticate(code)['token']}"}

    def grant(self, item: str, quantity: int, *, interaction: str) -> None:
        self.context.inventory.grant_interaction(
            interaction, actor_id=DM_ID, item_name=item, quantity=quantity
        )


class TokenExchangeTests(ApiTestCase):
    def test_a_valid_code_yields_a_token_naming_the_discord_user(self) -> None:
        body = self.authenticate("player-code")
        self.assertEqual(body["actor_id"], PLAYER_ID)
        self.assertFalse(body["is_dm"])
        self.assertEqual(self.tokens.verify(body["token"]).id, PLAYER_ID)

    def test_dm_authority_comes_from_the_guild_roles_discord_reported(self) -> None:
        self.assertTrue(self.authenticate("dm-code")["is_dm"])
        self.assertFalse(self.authenticate("player-code")["is_dm"])

    def test_a_rejected_code_is_not_a_session(self) -> None:
        response = self.client.post("/api/token", json={"code": "forged", "instance_id": None})
        self.assertEqual(response.status_code, 401)

    def test_the_instance_the_party_launched_rides_on_the_token(self) -> None:
        token = self.authenticate("player-code")["token"]
        self.assertEqual(self.tokens.verify(token).instance_id, "instance-1")

    def test_discords_own_token_comes_back_for_the_sdk_and_authorizes_nothing_here(self) -> None:
        """The client needs it to read the roster; this API never accepts it."""
        body = self.authenticate("player-code")
        self.assertEqual(body["discord_access_token"], "discord-player-token")
        response = self.client.get(
            "/api/stash", headers={"Authorization": f"Bearer {body['discord_access_token']}"}
        )
        self.assertEqual(response.status_code, 401)


class AuthorizationTests(ApiTestCase):
    def test_a_read_without_a_token_is_refused(self) -> None:
        self.assertEqual(self.client.get("/api/stash").status_code, 401)

    def test_a_token_signed_with_another_secret_is_refused(self) -> None:
        forged = SessionTokens("not-the-secret").issue(Actor(id=PLAYER_ID, is_dm=True))
        response = self.client.get("/api/stash", headers={"Authorization": f"Bearer {forged}"})
        self.assertEqual(response.status_code, 401)

    def test_an_expired_token_is_refused(self) -> None:
        now = [1_000.0]
        tokens = SessionTokens(CLIENT_SECRET, ttl_seconds=60, now=lambda: now[0])
        token = tokens.issue(Actor(id=PLAYER_ID, is_dm=False))
        self.assertEqual(tokens.verify(token).id, PLAYER_ID)
        now[0] = 1_061.0
        with self.assertRaises(TokenError):
            tokens.verify(token)

    def test_a_tampered_payload_does_not_verify(self) -> None:
        token = self.authenticate("player-code")["token"]
        body, _, signature = token.partition(".")
        raw = json.loads(_unpad(body))
        raw["dm"] = True
        response = self.client.get(
            "/api/export", headers={"Authorization": f"Bearer {_pad(json.dumps(raw))}.{signature}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_a_player_cannot_read_a_dm_only_surface(self) -> None:
        self.assertEqual(self.client.get("/api/export", headers=self.headers()).status_code, 403)

    def test_a_dm_can(self) -> None:
        response = self.client.get("/api/export", headers=self.headers("dm-code"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("# Quartermaster Export", response.json()["export"])

    def test_no_configured_dm_role_grants_nobody_authority(self) -> None:
        self.assertFalse(is_dm(("1", DM_ROLE_ID), ()))
        self.assertTrue(is_dm(("1", DM_ROLE_ID), (DM_ROLE_ID,)))

    def test_the_actor_comes_from_the_token_not_the_query(self) -> None:
        """A player naming someone else reads their own holdings, not theirs."""
        self.grant("Rope", 1, interaction="grant-rope")
        headers = self.headers("player-code")
        mine = self.client.get("/api/me/items", headers=headers).json()
        theirs = self.client.get(f"/api/me/items?actor_id={DM_ID}", headers=headers).json()
        self.assertEqual(mine, theirs)


class ReadSurfaceTests(ApiTestCase):
    def test_the_stash_read_is_unbounded_where_the_panel_was_not(self) -> None:
        for index in range(40):
            self.grant(f"Item {index:02d}", 1, interaction=f"grant-{index}")
        body = self.client.get("/api/stash", headers=self.headers()).json()
        self.assertEqual(body["total"], 40)
        self.assertEqual(len(body["items"]), 40)

    def test_home_states_what_the_panel_states(self) -> None:
        self.grant("Rope", 3, interaction="grant-rope")
        body = self.client.get("/api/home", headers=self.headers()).json()
        self.assertEqual(body["stash_count"], 1)
        self.assertIsNone(body["active_session_number"])
        self.assertEqual(body["treasury"]["gp"], 0)

    def test_every_read_the_panels_perform_is_reachable(self) -> None:
        headers = self.headers("dm-code")
        for path in (
            "/api/me",
            "/api/home",
            "/api/stash",
            "/api/me/items",
            "/api/loot",
            "/api/loot/claimable",
            "/api/treasury",
            "/api/characters",
            "/api/combat",
            "/api/session/continuity",
            "/api/export",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 200)

    def test_a_nonsense_page_size_is_refused_rather_than_passed_down(self) -> None:
        response = self.client.get("/api/me/items?limit=0", headers=self.headers())
        self.assertEqual(response.status_code, 422)

    def test_a_vast_page_size_is_clamped_rather_than_refused(self) -> None:
        response = self.client.get("/api/me/items?limit=100000", headers=self.headers())
        self.assertEqual(response.status_code, 200)

    def test_health_needs_no_session_and_names_no_campaign_state(self) -> None:
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("stash_count", body)


class StaticSurfaceTests(ApiTestCase):
    """Serving the built page from the same origin as the API.

    One origin is what makes one URL mapping enough, so the case that matters
    is that mounting the page does not shadow the API underneath it.
    """

    def _app_serving(self, distribution: Path) -> TestClient:
        settings = replace(self.settings, activity_dist=distribution)
        context = replace(self.context, settings=settings)
        return TestClient(create_app(context, self.identity, tokens=self.tokens))

    def setUp(self) -> None:
        super().setUp()
        self.dist = Path(self.directory.name) / "dist"
        self.dist.mkdir()
        (self.dist / "index.html").write_text("<!doctype html><title>Quartermaster</title>", encoding="utf-8")

    def test_the_page_is_served_at_the_root(self) -> None:
        response = self._app_serving(self.dist).get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Quartermaster", response.text)

    def test_mounting_the_page_does_not_shadow_the_api(self) -> None:
        client = self._app_serving(self.dist)
        self.assertEqual(client.get("/api/health").json()["status"], "ok")
        self.assertEqual(client.get("/api/stash").status_code, 401)

    def test_a_missing_build_is_a_startup_error_not_a_blank_frame(self) -> None:
        with self.assertRaises(ConfigurationError):
            self._app_serving(self.dist / "nowhere")

    def test_without_a_build_configured_the_api_still_serves(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 404)


def _next_message(socket, *, timeout: float = 5.0) -> dict:
    """Read one frame, or fail rather than hanging the suite.

    The test client's `receive_json` has no deadline, so a feed that says
    nothing would stop the run rather than fail it. The reader is a daemon
    thread for the same reason: on a failure it is abandoned, and abandoning a
    non-daemon thread would hold the interpreter open at exit.
    """
    box: queue.Queue = queue.Queue(maxsize=1)

    def pull() -> None:
        try:
            box.put(("message", socket.receive_json()))
        except BaseException as error:  # noqa: BLE001 - reported on the calling thread
            box.put(("error", error))

    threading.Thread(target=pull, daemon=True).start()
    try:
        kind, value = box.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(f"the live feed said nothing within {timeout}s") from None
    if kind == "error":
        raise value
    return value


class LiveFeedTests(ApiTestCase):
    """The socket that makes six copies of a screen one table.

    Every test here opens a real socket against the app and changes the domain
    through the same service calls the bot makes, because the thing worth
    proving is that a change nobody told the API about still reaches a client.
    """

    def setUp(self) -> None:
        super().setUp()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # Entered as a context manager, which is what runs the lifespan and so
        # what starts the pump. The suite's other client deliberately does not.
        self.served = self.stack.enter_context(TestClient(self.app))

    def socket(self, *, token: str | None = None, code: str = "player-code", since: int | None = None):
        path = "/api/live" if since is None else f"/api/live?since={since}"
        socket = self.stack.enter_context(self.served.websocket_connect(path))
        socket.send_json({"token": self.authenticate(code)["token"] if token is None else token})
        return socket

    def opened(self, **kwargs) -> tuple:
        socket = self.socket(**kwargs)
        return socket, _next_message(socket)

    # Opening ----------------------------------------------------------------

    def test_the_socket_states_who_you_proved_to_be_and_where_the_ledger_is(self) -> None:
        _, hello = self.opened()
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["actor_id"], PLAYER_ID)
        self.assertFalse(hello["is_dm"])
        self.assertEqual(hello["sequence"], 0)

    def test_the_feed_is_the_tables_rather_than_the_dms(self) -> None:
        _, hello = self.opened(code="dm-code")
        self.assertEqual(hello["actor_id"], DM_ID)
        self.assertTrue(hello["is_dm"])

    def test_a_forged_token_cannot_open_the_feed(self) -> None:
        forged = SessionTokens("not-the-secret").issue(Actor(id=PLAYER_ID, is_dm=True))
        socket = self.socket(token=forged)
        with self.assertRaises(WebSocketDisconnect) as refusal:
            _next_message(socket)
        self.assertEqual(refusal.exception.code, 4401)

    def test_an_opening_frame_that_is_not_a_token_is_refused(self) -> None:
        socket = self.stack.enter_context(self.served.websocket_connect("/api/live"))
        socket.send_text("hello?")
        with self.assertRaises(WebSocketDisconnect) as refusal:
            _next_message(socket)
        self.assertEqual(refusal.exception.code, 4401)

    def test_without_a_running_pump_the_socket_refuses_rather_than_going_quiet(self) -> None:
        """A frozen screen that looks live is worse than one that says it is not."""
        # A second assembly of the same context, whose lifespan is never
        # entered, so its feed was never started.
        unserved = TestClient(create_app(self.context, self.identity, tokens=self.tokens))
        with unserved.websocket_connect("/api/live") as socket:
            socket.send_json({"token": self.authenticate("player-code")["token"]})
            with self.assertRaises(WebSocketDisconnect) as refusal:
                _next_message(socket)
        self.assertEqual(refusal.exception.code, 4503)

    # Delivery ---------------------------------------------------------------

    def test_a_grant_issued_from_the_bot_reaches_an_open_socket(self) -> None:
        """Stage 3's exit criterion, as far as a test can carry it."""
        socket, _ = self.opened()
        self.grant("Rope", 2, interaction="grant-rope")
        notice = _next_message(socket)
        self.assertEqual(notice["type"], EVENTS)
        self.assertEqual([event["event_type"] for event in notice["events"]], ["ITEM_GRANTED"])
        self.assertEqual(notice["sequence"], notice["events"][-1]["sequence"])

    def test_the_socket_carries_notifications_rather_than_state(self) -> None:
        """No payload on the wire: the client refetches the read it has."""
        socket, _ = self.opened()
        self.grant("Rope", 2, interaction="grant-rope")
        event = _next_message(socket)["events"][0]
        self.assertEqual(set(event), {"sequence", "event_type", "created_at"})

    def test_a_change_reaches_every_socket_at_the_table(self) -> None:
        first, _ = self.opened()
        second, _ = self.opened(code="dm-code")
        self.grant("Rope", 2, interaction="grant-rope")
        for socket in (first, second):
            self.assertEqual(_next_message(socket)["type"], EVENTS)

    def test_the_cursor_only_advances_over_what_was_actually_sent(self) -> None:
        socket, hello = self.opened()
        self.grant("Rope", 2, interaction="grant-rope")
        first = _next_message(socket)
        self.grant("Torch", 3, interaction="grant-torch")
        second = _next_message(socket)
        self.assertGreater(first["sequence"], hello["sequence"])
        self.assertGreater(second["sequence"], first["sequence"])

    # Resuming ---------------------------------------------------------------

    def test_resuming_from_a_cursor_replays_the_gap_rather_than_the_campaign(self) -> None:
        self.grant("Rope", 2, interaction="grant-rope")
        self.grant("Torch", 3, interaction="grant-torch")
        socket, hello = self.opened(since=1)
        replay = _next_message(socket)
        self.assertEqual(replay["type"], EVENTS)
        self.assertEqual([event["sequence"] for event in replay["events"]], [2])
        self.assertEqual(hello["sequence"], 2)

    def test_a_client_that_is_already_current_is_told_nothing_to_replay(self) -> None:
        self.grant("Rope", 2, interaction="grant-rope")
        socket, hello = self.opened(since=1)
        self.grant("Torch", 3, interaction="grant-torch")
        # The next thing it hears is the new change, not a replay of the old one.
        notice = _next_message(socket)
        self.assertEqual(notice["type"], EVENTS)
        self.assertEqual([event["sequence"] for event in notice["events"]], [2])
        self.assertEqual(hello["sequence"], 1)

    def test_a_gap_too_wide_to_replay_is_a_reset_rather_than_a_flood(self) -> None:
        self.grant("Rope", 2, interaction="grant-rope")
        self.grant("Torch", 3, interaction="grant-torch")
        with mock.patch("quartermaster.api_app.REPLAY_LIMIT", 1):
            socket, _ = self.opened(since=0)
            reset = _next_message(socket)
        self.assertEqual(reset["type"], RESET)
        self.assertEqual(reset["sequence"], 2)


class SubscriptionTests(unittest.TestCase):
    """A client that cannot keep up, resolved without holding the feed.

    Driven directly rather than through a socket: the case is a queue that
    fills, and arranging that through a real client would mean arranging a slow
    one.
    """

    def _change(self, sequence: int) -> Change:
        return Change(sequence=sequence, event_type="ITEM_GRANTED", created_at="2026-08-16T00:00:00Z")

    def test_a_backlog_that_overflows_becomes_a_reset(self) -> None:
        async def scenario() -> tuple[str, ...]:
            subscription = Subscription(depth=2)
            for sequence in range(1, 6):
                subscription.offer((self._change(sequence),))
            first = await subscription.next(timeout=0.01)
            second = await subscription.next(timeout=0.01)
            return first[0], second[0]

        self.assertEqual(asyncio.run(scenario()), (RESET, IDLE))

    def test_a_quiet_feed_reports_idle_rather_than_waiting_forever(self) -> None:
        async def scenario() -> str:
            kind, _ = await Subscription().next(timeout=0.01)
            return kind

        self.assertEqual(asyncio.run(scenario()), IDLE)

    def test_a_client_that_went_away_wakes_the_socket(self) -> None:
        async def scenario() -> str:
            subscription = Subscription()

            async def leave() -> None:
                await asyncio.sleep(0)
                subscription.disconnect()

            waiting = asyncio.ensure_future(subscription.next(timeout=5.0))
            await leave()
            kind, _ = await waiting
            return kind

        self.assertEqual(asyncio.run(scenario()), CLOSED)


class CommitAnnouncementTests(unittest.TestCase):
    """The store's half of the live feed.

    This is the one thing `db.py` learned for the Activity, and what it learned
    is deliberately small: that a write committed, and nothing about what was
    in it. The alternative was a timer reading the sequence all evening against
    the connection the gateway shares.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStore(Path(self.directory.name) / "quartermaster.sqlite").open()
        self.addCleanup(self.store.close)
        self.announcements: list[int] = []
        self.listener = lambda: self.announcements.append(1)

    def test_a_committed_write_is_announced(self) -> None:
        self.store.add_commit_listener(self.listener)
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO maintenance_runs(name) VALUES ('live-feed-test')")
        self.assertEqual(len(self.announcements), 1)

    def test_a_rolled_back_write_is_not(self) -> None:
        self.store.add_commit_listener(self.listener)
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as connection:
                connection.execute("INSERT INTO maintenance_runs(name) VALUES ('live-feed-test')")
                raise RuntimeError("changed my mind")
        self.assertEqual(self.announcements, [])

    def test_a_listener_that_raises_does_not_fail_the_write_it_is_told_about(self) -> None:
        def broken() -> None:
            raise RuntimeError("the socket went away")

        self.store.add_commit_listener(broken)
        self.store.add_commit_listener(self.listener)
        with self.assertLogs("quartermaster.db", level="ERROR"):
            with self.store.transaction() as connection:
                connection.execute("INSERT INTO maintenance_runs(name) VALUES ('live-feed-test')")
        with self.store.read() as connection:
            written = connection.execute("SELECT COUNT(*) FROM maintenance_runs").fetchone()[0]
        self.assertEqual(written, 1)
        self.assertEqual(len(self.announcements), 1)

    def test_a_removed_listener_is_not_told(self) -> None:
        self.store.add_commit_listener(self.listener)
        self.store.remove_commit_listener(self.listener)
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO maintenance_runs(name) VALUES ('live-feed-test')")
        self.assertEqual(self.announcements, [])

    def test_a_commit_is_what_wakes_the_feed_rather_than_a_timer(self) -> None:
        """The poll is an hour away, so the delivery can only be the hook."""

        async def scenario() -> tuple[int, int]:
            feed = EventFeed(self.store, idle_poll_seconds=3600.0)
            await feed.start()
            try:
                subscription = feed.subscribe()
                idle, _ = await subscription.next(timeout=0.05)
                with self.store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO domain_events(operation_id, event_type, payload, created_at)"
                        " VALUES ('op', 'ITEM_GRANTED', '{}', '2026-08-16T00:00:00Z')"
                    )
                kind, changes = await subscription.next(timeout=5.0)
                self.assertEqual((idle, kind), (IDLE, EVENTS))
                return changes[0].sequence, feed.sequence
            finally:
                await feed.stop()

        self.assertEqual(asyncio.run(scenario()), (1, 1))


class ActivityConfigurationTests(unittest.TestCase):
    def _environment(self, **overrides: str) -> dict[str, str]:
        base = {"QM_GUILD_ID": GUILD_ID, "QM_DATABASE_PATH": "quartermaster.sqlite"}
        base.update(overrides)
        return base

    def test_the_activity_is_off_until_both_credentials_are_present(self) -> None:
        settings = Settings.from_env(self._environment())
        self.assertFalse(settings.activity_enabled)
        with self.assertRaises(ConfigurationError):
            settings.require_activity()

    def test_a_half_configured_activity_does_not_count_as_enabled(self) -> None:
        settings = Settings.from_env(self._environment(QM_DISCORD_CLIENT_ID="1234"))
        self.assertFalse(settings.activity_enabled)

    def test_configuring_it_does_not_disturb_the_bot_or_the_cli(self) -> None:
        settings = Settings.from_env(
            self._environment(QM_DISCORD_CLIENT_ID="1234", QM_DISCORD_CLIENT_SECRET="shhh")
        )
        self.assertTrue(settings.activity_enabled)
        self.assertEqual(settings.require_activity(), ("1234", "shhh"))
        self.assertEqual(settings.api_bind, "127.0.0.1:8080")

    def test_an_origin_discord_cannot_frame_is_refused_at_startup(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(self._environment(QM_ACTIVITY_ORIGIN="http://localhost:5173"))

    def test_a_bind_without_a_port_is_refused_at_startup(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(self._environment(QM_API_BIND="127.0.0.1"))

    def test_a_usable_origin_and_bind_survive(self) -> None:
        settings = Settings.from_env(
            self._environment(
                QM_ACTIVITY_ORIGIN="https://quartermaster.example/", QM_API_BIND="0.0.0.0:9001"
            )
        )
        self.assertEqual(settings.activity_origin, "https://quartermaster.example")
        self.assertEqual(settings.api_bind, "0.0.0.0:9001")


def _unpad(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _pad(value: str) -> str:
    import base64

    return base64.urlsafe_b64encode(value.encode()).decode("ascii").rstrip("=")


if __name__ == "__main__":
    unittest.main()
