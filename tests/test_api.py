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
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quartermaster.api_app import PROXY_PREFIX, create_app
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
from quartermaster.api_server import _require_websockets, serve_api
from quartermaster.characters import CharacterService
from quartermaster.combat import CombatService
from quartermaster.config import ConfigurationError, Settings
from quartermaster.currency import CurrencyService
from quartermaster.db import SCHEMA_VERSION, SQLiteStore
from quartermaster.discord_common import BotServices, Quartermaster
from quartermaster.handles import HandleRepository
from quartermaster.integration import ProviderResult, ProviderTimeout
from quartermaster.inventory import InventoryService
from quartermaster.loot import LootDropService
from quartermaster.preflight import _built_page, run_preflight
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

    def register(self, name: str, discord_user_id: str, *, interaction: str) -> str:
        result = self.context.characters.create_interaction(
            interaction, actor_id=DM_ID, name=name, discord_user_id=discord_user_id
        )
        return result.logical_response["character_id"]

    def post(
        self,
        path: str,
        body: dict | None = None,
        *,
        code: str = "player-code",
        key: str | None = None,
        headers: dict[str, str] | None = None,
        client=None,
    ):
        """One request the way the client makes it: a token, and a key per action."""
        request_headers = dict(headers if headers is not None else self.headers(code))
        request_headers["Idempotency-Key"] = key if key is not None else uuid.uuid4().hex
        return (client or self.client).post(path, json=body or {}, headers=request_headers)

    def stash_stack(self, item_name: str) -> dict:
        for item in self.client.get("/api/stash", headers=self.headers()).json()["items"]:
            if item["item_name"] == item_name:
                return item
        raise AssertionError(f"the Party Stash has no {item_name}")

    def take(self, item_name: str, amount: int | str = 1, *, code: str = "player-code") -> dict:
        """Prepare and spend one take, which is what one press costs."""
        prepared = self.post(
            "/api/stash/take/prepare",
            {"stack_id": self.stash_stack(item_name)["id"], "amount": amount},
            code=code,
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        return self.post("/api/stash/take", {"handle_id": prepared.json()["handle_id"]}, code=code).json()


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


class TakeTests(ApiTestCase):
    """Stage 4's first mutation, and the one that carries a read set.

    The panel minted take handles when it rendered a message, so a handle could
    be minutes older than the press it answered. Here it is minted when the
    player acts, and the interesting question is whether the check that made
    `Take all` honest still fires when it should.
    """

    def setUp(self) -> None:
        super().setUp()
        self.register("Vex", PLAYER_ID, interaction="register-vex")

    def test_a_take_moves_the_stack_onto_the_players_own_character(self) -> None:
        self.grant("Rope", 3, interaction="grant-rope")
        body = self.take("Rope")
        self.assertEqual(body["result"]["quantity"], 1)
        self.assertEqual(body["result"]["remaining"], 2)
        holdings = self.client.get("/api/me/items", headers=self.headers()).json()
        self.assertEqual(holdings["character"]["name"], "Vex")
        self.assertEqual([(item["item_name"], item["quantity"]) for item in holdings["items"]], [("Rope", 1)])

    def test_take_all_means_the_quantity_the_player_acted_against(self) -> None:
        self.grant("Rope", 3, interaction="grant-rope")
        self.assertEqual(self.take("Rope", "all")["result"]["quantity"], 3)
        self.assertEqual(self.client.get("/api/stash", headers=self.headers()).json()["total"], 0)

    def test_a_quantity_that_moved_under_the_press_is_asked_about_rather_than_substituted(self) -> None:
        """The race the plan names, now inside one round trip instead of one panel."""
        self.grant("Rope", 3, interaction="grant-rope")
        prepared = self.post(
            "/api/stash/take/prepare", {"stack_id": self.stash_stack("Rope")["id"], "amount": "all"}
        )
        handle = prepared.json()["handle_id"]
        self.grant("Rope", 2, interaction="grant-more-rope")

        refused = self.post("/api/stash/take", {"handle_id": handle})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "STALE")
        self.assertIn("3", refused.json()["detail"])

        confirmed = self.post("/api/stash/take/confirm", {"handle_id": handle})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["result"]["quantity"], 5)

    def test_a_refused_take_leaves_the_handle_to_answer_with(self) -> None:
        """The refusal rolls back, so the confirmation has something to spend."""
        self.grant("Rope", 3, interaction="grant-rope")
        prepared = self.post(
            "/api/stash/take/prepare", {"stack_id": self.stash_stack("Rope")["id"], "amount": "all"}
        )
        self.grant("Rope", 1, interaction="grant-more-rope")
        self.post("/api/stash/take", {"handle_id": prepared.json()["handle_id"]})
        self.assertEqual(self.client.get("/api/stash", headers=self.headers()).json()["items"][0]["quantity"], 4)

    def test_a_spent_handle_is_not_a_second_take(self) -> None:
        self.grant("Rope", 3, interaction="grant-rope")
        prepared = self.post(
            "/api/stash/take/prepare", {"stack_id": self.stash_stack("Rope")["id"], "amount": 1}
        )
        handle = prepared.json()["handle_id"]
        self.assertEqual(self.post("/api/stash/take", {"handle_id": handle}).status_code, 200)
        replayed = self.post("/api/stash/take", {"handle_id": handle})
        self.assertEqual(replayed.status_code, 409)
        self.assertEqual(replayed.json()["code"], "HANDLE")

    def test_one_players_handle_is_not_another_players_to_spend(self) -> None:
        """The handle is bound to the actor it was minted for, not to whoever holds it."""
        self.grant("Rope", 3, interaction="grant-rope")
        prepared = self.post(
            "/api/stash/take/prepare", {"stack_id": self.stash_stack("Rope")["id"], "amount": 1}
        )
        stolen = self.post(
            "/api/stash/take", {"handle_id": prepared.json()["handle_id"]}, code="dm-code"
        )
        self.assertEqual(stolen.status_code, 409)
        self.assertEqual(stolen.json()["code"], "HANDLE")

    def test_a_take_without_a_character_is_refused_in_words_a_player_can_act_on(self) -> None:
        self.grant("Rope", 1, interaction="grant-rope")
        roster = self.client.get("/api/characters", headers=self.headers()).json()["characters"]
        self.context.characters.transition_interaction(
            "retire-vex", actor_id=DM_ID, character_id=roster[0]["id"], lifecycle="RETIRED"
        )
        prepared = self.post(
            "/api/stash/take/prepare", {"stack_id": self.stash_stack("Rope")["id"], "amount": 1}
        )
        refused = self.post("/api/stash/take", {"handle_id": prepared.json()["handle_id"]})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()["code"], "REFUSED")
        self.assertIn("character", refused.json()["detail"])


class GiveAndUseTests(ApiTestCase):
    """Possession moving back out again, which is what makes a take repairable."""

    def setUp(self) -> None:
        super().setUp()
        self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.other = self.register("Brann", DM_ID, interaction="register-brann")
        self.grant("Rope", 3, interaction="grant-rope")
        self.take("Rope", "all")

    def held(self, code: str = "player-code") -> list[dict]:
        return self.client.get("/api/me/items", headers=self.headers(code)).json()["items"]

    def test_a_give_returns_what_a_take_moved(self) -> None:
        prepared = self.post("/api/items/give/prepare", {"stack_id": self.held()[0]["id"]})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        given = self.post(
            "/api/items/give", {"handle_id": prepared.json()["handles"]["all"], "destination": "party"}
        )
        self.assertEqual(given.json()["result"]["destination_name"], "the Party Stash")
        self.assertEqual(self.stash_stack("Rope")["quantity"], 3)
        self.assertEqual(self.held(), [])

    def test_a_give_can_hand_it_to_another_active_character(self) -> None:
        prepared = self.post("/api/items/give/prepare", {"stack_id": self.held()[0]["id"]})
        given = self.post(
            "/api/items/give",
            {"handle_id": prepared.json()["handles"]["one"], "destination": self.other},
        )
        self.assertEqual(given.json()["result"]["destination_name"], "Brann")
        self.assertEqual([(item["item_name"], item["quantity"]) for item in self.held("dm-code")], [("Rope", 1)])

    def test_a_quantity_the_player_typed_needs_no_handle(self) -> None:
        given = self.post(
            "/api/items/give/some", {"item_name": "Rope", "quantity": 2, "destination": "party"}
        )
        self.assertEqual(given.status_code, 200, given.text)
        self.assertEqual(given.json()["result"]["remaining"], 1)

    def test_a_give_that_moved_under_the_press_is_asked_about(self) -> None:
        prepared = self.post("/api/items/give/prepare", {"stack_id": self.held()[0]["id"]})
        handle = prepared.json()["handles"]["all"]
        self.grant("Rope", 1, interaction="grant-more-rope")
        self.take("Rope")  # another character hands the giver one more

        refused = self.post("/api/items/give", {"handle_id": handle, "destination": "party"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "STALE"))
        confirmed = self.post("/api/items/give/confirm", {"handle_id": handle, "destination": "party"})
        self.assertEqual(confirmed.json()["result"]["quantity"], 4)

    def test_using_something_spends_it_rather_than_moving_it(self) -> None:
        used = self.post(
            "/api/items/use", {"stack_id": self.held()[0]["id"], "quantity": 2, "reason": "Climbed a wall"}
        )
        self.assertEqual(used.json()["result"]["status"], "CONSUMED")
        self.assertEqual(self.held()[0]["quantity"], 1)
        self.assertEqual(self.client.get("/api/stash", headers=self.headers()).json()["total"], 0)

    def test_a_player_cannot_correct_the_party_stash_by_calling_use(self) -> None:
        """The one flag that separates using an item from emptying the stash.

        `party_authorized` is set by the route and reaching for it in the body
        changes nothing, which is the difference between a rule and a habit.
        """
        self.grant("Torch", 5, interaction="grant-torch")
        refused = self.post(
            "/api/items/use",
            {
                "stack_id": self.stash_stack("Torch")["id"],
                "quantity": 5,
                "party_authorized": True,
                "actor_id": DM_ID,
            },
        )
        self.assertEqual(refused.status_code, 422)
        self.assertIn("DM", refused.json()["detail"])
        self.assertEqual(self.stash_stack("Torch")["quantity"], 5)


class ClaimAndCoinTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("Vex", PLAYER_ID, interaction="register-vex")

    def drop_item(self) -> str:
        self.context.loot.create_drop_interaction(
            "open-drop", actor_id=DM_ID, items=[("Silver Dagger", 2, "Bandit camp")]
        )
        drops = self.client.get("/api/loot", headers=self.headers()).json()["drops"]
        return drops[0]["items"][0]["id"]

    def test_a_claim_moves_loot_onto_the_claimants_character(self) -> None:
        claimed = self.post("/api/loot/claim", {"drop_item_id": self.drop_item(), "amount": 2})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["result"]["status"], "CLAIMED")
        self.assertEqual(claimed.json()["result"]["quantity"], 2)
        holdings = self.client.get("/api/me/items", headers=self.headers()).json()
        self.assertEqual(holdings["items"][0]["item_name"], "Silver Dagger")

    def test_a_claim_for_more_than_remains_is_refused_before_anything_moves(self) -> None:
        refused = self.post("/api/loot/claim", {"drop_item_id": self.drop_item(), "amount": 3})
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "REFUSED"))
        self.assertEqual(self.client.get("/api/me/items", headers=self.headers()).json()["items"], [])

    def test_coin_travels_back_to_the_treasury(self) -> None:
        self.context.currency.adjust_treasury_interaction(
            "fund-treasury", actor_id=DM_ID, deltas={"gp": 100}
        )
        self.post(
            "/api/treasury/give",
            {
                "character_id": self.client.get("/api/characters", headers=self.headers()).json()["characters"][0]["id"],
                "amounts": {"gp": 40},
            },
            code="dm-code",
        )
        returned = self.post("/api/treasury/return", {"amounts": {"gp": 15}, "destination": "party"})
        self.assertEqual(returned.status_code, 200, returned.text)
        body = self.client.get("/api/treasury", headers=self.headers()).json()
        self.assertEqual(body["treasury"]["gp"], 75)
        self.assertEqual(body["purse"]["balance"]["gp"], 25)

    def test_coin_a_character_does_not_have_is_refused(self) -> None:
        refused = self.post("/api/treasury/return", {"amounts": {"gp": 5}, "destination": "party"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (422, "REFUSED"))


class IdempotencyTests(ApiTestCase):
    """The key that replaced Discord's interaction id, and what it now has to defend.

    Discord supplied an id nobody could choose. A browser chooses this one, so
    the two things that were previously free — that a key belongs to the actor
    who used it, and that it is a key at all — are checked here.
    """

    def setUp(self) -> None:
        super().setUp()
        self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.register("Brann", DM_ID, interaction="register-brann")
        self.grant("Rope", 6, interaction="grant-rope")

    def handle(self, code: str = "player-code") -> str:
        return self.post(
            "/api/stash/take/prepare", {"stack_id": self.stash_stack("Rope")["id"], "amount": 1}, code=code
        ).json()["handle_id"]

    def test_a_mutation_without_a_key_is_refused_rather_than_run(self) -> None:
        response = self.client.post(
            "/api/stash/take", json={"handle_id": self.handle()}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stash_stack("Rope")["quantity"], 6)

    def test_a_key_that_is_not_a_key_is_refused(self) -> None:
        for key in ("x" * 200, "not a key", "../../etc/passwd"):
            with self.subTest(key=key):
                response = self.post("/api/stash/take", {"handle_id": self.handle()}, key=key)
                self.assertEqual(response.status_code, 400)

    def test_a_replayed_key_returns_the_stored_receipt_rather_than_taking_twice(self) -> None:
        """Retry-safety for a flaky socket, which is what the receipt was always for."""
        handle = self.handle()
        first = self.post("/api/stash/take", {"handle_id": handle}, key="one-press")
        second = self.post("/api/stash/take", {"handle_id": handle}, key="one-press")
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.stash_stack("Rope")["quantity"], 5)

    def test_one_players_key_does_not_answer_for_anothers_action(self) -> None:
        """Two clients generating the same string is a collision, not a replay."""
        mine = self.post("/api/stash/take", {"handle_id": self.handle()}, key="same-key")
        theirs = self.post(
            "/api/stash/take", {"handle_id": self.handle("dm-code")}, key="same-key", code="dm-code"
        )
        self.assertEqual((mine.status_code, theirs.status_code), (200, 200))
        self.assertNotEqual(mine.json()["operation_id"], theirs.json()["operation_id"])
        self.assertEqual(self.stash_stack("Rope")["quantity"], 4)


class MutationAuthorityTests(ApiTestCase):
    """Who may do what, asked of the transport rather than of the panel."""

    DM_ONLY = (
        ("/api/treasury/give", {"character_id": "whoever", "amounts": {"gp": 1}}),
        ("/api/characters", {"name": "Vex", "discord_user_id": PLAYER_ID}),
        ("/api/characters/transition", {"character_id": "whoever", "lifecycle": "RETIRED"}),
        # Stage 5. Every one of these is behind `_require_dm` on the panel, so
        # every one of them is behind the DM check here: an API that grants
        # authority the surface it replaces does not grant is not a migration
        # of it.
        ("/api/stash/grant", {"item_name": "Rope", "quantity": 1}),
        ("/api/stash/correct", {"stack_id": "whatever", "quantity": 1}),
        ("/api/loot/drops", {"items": [{"item_name": "Gem", "quantity": 1}]}),
        ("/api/loot/drops/close", {"drop_id": "whatever"}),
        ("/api/treasury/adjust", {"deltas": {"gp": 1}}),
        ("/api/treasury/split/preview", {"amounts": {"gp": 2}}),
        ("/api/treasury/split", {"handle_id": "whatever"}),
        ("/api/session/start", {}),
        ("/api/session/end", {"where_ended": "The Sunken Tomb"}),
        ("/api/combat/open", {"channel_id": "9"}),
        ("/api/combat/close", {}),
        ("/api/characters/estate", {"character_id": "whoever", "destination": "party"}),
        ("/api/maintenance/run", {}),
        ("/api/maintenance/backup", {}),
    )

    def test_a_player_cannot_reach_a_dm_mutation(self) -> None:
        for path, body in self.DM_ONLY:
            with self.subTest(path=path):
                self.assertEqual(self.post(path, body).status_code, 403)

    def test_a_player_cannot_read_the_full_health_report(self) -> None:
        """Unlike `/api/health`, which says only that the process is up."""
        self.assertEqual(self.client.get("/api/maintenance/health", headers=self.headers()).status_code, 403)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_a_dm_can_register_a_character_for_a_player(self) -> None:
        created = self.post("/api/characters", {"name": "Vex", "discord_user_id": PLAYER_ID}, code="dm-code")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["result"]["name"], "Vex")
        # Registered for the player named in the body, not for the DM who is
        # the actor on the call. This is the one identifier a body may carry.
        mine = self.client.get("/api/me/items", headers=self.headers()).json()
        theirs = self.client.get("/api/me/items", headers=self.headers("dm-code")).json()
        self.assertEqual(mine["character"]["name"], "Vex")
        self.assertIsNone(theirs["character"])

    def test_a_dm_token_is_the_only_thing_that_confers_dm_authority(self) -> None:
        """A player who says they are one is still a player."""
        response = self.post(
            "/api/characters", {"name": "Vex", "discord_user_id": PLAYER_ID, "is_dm": True}
        )
        self.assertEqual(response.status_code, 403)

    def test_the_actor_a_mutation_runs_as_is_the_one_the_token_proved(self) -> None:
        self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.register("Brann", DM_ID, interaction="register-brann")
        self.grant("Rope", 2, interaction="grant-rope")
        prepared = self.post(
            "/api/stash/take/prepare",
            {"stack_id": self.stash_stack("Rope")["id"], "amount": 1, "actor_id": DM_ID},
        )
        self.post("/api/stash/take", {"handle_id": prepared.json()["handle_id"], "actor_id": DM_ID})
        self.assertEqual(self.client.get("/api/me/items", headers=self.headers()).json()["items"][0]["quantity"], 1)
        self.assertEqual(self.client.get("/api/me/items", headers=self.headers("dm-code")).json()["items"], [])


class DmSurfaceTests(ApiTestCase):
    """Stage 5: the evening, run without opening a panel.

    The authority on every route below is covered once, in
    `MutationAuthorityTests`. What is here is the behaviour — that a grant
    reaches the stash the same way the bot's does, that closing a drop returns
    what nobody claimed, that ending a session closes what the session owned,
    and that the two-step controls stay two steps.
    """

    def dm(self, path: str, body: dict | None = None, **kwargs):
        return self.post(path, body, code="dm-code", **kwargs)

    def stash_quantity(self, item_name: str) -> int:
        for item in self.client.get("/api/stash", headers=self.headers()).json()["items"]:
            if item["item_name"] == item_name:
                return int(item["quantity"])
        return 0

    # The Party Stash ---------------------------------------------------------

    def test_a_grant_lands_in_the_stash_with_its_provenance(self) -> None:
        granted = self.dm(
            "/api/stash/grant", {"item_name": "Rope", "quantity": 3, "provenance": "The ogre's cave"}
        )
        self.assertEqual(granted.status_code, 200, granted.text)
        self.assertEqual(granted.json()["result"]["new_quantity"], 3)
        self.assertEqual(self.stash_stack("Rope")["provenance"], "The ogre's cave")

    def test_a_correction_removes_from_the_stash_rather_than_moving_it(self) -> None:
        self.grant("Rope", 5, interaction="grant-rope")
        corrected = self.dm(
            "/api/stash/correct",
            {"stack_id": self.stash_stack("Rope")["id"], "quantity": 2, "reason": "Miscounted"},
        )
        self.assertEqual(corrected.status_code, 200, corrected.text)
        self.assertEqual(corrected.json()["result"]["remaining"], 3)
        self.assertEqual(self.stash_quantity("Rope"), 3)
        # Out of the campaign, not into anyone's hands: nothing was credited.
        with self.store.read() as connection:
            held = connection.execute(
                "SELECT COUNT(*) FROM inventory_stacks WHERE owner_type = 'CHARACTER'"
            ).fetchone()[0]
        self.assertEqual(held, 0)

    def test_a_player_cannot_reach_the_stash_through_the_use_route(self) -> None:
        """The flag that separates the two is set by the route, never by a body.

        `consume_interaction` is one call: using up what you carry and removing
        what the party shares differ only by `party_authorized`. If a body could
        carry it, every player would hold the DM's control.
        """
        self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.grant("Rope", 4, interaction="grant-rope")
        refused = self.post(
            "/api/items/use",
            {"stack_id": self.stash_stack("Rope")["id"], "quantity": 1, "party_authorized": True},
        )
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()["code"], "REFUSED")
        self.assertEqual(self.stash_quantity("Rope"), 4)

    # Loot Drops --------------------------------------------------------------

    def test_a_drop_can_hold_more_than_one_item(self) -> None:
        """The panel's drop holds one, because a modal holds five fields."""
        created = self.dm(
            "/api/loot/drops",
            {
                "items": [
                    {"item_name": "Gem", "quantity": 2},
                    {"item_name": "Idol", "quantity": 1, "provenance": "The shrine"},
                ]
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        drops = self.client.get("/api/loot", headers=self.headers()).json()["drops"]
        self.assertEqual(len(drops), 1)
        self.assertEqual({item["item_name"] for item in drops[0]["items"]}, {"Gem", "Idol"})

    def test_closing_a_drop_returns_what_nobody_claimed(self) -> None:
        self.dm("/api/loot/drops", {"items": [{"item_name": "Gem", "quantity": 3}]})
        drop_id = self.client.get("/api/loot", headers=self.headers()).json()["drops"][0]["drop_id"]

        self.register("Vex", PLAYER_ID, interaction="register-vex")
        item_id = self.client.get("/api/loot", headers=self.headers()).json()["drops"][0]["items"][0]["id"]
        self.post("/api/loot/claim", {"drop_item_id": item_id, "amount": 1})

        closed = self.dm("/api/loot/drops/close", {"drop_id": drop_id})
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["result"]["returned_item_count"], 2)
        self.assertEqual(self.stash_quantity("Gem"), 2)
        self.assertEqual(self.client.get("/api/loot", headers=self.headers()).json()["drops"], [])

    # The treasury ------------------------------------------------------------

    def test_the_treasury_is_adjusted_by_a_signed_amount(self) -> None:
        self.dm("/api/treasury/adjust", {"deltas": {"gp": 10}, "reason": "Sold the idol"})
        self.dm("/api/treasury/adjust", {"deltas": {"gp": -4}, "reason": "Paid the ferryman"})
        treasury = self.client.get("/api/treasury", headers=self.headers()).json()["treasury"]
        self.assertEqual(treasury["gp"], 6)

    def test_a_split_previews_its_recipients_before_it_pays_them(self) -> None:
        self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.register("Brann", DM_ID, interaction="register-brann")
        self.dm("/api/treasury/adjust", {"deltas": {"gp": 7}})

        preview = self.client.post(
            "/api/treasury/split/preview",
            json={"amounts": {"gp": 7}},
            headers=self.headers("dm-code"),
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        prepared = preview.json()
        self.assertEqual({row["name"] for row in prepared["recipients"]}, {"Vex", "Brann"})
        self.assertEqual(prepared["per_recipient"]["gp"], 3)
        # Specification 33.1: what will not divide stays where it is.
        self.assertEqual(prepared["remainder"]["gp"], 1)
        # Nothing has moved yet, which is the whole point of the preview.
        self.assertEqual(self.client.get("/api/treasury", headers=self.headers()).json()["treasury"]["gp"], 7)

        committed = self.dm("/api/treasury/split", {"handle_id": prepared["handle_id"]})
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(self.client.get("/api/treasury", headers=self.headers()).json()["treasury"]["gp"], 1)
        self.assertEqual(self.client.get("/api/treasury", headers=self.headers()).json()["purse"]["balance"]["gp"], 3)

    def test_a_roster_that_moves_under_a_split_is_asked_about(self) -> None:
        """The share depends on how many are alive, so a death changes all of them."""
        vex = self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.register("Brann", DM_ID, interaction="register-brann")
        self.dm("/api/treasury/adjust", {"deltas": {"gp": 8}})
        prepared = self.client.post(
            "/api/treasury/split/preview",
            json={"amounts": {"gp": 8}},
            headers=self.headers("dm-code"),
        ).json()

        self.dm("/api/characters/transition", {"character_id": vex, "lifecycle": "DEAD"})

        refused = self.dm("/api/treasury/split", {"handle_id": prepared["handle_id"]})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["code"], "STALE")
        self.assertEqual(self.client.get("/api/treasury", headers=self.headers("dm-code")).json()["treasury"]["gp"], 8)

        answered = self.dm(
            "/api/treasury/split", {"handle_id": prepared["handle_id"], "confirm_current": True}
        )
        self.assertEqual(answered.status_code, 200, answered.text)
        # One recipient now, so the whole eight rather than four each.
        self.assertEqual(answered.json()["result"]["per_recipient"]["gp"], 8)

    # The session -------------------------------------------------------------

    def test_a_session_starts_and_a_second_start_names_the_first(self) -> None:
        started = self.dm("/api/session/start")
        self.assertEqual(started.json()["result"]["session_number"], 1)
        again = self.dm("/api/session/start")
        # Never closed silently — specification 28.2 makes it the DM's decision.
        self.assertEqual(again.json()["result"]["status"], "ACTIVE_EXISTS")
        self.assertEqual(again.json()["result"]["active_session_number"], 1)

    def test_ending_a_session_needs_the_sentence_the_next_one_opens_on(self) -> None:
        self.dm("/api/session/start")
        self.assertEqual(self.dm("/api/session/end", {}).status_code, 422)
        # A space is not an endpoint, and it is the shape a required field
        # collects from somebody who does not want to answer it.
        self.assertEqual(self.dm("/api/session/end", {"where_ended": "   "}).status_code, 422)
        ended = self.dm("/api/session/end", {"where_ended": "  The Sunken Tomb  "})
        self.assertEqual(ended.status_code, 200, ended.text)
        continuity = self.client.get("/api/session/continuity", headers=self.headers()).json()
        self.assertEqual(continuity["previous"]["where_ended"], "The Sunken Tomb")

    def test_ending_a_session_closes_what_the_session_owned(self) -> None:
        self.dm("/api/session/start")
        self.dm("/api/loot/drops", {"items": [{"item_name": "Gem", "quantity": 2}]})
        self.dm("/api/combat/open", {"channel_id": "9"})

        ended = self.dm("/api/session/end", {"where_ended": "The Sunken Tomb"})
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(ended.json()["result"]["closed_drops"], 1)
        self.assertEqual(ended.json()["result"]["closed_combats"], 1)
        self.assertEqual(self.stash_quantity("Gem"), 2)

        continuity = self.client.get("/api/session/continuity", headers=self.headers()).json()
        self.assertIsNone(continuity["active_session_number"])
        self.assertEqual(continuity["previous"]["where_ended"], "The Sunken Tomb")

    # Combat ------------------------------------------------------------------

    def test_combat_is_recorded_against_the_session_and_nothing_else(self) -> None:
        self.assertEqual(self.dm("/api/combat/open", {"channel_id": "9"}).json()["result"]["status"], "NO_ACTIVE_SESSION")
        self.dm("/api/session/start")
        opened = self.dm("/api/combat/open", {"channel_id": "9"})
        self.assertEqual(opened.json()["result"]["status"], "OPENED")
        self.assertEqual(self.client.get("/api/combat", headers=self.headers()).json()["status"], "OPEN")

        closed = self.dm("/api/combat/close", {"outcome": "The ogre fled"})
        self.assertEqual(closed.json()["result"]["status"], "CLOSED")
        status = self.client.get("/api/combat", headers=self.headers()).json()
        self.assertEqual(status["status"], "NO_OPEN_COMBAT")
        self.assertEqual(status["last_closed"]["outcome"], "The ogre fled")

    def test_avrae_status_is_not_queried_without_an_open_quartermaster_encounter(self) -> None:
        class Gateway:
            def execute(self, _request):
                raise AssertionError("Avrae must not be queried without a Q encounter")

        self.app.state.quartermaster = replace(
            self.app.state.quartermaster,
            context=replace(self.context, avrae_gateway=Gateway()),
        )
        response = self.client.get("/api/combat/avrae", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "NOT_QUERIED")

    def test_avrae_status_passes_authenticated_context_to_the_read_only_gateway(self) -> None:
        seen = []

        class Gateway:
            def execute(self, request):
                seen.append(request)
                return ProviderResult(
                    status="COMMITTED",
                    provider_reference="channel:9",
                    provider_version="test-avrae",
                    payload={"active": True},
                )

        self.app.state.quartermaster = replace(
            self.app.state.quartermaster,
            context=replace(self.context, avrae_gateway=Gateway()),
        )
        self.dm("/api/session/start")
        self.dm("/api/combat/open", {"channel_id": "9"})
        response = self.client.get("/api/combat/avrae", headers=self.headers())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "COMMITTED")
        self.assertEqual(response.json()["result"], {"active": True})
        self.assertEqual(seen[0].actor_id, PLAYER_ID)
        self.assertEqual(seen[0].guild_id, GUILD_ID)
        self.assertEqual(seen[0].channel_id, "9")
        self.assertEqual(seen[0].operation_kind, "status")

    def test_avrae_status_timeout_is_explicitly_unknown(self) -> None:
        class Gateway:
            def execute(self, _request):
                raise ProviderTimeout("status response did not arrive")

        self.app.state.quartermaster = replace(
            self.app.state.quartermaster,
            context=replace(self.context, avrae_gateway=Gateway()),
        )
        self.dm("/api/session/start")
        self.dm("/api/combat/open", {"channel_id": "9"})
        response = self.client.get("/api/combat/avrae", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "UNKNOWN")
        self.assertEqual(response.json()["retryable"], False)

    # Characters --------------------------------------------------------------

    def test_a_lifecycle_change_moves_nothing_and_an_estate_moves_it_explicitly(self) -> None:
        vex = self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.grant("Rope", 2, interaction="grant-rope")
        self.take("Rope", "all")

        self.dm("/api/characters/transition", {"character_id": vex, "lifecycle": "DEAD"})
        # The invariant: dying does not hand anything over.
        self.assertEqual(self.stash_quantity("Rope"), 0)

        resolved = self.dm("/api/characters/estate", {"character_id": vex, "destination": "party"})
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["result"]["items_moved"], 1)
        self.assertEqual(self.stash_quantity("Rope"), 2)

    def test_an_active_character_has_no_estate_to_resolve(self) -> None:
        vex = self.register("Vex", PLAYER_ID, interaction="register-vex")
        refused = self.dm("/api/characters/estate", {"character_id": vex, "destination": "party"})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()["code"], "REFUSED")

    # Maintenance -------------------------------------------------------------

    def test_maintenance_backup_and_health_answer_the_dm(self) -> None:
        ran = self.dm("/api/maintenance/run")
        self.assertEqual(ran.status_code, 200, ran.text)
        self.assertEqual(ran.json()["expired_drops"], 0)

        backups = Path(self.directory.name) / "backups"
        client = self._client_with(replace(self.settings, backup_directory=backups))
        taken = self.post("/api/maintenance/backup", code="dm-code", client=client)
        self.assertEqual(taken.status_code, 200, taken.text)
        self.assertTrue(Path(taken.json()["primary_path"]).is_file())

        health = self.client.get("/api/maintenance/health", headers=self.headers("dm-code")).json()
        self.assertEqual(health["report"]["schema_version"], SCHEMA_VERSION)
        # Both renderings of one truth, so a DM reading it here and a DM
        # reading it in Discord are reading the same sentences.
        self.assertIn("Schema", health["rendered"])

    def test_a_backup_that_cannot_be_written_says_why(self) -> None:
        """The person pressing this is the person who can fix it."""
        client = self._client_with(
            replace(self.settings, backup_directory=Path(self.directory.name) / "nope")
        )
        with mock.patch(
            "quartermaster.api_app.create_scheduled_backup",
            side_effect=OSError("No space left on device"),
        ):
            failed = self.post("/api/maintenance/backup", code="dm-code", client=client)
        self.assertEqual(failed.status_code, 500)
        self.assertIn("No space left on device", failed.json()["detail"])

    def _client_with(self, settings) -> TestClient:
        return TestClient(create_app(replace(self.context, settings=settings), self.identity, tokens=self.tokens))


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

    def test_the_page_is_served_behind_the_proxy_prefix_too(self) -> None:
        """A page loaded at `/.proxy/` has to find itself there."""
        response = self._app_serving(self.dist).get(f"{PROXY_PREFIX}/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Quartermaster", response.text)


class ProxyPrefixTests(ApiTestCase):
    """Both path forms Discord's proxy may present, answered identically.

    The client addresses `/.proxy/api/...` because that form has been carried
    since Activities shipped; the proxy forwards it to `/api/...` here, and
    since 2025-07-30 an unprefixed path is forwarded the same way. Answering
    both is what keeps the first launch from depending on which behaviour is
    live — and it is what lets the built page be opened straight from the bind
    for a smoke test, with no proxy in front of it at all.
    """

    def test_a_read_answers_under_both_prefixes(self) -> None:
        headers = self.headers()
        plain = self.client.get("/api/stash", headers=headers)
        proxied = self.client.get(f"{PROXY_PREFIX}/api/stash", headers=headers)
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(proxied.status_code, 200)
        self.assertEqual(plain.json(), proxied.json())

    def test_the_trust_boundary_holds_under_the_prefix(self) -> None:
        """A second mount is a second way in, so it gets the same door."""
        self.assertEqual(self.client.get(f"{PROXY_PREFIX}/api/stash").status_code, 401)
        self.assertEqual(
            self.client.get(f"{PROXY_PREFIX}/api/export", headers=self.headers()).status_code, 403
        )

    def test_a_mutation_answers_under_the_prefix(self) -> None:
        self.grant("Rope", 3, interaction="grant-1")
        self.register("Sable", PLAYER_ID, interaction="register-1")
        stack = self.stash_stack("Rope")
        prepared = self.post(
            f"{PROXY_PREFIX}/api/stash/take/prepare", {"stack_id": stack["id"], "amount": 1}
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        took = self.post(
            f"{PROXY_PREFIX}/api/stash/take", {"handle_id": prepared.json()["handle_id"]}
        )
        self.assertEqual(took.status_code, 200, took.text)
        self.assertEqual(took.json()["result"]["quantity"], 1)

    def test_the_live_feed_upgrades_under_the_prefix(self) -> None:
        token = self.authenticate("player-code")["token"]
        # Entered as a context manager, which is what runs the lifespan and so
        # starts the pump; a socket against an unstarted feed is refused rather
        # than left to look live.
        with TestClient(self.app) as served:
            with served.websocket_connect(f"{PROXY_PREFIX}/api/live") as socket:
                socket.send_json({"token": token})
                self.assertEqual(_next_message(socket)["type"], "hello")


class WebSocketDependencyTests(unittest.TestCase):
    """The dependency whose absence looks like a working Activity.

    With neither websockets nor wsproto installed, uvicorn serves every route
    normally and answers the upgrade on `/api/live` with a 404. The screen then
    loads, says it is connecting, backs off, and shows numbers that stopped
    moving — the exact failure the live feed exists to prevent, produced by a
    missing package rather than by anything in the code.
    """

    def test_a_missing_implementation_is_named_rather_than_served_into(self) -> None:
        with mock.patch("quartermaster.api_server.importlib.util.find_spec", return_value=None):
            with self.assertRaises(ConfigurationError) as raised:
                _require_websockets()
        self.assertIn("websockets", str(raised.exception))

    def test_either_implementation_is_enough(self) -> None:
        for present in ("websockets", "wsproto"):
            with self.subTest(implementation=present):
                with mock.patch(
                    "quartermaster.api_server.importlib.util.find_spec",
                    side_effect=lambda name, wanted=present: object() if name == wanted else None,
                ):
                    _require_websockets()

    def test_the_bot_keeps_running_when_the_activity_cannot_be_served(self) -> None:
        """The bot is the surface the table still has; it does not come down."""
        settings = Settings(
            guild_id=GUILD_ID,
            database_path=Path("unused.sqlite"),
            discord_client_id="1234",
            discord_client_secret=CLIENT_SECRET,
        )
        context = mock.Mock(settings=settings)
        with mock.patch("quartermaster.api_server.importlib.util.find_spec", return_value=None):
            with self.assertLogs("quartermaster.api_server", level="ERROR") as logs:
                asyncio.run(serve_api(context, asyncio.Event()))
        self.assertIn("not served", logs.output[0])


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

    def test_a_take_made_through_the_activity_reaches_the_rest_of_the_table(self) -> None:
        """Stage 4's half of the criterion Stage 3 set for a grant.

        The bot changing the domain was the case that had to work first. This
        is the one the migration is actually for: a player acts on the Activity
        and every other screen at the table hears about it.
        """
        self.register("Vex", PLAYER_ID, interaction="register-vex")
        self.grant("Rope", 2, interaction="grant-rope")
        socket, _ = self.opened(code="dm-code")
        self.take("Rope", "all")
        notice = _next_message(socket)
        self.assertEqual(notice["type"], EVENTS)
        self.assertIn("ITEM_TAKEN", [event["event_type"] for event in notice["events"]])

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

    def test_a_table_that_never_enabled_the_activity_is_not_warned_about_it(self) -> None:
        settings = Settings.from_env(self._environment())
        self.assertFalse(settings.activity_half_configured)

    def test_a_served_activity_is_not_warned_about_either(self) -> None:
        settings = Settings.from_env(
            self._environment(
                QM_DISCORD_CLIENT_ID="1234",
                QM_DISCORD_CLIENT_SECRET="shhh",
                QM_ACTIVITY_DIST="activity/dist",
            )
        )
        self.assertFalse(settings.activity_half_configured)

    def test_somewhere_to_serve_it_from_without_the_credentials_is_reported(self) -> None:
        """The failure the start script used to cause, and the reason it warns.

        The bot starts, the API does not, and the only symptom at the table is
        a launcher that opens nothing.
        """
        for missing in (
            {"QM_ACTIVITY_DIST": "activity/dist"},
            {"QM_ACTIVITY_ORIGIN": "https://example.invalid"},
            {"QM_ACTIVITY_DIST": "activity/dist", "QM_DISCORD_CLIENT_ID": "1234"},
        ):
            with self.subTest(**missing):
                settings = Settings.from_env(self._environment(**missing))
                self.assertFalse(settings.activity_enabled)
                self.assertTrue(settings.activity_half_configured)

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

    def test_avrae_adapter_is_optional_and_validates_its_pair_of_credentials(self) -> None:
        settings = Settings.from_env(
            self._environment(
                QM_AVRAE_ADAPTER_URL="https://avrae.example/quartermaster/status/",
                QM_AVRAE_ADAPTER_SECRET="shared-secret",
            )
        )
        self.assertTrue(settings.avrae_adapter_enabled)
        self.assertEqual(settings.avrae_adapter_url, "https://avrae.example/quartermaster/status")
        self.assertEqual(settings.avrae_adapter_timeout_seconds, 2.5)

        for overrides in (
            {"QM_AVRAE_ADAPTER_URL": "https://avrae.example/status"},
            {"QM_AVRAE_ADAPTER_SECRET": "shared-secret"},
            {"QM_AVRAE_ADAPTER_URL": "ftp://avrae.example/status", "QM_AVRAE_ADAPTER_SECRET": "secret"},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(self._environment(**overrides))


class PreflightTests(unittest.TestCase):
    """Stage 0's checks, checked.

    `preflight` exists to catch the things that would otherwise be discovered
    from a blank frame inside a Discord client. The cases worth proving here
    are the ones where a broken setup looks exactly like a working one — most
    of all a bundle built without a client id, which compiles and serves and
    contains no application.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dist = Path(self.directory.name) / "dist"
        (self.dist / "assets").mkdir(parents=True)

    def _settings(self, **overrides) -> Settings:
        return Settings(
            guild_id=GUILD_ID,
            database_path=Path(self.directory.name) / "quartermaster.sqlite",
            discord_client_id="1234",
            discord_client_secret=CLIENT_SECRET,
            activity_dist=self.dist,
            **overrides,
        )

    def _named(self, checks: list, name: str):
        for check in checks:
            if check.name == name:
                return check
        raise AssertionError(f"no {name!r} check among {[check.name for check in checks]}")

    def _write_bundle(self, body: str) -> None:
        (self.dist / "index.html").write_text("<!doctype html><title>Quartermaster</title>", encoding="utf-8")
        (self.dist / "assets" / "index-abc.js").write_text(body, encoding="utf-8")

    def test_a_bundle_built_without_a_client_id_is_caught(self) -> None:
        """The failure that looks like success.

        Vite replaces `import.meta.env` at build time, so with no
        `VITE_DISCORD_CLIENT_ID` the boot sequence returns on its first branch
        and the bundler removes the application as unreachable. The build
        succeeds, the page serves, and nothing happens in the frame.
        """
        self._write_bundle("console.log('an application that was tree-shaken away')")
        self.assertFalse(self._named(_built_page(self._settings()), "bundle").passed)

    def test_a_bundle_that_still_carries_the_application_passes(self) -> None:
        self._write_bundle('fetch("/.proxy/api/home")')
        self.assertTrue(self._named(_built_page(self._settings()), "bundle").passed)

    def test_a_build_that_never_ran_is_named_as_such(self) -> None:
        self.assertFalse(self._named(_built_page(self._settings()), "built page").passed)

    def test_an_unconfigured_activity_stops_before_serving(self) -> None:
        """Nothing below the configuration check can be asked without a secret."""
        settings = replace(self._settings(), discord_client_id=None, discord_client_secret=None)
        checks, remaining = run_preflight(settings)
        self.assertFalse(self._named(checks, "configuration").passed)
        self.assertNotIn("origin", [check.name for check in checks])
        self.assertIn("URL Mappings", remaining)


def _unpad(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _pad(value: str) -> str:
    import base64

    return base64.urlsafe_b64encode(value.encode()).decode("ascii").rstrip("=")


if __name__ == "__main__":
    unittest.main()
