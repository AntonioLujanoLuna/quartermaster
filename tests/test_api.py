"""Behavioural coverage for the Activity API.

The panel tests drive the surface the way the table does. These drive it the
way a browser does, which means the interesting cases are not the reads — those
are the same service calls the panels make — but everything around them: a
caller who presents no token, a forged one, an expired one, a token that claims
DM authority it was not issued with, and a request that tries to name an actor
other than the one it proved.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from fastapi.testclient import TestClient

from quartermaster.api_app import create_app
from quartermaster.api_auth import (
    Actor,
    Identity,
    IdentityError,
    SessionTokens,
    TokenError,
    is_dm,
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
                "player-code": Identity(user_id=PLAYER_ID, guild_roles=("1",)),
                "dm-code": Identity(user_id=DM_ID, guild_roles=("1", DM_ROLE_ID)),
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
