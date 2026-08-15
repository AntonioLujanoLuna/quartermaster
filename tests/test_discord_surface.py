"""Behavioural coverage for the Discord command and component surface.

The registration test only proved that command names exist. These drive the
command bodies and view callbacks themselves — authorization, refusals, the
wording a player reads, and the canonical state each one leaves behind — because
that surface is what the table actually touches and it was the least covered
part of the codebase.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import discord
from discord import app_commands

from quartermaster.characters import CharacterService
from quartermaster.config import Settings
from quartermaster.currency import CurrencyService
from quartermaster.db import SQLiteStore
from quartermaster.discord_adapter import create_bot
from quartermaster.discord_common import BotServices
from quartermaster.discord_views import (
    GrantLootModal,
    LauncherMoreView,
    PartyStashView,
    QuartermasterLauncherView,
    TakeConfirmationView,
)
from quartermaster.handles import HandleRepository
from quartermaster.inventory import InventoryService
from quartermaster.loot import LootDropService
from quartermaster.receipts import ReceiptRepository
from quartermaster.rendering import DISCORD_MESSAGE_LIMIT
from quartermaster.sessions import SessionService

GUILD_ID = 4242
OTHER_GUILD_ID = 9999
OWNER_ID = 11
PLAYER_ID = 22
BYSTANDER_ID = 33
CHANNEL_ID = 555

_interaction_ids = itertools.count(100_000)


class FakeResponse:
    """The half of a Discord interaction that can be acknowledged exactly once."""

    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict]] = []
        self.deferred = False
        self.modal: discord.ui.Modal | None = None
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str | None = None, **kwargs: object) -> None:
        if self._done:
            raise RuntimeError("interaction was already acknowledged")
        self._done = True
        self.messages.append((content, dict(kwargs)))

    async def defer(self, *, ephemeral: bool = False) -> None:
        if self._done:
            raise RuntimeError("interaction was already acknowledged")
        self._done = True
        self.deferred = True

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._done = True
        self.modal = modal


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict]] = []

    async def send(self, content: str | None = None, **kwargs: object) -> None:
        self.messages.append((content, dict(kwargs)))


class FakeInteraction:
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int = GUILD_ID,
        owner_id: int = OWNER_ID,
        user: object | None = None,
    ) -> None:
        self.id = next(_interaction_ids)
        self.guild_id = guild_id
        self.channel_id = CHANNEL_ID
        self.user = user if user is not None else SimpleNamespace(id=user_id)
        self.guild = SimpleNamespace(id=guild_id, owner_id=owner_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.client = SimpleNamespace()

    @property
    def replies(self) -> list[tuple[str | None, dict]]:
        return self.response.messages + self.followup.messages

    @property
    def text(self) -> str:
        """The content of the single reply this interaction produced."""
        if not self.replies:
            raise AssertionError("the interaction produced no reply")
        return str(self.replies[-1][0])

    @property
    def kwargs(self) -> dict:
        return self.replies[-1][1]


def run(coroutine) -> object:
    return asyncio.run(coroutine)


class DiscordSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteStore(self.root / "quartermaster.sqlite").open()
        self.receipts = ReceiptRepository(self.store)
        self.handles = HandleRepository(self.store)
        self.inventory = InventoryService(self.store, self.receipts, self.handles)
        self.loot = LootDropService(self.store, self.receipts, self.handles)
        self.sessions = SessionService(self.store, self.receipts, self.loot)
        self.characters = CharacterService(self.store, self.receipts)
        self.currency = CurrencyService(self.store, self.receipts, handles=self.handles)
        self.services = BotServices(
            store=self.store,
            receipts=self.receipts,
            inventory=self.inventory,
            sessions=self.sessions,
            characters=self.characters,
            currency=self.currency,
            loot=self.loot,
        )
        self.settings = Settings(
            guild_id=str(GUILD_ID),
            database_path=self.root / "quartermaster.sqlite",
            backup_directory=self.root / "backups",
            soft_deadline_seconds=5.0,
        )
        self.bot = create_bot(self.settings, self.services)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    # Harness helpers -----------------------------------------------------

    def command(self, name: str):
        for command in self.bot.tree.get_commands(guild=discord.Object(id=GUILD_ID)):
            if command.name == name:
                return command.callback
        raise AssertionError(f"no registered command named {name}")

    def dm(self) -> FakeInteraction:
        return FakeInteraction(user_id=OWNER_ID)

    def player(self) -> FakeInteraction:
        return FakeInteraction(user_id=PLAYER_ID)

    def bystander(self) -> FakeInteraction:
        return FakeInteraction(user_id=BYSTANDER_ID)

    def elsewhere(self) -> FakeInteraction:
        return FakeInteraction(user_id=OWNER_ID, guild_id=OTHER_GUILD_ID, owner_id=OWNER_ID)

    def register_player_character(self, name: str = "Tamsin", user_id: int = PLAYER_ID) -> str:
        interaction = self.dm()
        run(self.command("character-add")(interaction, name=name, discord_user_id=str(user_id)))
        return str(
            self.store.connection.execute(
                "SELECT id FROM characters WHERE name = ?", (name,)
            ).fetchone()["id"]
        )

    def stash_quantities(self) -> dict[str, int]:
        return {
            row["item_name"]: row["quantity"]
            for row in self.store.connection.execute(
                "SELECT item_name, quantity FROM inventory_stacks WHERE owner_type = 'PARTY'"
            )
        }

    # Authorization -------------------------------------------------------

    def test_dm_only_commands_refuse_a_non_dm_and_change_nothing(self) -> None:
        invocations = {
            "grant": {"item": "Crown", "quantity": 1},
            "loot-drop": {"item": "Crown", "quantity": 1},
            "loot-close": {"drop_id": "whatever"},
            "session-start": {},
            "session-end": {"where_ended": "The inn"},
            "treasury-adjust": {"gp": 5},
            "treasury-split": {"gp": 5},
            "treasury-give": {"character_id": "whoever", "gp": 5},
            "character-add": {"name": "Interloper"},
            "character-lifecycle": {"character_id": "whoever", "lifecycle": "DEAD"},
            "character-resolve": {"character_id": "whoever", "destination": "party"},
            "export": {},
            "backup": {},
        }
        self.assertEqual(
            set(invocations),
            {
                name
                for name in [
                    "grant", "loot-drop", "loot-close", "session-start", "session-end",
                    "treasury-adjust", "treasury-split", "treasury-give", "character-add",
                    "character-lifecycle", "character-resolve", "export", "backup",
                ]
            },
            "every DM-only command should be exercised here",
        )
        for name, arguments in invocations.items():
            with self.subTest(command=name):
                interaction = self.bystander()
                run(self.command(name)(interaction, **arguments))
                self.assertIn("Only configured DM administrators", interaction.text)
                self.assertTrue(interaction.kwargs["ephemeral"])
        self.assertEqual(self.stash_quantities(), {})
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0
        )
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM characters").fetchone()[0], 0
        )

    def test_player_commands_refuse_interactions_from_another_guild(self) -> None:
        for name in ["stash", "loot", "treasury", "characters"]:
            with self.subTest(command=name):
                interaction = self.elsewhere()
                run(self.command(name)(interaction))
                self.assertIn("configured for a different guild", interaction.text)

    def test_dm_commands_from_another_guild_refuse_with_the_authorization_message(self) -> None:
        # DM-only commands fold the guild and DM checks into one refusal, so the
        # wrong guild reads as "not an administrator" rather than "wrong guild".
        interaction = self.elsewhere()
        run(self.command("grant")(interaction, item="Crown", quantity=1))
        self.assertIn("Only configured DM administrators", interaction.text)
        self.assertEqual(self.stash_quantities(), {})

    def test_a_configured_dm_role_can_run_dm_commands(self) -> None:
        member = MagicMock(spec=discord.Member)
        member.id = BYSTANDER_ID
        member.guild_permissions.manage_guild = False
        member.roles = [SimpleNamespace(id=77)]
        self.settings = Settings(
            guild_id=str(GUILD_ID),
            database_path=self.root / "quartermaster.sqlite",
            dm_role_ids=("77",),
            soft_deadline_seconds=5.0,
        )
        self.bot = create_bot(self.settings, self.services)
        interaction = FakeInteraction(user_id=BYSTANDER_ID, user=member)
        run(self.command("grant")(interaction, item="Banner", quantity=1))
        self.assertEqual(interaction.text, "Granted 1 Banner. Total: 1.")

    # Party Stash ---------------------------------------------------------

    def test_grant_adds_to_the_stash_and_reports_the_running_total(self) -> None:
        first = self.dm()
        run(self.command("grant")(first, item="Silvered Dagger", quantity=2))
        self.assertEqual(first.text, "Granted 2 Silvered Dagger. Total: 2.")

        # Case and internal spacing merge into the existing stack, while the
        # reply echoes back what the DM actually typed.
        second = self.dm()
        run(self.command("grant")(second, item="silvered  dagger", quantity=3))
        self.assertEqual(second.text, "Granted 3 silvered  dagger. Total: 5.")
        self.assertEqual(self.stash_quantities(), {"Silvered Dagger": 5})

    def test_grant_refuses_a_blank_item_name(self) -> None:
        interaction = self.dm()
        run(self.command("grant")(interaction, item="   ", quantity=1))
        self.assertIn("item name is required", interaction.text)
        self.assertEqual(self.stash_quantities(), {})

    def test_stash_renders_the_contents_and_offers_the_browse_control(self) -> None:
        run(self.command("grant")(self.dm(), item="Rope", quantity=1))
        interaction = self.player()
        run(self.command("stash")(interaction))
        self.assertIn("**PARTY STASH**", interaction.text)
        self.assertIn("• Rope x1", interaction.text)
        self.assertIsInstance(interaction.kwargs["view"], PartyStashView)

    def test_empty_stash_says_so(self) -> None:
        interaction = self.player()
        run(self.command("stash")(interaction))
        self.assertIn("Nothing is recorded yet.", interaction.text)

    def test_a_stash_too_large_for_one_message_still_answers_and_says_so(self) -> None:
        """`/stash` must survive the campaign that fills it.

        Discord rejects content over 2000 characters outright, so an unbounded
        listing does not read badly — it fails the interaction, and the player
        sees only that Quartermaster did not respond.
        """
        for index in range(120):
            run(self.command("grant")(self.dm(), item=f"Relic of the Sunken Court {index}", quantity=1))
        interaction = self.player()
        run(self.command("stash")(interaction))
        self.assertLessEqual(len(interaction.text), DISCORD_MESSAGE_LIMIT)
        self.assertIn("**PARTY STASH**", interaction.text)
        self.assertIn("not shown here", interaction.text)

    def test_browse_says_when_it_holds_only_part_of_the_stash(self) -> None:
        """A capped snapshot read as the whole stash is worse than a short one."""
        for index in range(40):
            run(self.command("grant")(self.dm(), item=f"Relic {index}", quantity=1))
        stash_interaction = self.player()
        run(self.command("stash")(stash_interaction))
        browse_interaction = self.player()
        run(stash_interaction.kwargs["view"].children[0].callback(browse_interaction))
        self.assertIn("Showing 25 of 40 stacks", browse_interaction.text)
        self.assertLessEqual(len(browse_interaction.text), DISCORD_MESSAGE_LIMIT)

    def test_browse_names_the_stacks_it_has_no_take_control_for(self) -> None:
        """A stack above one costs two controls; one view holds twenty-five.

        Twenty listed stacks wanted forty buttons and got twenty-five, so eight
        of them were listed with nothing to press and nothing said — the same
        gap the Loot Drop listing already names.
        """
        self.register_player_character()
        for index in range(20):
            run(self.command("grant")(self.dm(), item=f"Relic {index}", quantity=3))
        stash_interaction = self.player()
        run(self.command("stash")(stash_interaction))
        browse_interaction = self.player()
        run(stash_interaction.kwargs["view"].children[0].callback(browse_interaction))
        view = browse_interaction.kwargs["view"]
        self.assertLessEqual(len(view.children), 25)
        self.assertIn("have no take control here", browse_interaction.text)
        self.assertLessEqual(len(browse_interaction.text), DISCORD_MESSAGE_LIMIT)
        # Every control the message admits to is a button that exists, and
        # every button belongs to a stack the message lists.
        listed = [line for line in browse_interaction.text.splitlines() if line.startswith("• ")]
        uncontrolled = int(browse_interaction.text.split("The last ")[1].split(" ")[0])
        self.assertEqual(len(view.children), sum(2 for _ in range(len(listed) - uncontrolled)))


    def test_a_character_roster_too_large_for_one_message_still_answers(self) -> None:
        for index in range(60):
            run(
                self.command("character-add")(
                    self.dm(), name=f"Adventurer of the Long Road {index}", discord_user_id=None
                )
            )
        interaction = self.player()
        run(self.command("characters")(interaction))
        self.assertLessEqual(len(interaction.text), DISCORD_MESSAGE_LIMIT)
        self.assertIn("not shown here", interaction.text)

    def test_browse_then_take_transfers_the_item_to_the_players_character(self) -> None:
        character_id = self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=2))

        stash_interaction = self.player()
        run(self.command("stash")(stash_interaction))
        browse = stash_interaction.kwargs["view"].children[0]

        browse_interaction = self.player()
        run(browse.callback(browse_interaction))
        take_view = browse_interaction.kwargs["view"]
        self.assertEqual(
            [button.label for button in take_view.children], ["Take 1 Rope", "Take all Rope"]
        )

        take_interaction = self.player()
        run(take_view.children[0].callback(take_interaction))
        self.assertEqual(take_interaction.text, "You took 1 Rope. 1 remain.")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT quantity FROM inventory_stacks WHERE owner_type = 'CHARACTER' AND owner_id = ?",
                (character_id,),
            ).fetchone()["quantity"],
            1,
        )
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_taking_without_a_registered_character_is_refused(self) -> None:
        run(self.command("grant")(self.dm(), item="Rope", quantity=1))
        stash_interaction = self.player()
        run(self.command("stash")(stash_interaction))
        browse_interaction = self.player()
        run(stash_interaction.kwargs["view"].children[0].callback(browse_interaction))

        take_interaction = self.player()
        run(browse_interaction.kwargs["view"].children[0].callback(take_interaction))
        self.assertIn("active registered character is required", take_interaction.text)
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_a_consumed_take_handle_is_refused_on_a_second_press(self) -> None:
        self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=2))
        stash_interaction = self.player()
        run(self.command("stash")(stash_interaction))
        browse_interaction = self.player()
        run(stash_interaction.kwargs["view"].children[0].callback(browse_interaction))
        button = browse_interaction.kwargs["view"].children[0]

        run(button.callback(self.player()))
        second_press = self.player()
        run(button.callback(second_press))
        self.assertIn("could not be completed", second_press.text)
        self.assertIn("HANDLE_CONSUMED", second_press.text)
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def browse_buttons(self) -> list:
        """Open the stash, press Browse, and hand back the per-item controls."""
        stash_interaction = self.player()
        run(self.command("stash")(stash_interaction))
        browse_interaction = self.player()
        run(stash_interaction.kwargs["view"].children[0].callback(browse_interaction))
        return list(browse_interaction.kwargs["view"].children)

    def test_take_all_moves_the_whole_stack(self) -> None:
        character_id = self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=4))
        take_all = next(b for b in self.browse_buttons() if b.label == "Take all Rope")

        interaction = self.player()
        run(take_all.callback(interaction))
        self.assertEqual(interaction.text, "You took 4 Rope. 0 remain.")
        self.assertEqual(self.stash_quantities(), {})
        self.assertEqual(
            self.store.connection.execute(
                "SELECT quantity FROM inventory_stacks WHERE owner_id = ?", (character_id,)
            ).fetchone()["quantity"],
            4,
        )

    def test_a_single_item_stack_offers_no_take_all_control(self) -> None:
        self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=1))
        self.assertEqual([b.label for b in self.browse_buttons()], ["Take 1 Rope"])

    def test_take_all_asks_for_confirmation_when_the_quantity_moved(self) -> None:
        """The point of the relative handle: 'all' must not silently mean a new number."""
        character_id = self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=2))
        take_all = next(b for b in self.browse_buttons() if b.label == "Take all Rope")

        # The DM adds more rope after the player rendered the view.
        run(self.command("grant")(self.dm(), item="Rope", quantity=5))

        stale = self.player()
        run(take_all.callback(stale))
        self.assertIn("The quantity changed.", stale.text)
        confirmation = stale.kwargs["view"]
        self.assertIsInstance(confirmation, TakeConfirmationView)
        self.assertEqual(self.stash_quantities(), {"Rope": 7})

        confirmed = self.player()
        run(confirmation.children[0].callback(confirmed))
        self.assertEqual(confirmed.text, "You took 7 Rope. 0 remain.")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT quantity FROM inventory_stacks WHERE owner_id = ?", (character_id,)
            ).fetchone()["quantity"],
            7,
        )

    def test_declining_the_stale_confirmation_leaves_the_stash_alone(self) -> None:
        self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=2))
        take_all = next(b for b in self.browse_buttons() if b.label == "Take all Rope")
        run(self.command("grant")(self.dm(), item="Rope", quantity=5))
        run(take_all.callback(self.player()))
        # Walking away from the confirmation is simply never pressing it.
        self.assertEqual(self.stash_quantities(), {"Rope": 7})

    # Loot Drops ----------------------------------------------------------

    def test_loot_drop_can_be_created_listed_and_claimed(self) -> None:
        character_id = self.register_player_character()
        run(self.command("session-start")(self.dm()))
        create = self.dm()
        run(self.command("loot-drop")(create, item="Loot Gem", quantity=2, expiry_hours=72))
        self.assertRegex(create.text, r"^Loot Drop `[0-9a-f]{8}` created with 2 Loot Gem\.$")

        listing = self.player()
        run(self.command("loot")(listing))
        self.assertIn("**OPEN LOOT**", listing.text)
        self.assertIn("• Loot Gem x2", listing.text)

        claim = self.player()
        run(listing.kwargs["view"].children[0].callback(claim))
        self.assertEqual(claim.text, "You claimed 1 Loot Gem. 1 remain.")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT quantity FROM inventory_stacks WHERE owner_id = ?", (character_id,)
            ).fetchone()["quantity"],
            1,
        )

    def test_loot_listing_names_items_it_has_no_claim_control_for(self) -> None:
        """One view carries a bounded number of buttons; the rest must be admitted.

        Listing an item with no control and saying nothing leaves the player
        waiting for a button that is never going to appear.
        """
        self.register_player_character()
        for index in range(30):
            run(self.command("loot-drop")(self.dm(), item=f"Loot Gem {index}", quantity=1))
        listing = self.player()
        run(self.command("loot")(listing))
        self.assertLessEqual(len(listing.text), DISCORD_MESSAGE_LIMIT)
        self.assertIn("have no claim control here", listing.text)
        self.assertLessEqual(len(listing.kwargs["view"].children), 25)

    def test_loot_listing_is_explicit_when_there_is_nothing_open(self) -> None:
        interaction = self.player()
        run(self.command("loot")(interaction))
        self.assertIn("There are no open Loot Drops.", interaction.text)

    def test_closing_a_loot_drop_returns_the_remainder_to_the_stash(self) -> None:
        run(self.command("loot-drop")(self.dm(), item="Loot Gem", quantity=3))
        drop_id = str(self.store.connection.execute("SELECT id FROM loot_drops").fetchone()["id"])
        close = self.dm()
        run(self.command("loot-close")(close, drop_id=drop_id))
        self.assertEqual(close.text, f"Loot Drop `{drop_id[:8]}` closed.")
        self.assertEqual(self.stash_quantities(), {"Loot Gem": 3})

    def test_closing_an_unknown_loot_drop_reports_the_failure(self) -> None:
        interaction = self.dm()
        run(self.command("loot-close")(interaction, drop_id="not-a-drop"))
        self.assertIn("loot drop not found", interaction.text)

    # Sessions ------------------------------------------------------------

    def test_session_start_and_end_report_the_session_number(self) -> None:
        start = self.dm()
        run(self.command("session-start")(start))
        self.assertEqual(start.text, "Session 1 started.")

        end = self.dm()
        run(self.command("session-end")(end, where_ended="The Sunken Tomb"))
        self.assertEqual(end.text, "Session 1 closed.")
        self.assertEqual(
            self.store.connection.execute("SELECT where_ended FROM sessions").fetchone()["where_ended"],
            "The Sunken Tomb",
        )

    def test_starting_a_second_session_points_at_the_open_one(self) -> None:
        run(self.command("session-start")(self.dm()))
        second = self.dm()
        run(self.command("session-start")(second))
        self.assertIn("Session 1 is still active", second.text)
        self.assertTrue(second.kwargs["ephemeral"])
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1
        )

    def test_ending_with_no_active_session_says_so(self) -> None:
        interaction = self.dm()
        run(self.command("session-end")(interaction, where_ended="Nowhere"))
        self.assertEqual(interaction.text, "There is no active session.")

    # Treasury ------------------------------------------------------------

    def test_treasury_can_be_adjusted_viewed_split_and_given(self) -> None:
        alpha = self.register_player_character("Alpha", PLAYER_ID)
        self.register_player_character("Beta", BYSTANDER_ID)

        adjust = self.dm()
        run(self.command("treasury-adjust")(adjust, gp=101, reason="Dragon hoard"))
        self.assertEqual(adjust.text, "Treasury updated: 0 cp · 0 sp · 101 gp · 0 pp.")

        view = self.player()
        run(self.command("treasury")(view))
        self.assertEqual(view.text, "Treasury: 0 cp · 0 sp · 101 gp · 0 pp")

        split = self.dm()
        run(self.command("treasury-split")(split, gp=101))
        self.assertIn("Split among 2 active characters", split.text)
        self.assertIn("50 gp", split.text)
        # Specification 33.1: the indivisible remainder stays with the treasury.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT gp FROM currency_balances WHERE owner_type = 'PARTY'"
            ).fetchone()["gp"],
            1,
        )

        give = self.dm()
        run(self.command("treasury-give")(give, character_id=alpha, gp=1))
        self.assertEqual(give.text, "Gave 0 cp · 0 sp · 1 gp · 0 pp to Alpha.")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT gp FROM currency_balances WHERE owner_type = 'PARTY'"
            ).fetchone()["gp"],
            0,
        )

    def test_treasury_adjustment_cannot_drive_a_balance_negative(self) -> None:
        interaction = self.dm()
        run(self.command("treasury-adjust")(interaction, gp=-5))
        self.assertIn("cannot become negative", interaction.text)

    def test_giving_more_than_the_treasury_holds_is_refused(self) -> None:
        character_id = self.register_player_character("Alpha", PLAYER_ID)
        interaction = self.dm()
        run(self.command("treasury-give")(interaction, character_id=character_id, gp=10))
        self.assertIn("does not contain enough currency", interaction.text)

    def test_giving_to_a_non_active_character_is_refused(self) -> None:
        character_id = self.register_player_character("Alpha", PLAYER_ID)
        run(self.command("treasury-adjust")(self.dm(), gp=10))
        run(self.command("character-lifecycle")(self.dm(), character_id=character_id, lifecycle="DEAD"))

        interaction = self.dm()
        run(self.command("treasury-give")(interaction, character_id=character_id, gp=5))
        self.assertIn("only active characters can receive currency", interaction.text)

    def test_splitting_with_no_active_characters_is_refused(self) -> None:
        run(self.command("treasury-adjust")(self.dm(), gp=10))
        interaction = self.dm()
        run(self.command("treasury-split")(interaction, gp=10))
        self.assertIn("at least one active character is required", interaction.text)

    # Characters ----------------------------------------------------------

    def test_characters_can_be_registered_listed_and_transitioned(self) -> None:
        add = self.dm()
        run(self.command("character-add")(add, name="Tamsin", discord_user_id=str(PLAYER_ID)))
        self.assertRegex(add.text, r"^Registered Tamsin with ID `[0-9a-f-]{36}`\.$")

        listing = self.player()
        run(self.command("characters")(listing))
        self.assertIn("Tamsin", listing.text)
        self.assertIn("ACTIVE", listing.text)

        character_id = str(
            self.store.connection.execute("SELECT id FROM characters").fetchone()["id"]
        )
        transition = self.dm()
        run(self.command("character-lifecycle")(transition, character_id=character_id, lifecycle="RETIRED"))
        self.assertEqual(transition.text, "Tamsin moved from ACTIVE to RETIRED.")

    def test_registering_a_second_active_character_for_one_player_is_refused(self) -> None:
        run(self.command("character-add")(self.dm(), name="Tamsin", discord_user_id=str(PLAYER_ID)))
        second = self.dm()
        run(self.command("character-add")(second, name="Rowan", discord_user_id=str(PLAYER_ID)))
        self.assertIn("Tamsin is already active", second.text)
        self.assertEqual(
            self.store.connection.execute("SELECT COUNT(*) FROM characters").fetchone()[0], 1
        )

    def test_an_impossible_lifecycle_transition_is_refused(self) -> None:
        character_id = self.register_player_character()
        run(self.command("character-lifecycle")(self.dm(), character_id=character_id, lifecycle="DEAD"))
        interaction = self.dm()
        run(self.command("character-lifecycle")(interaction, character_id=character_id, lifecycle="RETIRED"))
        self.assertIn("cannot transition DEAD to RETIRED", interaction.text)

    def test_an_unknown_lifecycle_name_is_refused(self) -> None:
        character_id = self.register_player_character()
        interaction = self.dm()
        run(self.command("character-lifecycle")(interaction, character_id=character_id, lifecycle="SLEEPY"))
        self.assertIn("unknown lifecycle", interaction.text)

    def test_resolving_belongings_moves_them_to_the_party_stash(self) -> None:
        character_id = self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=1))
        stash_interaction = self.player()
        run(self.command("stash")(stash_interaction))
        browse_interaction = self.player()
        run(stash_interaction.kwargs["view"].children[0].callback(browse_interaction))
        run(browse_interaction.kwargs["view"].children[0].callback(self.player()))
        run(self.command("character-lifecycle")(self.dm(), character_id=character_id, lifecycle="DEAD"))

        resolve = self.dm()
        run(self.command("character-resolve")(resolve, character_id=character_id, destination="party"))
        self.assertIn("Resolved 1 item stacks", resolve.text)
        self.assertIn("to Party Stash", resolve.text)
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_resolving_an_active_character_is_refused(self) -> None:
        character_id = self.register_player_character()
        interaction = self.dm()
        run(self.command("character-resolve")(interaction, character_id=character_id, destination="party"))
        self.assertIn("only non-active characters can resolve belongings", interaction.text)

    # Avrae handoff -------------------------------------------------------

    def combat(self, interaction: FakeInteraction, value: str, **kwargs: object) -> None:
        run(self.command("combat")(interaction, action=app_commands.Choice(name=value, value=value), **kwargs))

    def encounter_statuses(self) -> list[str]:
        return [
            row["status"]
            for row in self.store.connection.execute(
                "SELECT status FROM combat_encounters ORDER BY opened_at"
            )
        ]

    def test_combat_handoff_requires_an_active_session(self) -> None:
        interaction = self.player()
        self.combat(interaction, "join")
        self.assertIn("No active Quartermaster session", interaction.text)

    def test_combat_handoff_renders_the_native_avrae_command(self) -> None:
        run(self.command("session-start")(self.dm()))
        interaction = self.player()
        self.combat(interaction, "join")
        self.assertIn("**AVRAE HANDOFF · JOIN**", interaction.text)
        self.assertIn("`!i join`", interaction.text)
        self.assertIn(f"<#{CHANNEL_ID}>", interaction.text)
        self.assertIn("Avrae remains authoritative", interaction.text)

    def test_combat_handoff_actions_stay_open_to_players(self) -> None:
        run(self.command("session-start")(self.dm()))
        for value in ["join", "next", "attack", "cast", "check", "save", "status"]:
            with self.subTest(action=value):
                interaction = self.player()
                self.combat(interaction, value)
                self.assertNotIn("Only configured DM administrators", interaction.text)

    # Combat record -------------------------------------------------------

    def test_opening_and_closing_combat_are_refused_for_a_non_dm(self) -> None:
        run(self.command("session-start")(self.dm()))
        for value in ["start", "end"]:
            with self.subTest(action=value):
                interaction = self.bystander()
                self.combat(interaction, value)
                self.assertIn("Only configured DM administrators", interaction.text)
                self.assertTrue(interaction.kwargs["ephemeral"])
        self.assertEqual(self.encounter_statuses(), [])

    def test_combat_refuses_interactions_from_another_guild(self) -> None:
        interaction = self.elsewhere()
        self.combat(interaction, "status")
        self.assertIn("configured for a different guild", interaction.text)

    def test_starting_combat_records_it_and_hands_the_dm_the_avrae_command(self) -> None:
        run(self.command("session-start")(self.dm()))
        interaction = self.dm()
        self.combat(interaction, "start")
        self.assertIn("**COMBAT OPEN**", interaction.text)
        self.assertIn(f"<#{CHANNEL_ID}>", interaction.text)
        self.assertIn("`!i begin`", interaction.text)
        self.assertEqual(self.encounter_statuses(), ["OPEN"])

    def test_starting_combat_without_a_session_records_nothing(self) -> None:
        interaction = self.dm()
        self.combat(interaction, "start")
        self.assertIn("No active Quartermaster session", interaction.text)
        self.assertEqual(self.encounter_statuses(), [])

    def test_starting_a_second_combat_names_the_one_already_open(self) -> None:
        run(self.command("session-start")(self.dm()))
        self.combat(self.dm(), "start")
        interaction = self.dm()
        self.combat(interaction, "start")
        self.assertIn("**COMBAT ALREADY OPEN**", interaction.text)
        self.assertIn("End combat before opening another", interaction.text)
        self.assertEqual(self.encounter_statuses(), ["OPEN"])

    def test_combat_status_reports_quartermaster_state_and_names_avraes(self) -> None:
        run(self.command("session-start")(self.dm()))
        self.combat(self.dm(), "start")
        interaction = self.player()
        self.combat(interaction, "status")
        self.assertIn("**COMBAT STATUS**", interaction.text)
        self.assertIn("Session 1 is active", interaction.text)
        self.assertIn(f"Combat is open in <#{CHANNEL_ID}>", interaction.text)
        self.assertIn("Avrae holds initiative, HP, conditions", interaction.text)

    def test_combat_status_without_an_open_combat_offers_the_way_in(self) -> None:
        run(self.command("session-start")(self.dm()))
        interaction = self.player()
        self.combat(interaction, "status")
        self.assertIn("No Quartermaster combat is open", interaction.text)

    def test_combat_status_lists_outstanding_loot_for_the_session(self) -> None:
        run(self.command("session-start")(self.dm()))
        run(self.command("loot-drop")(self.dm(), item="Crown", quantity=2))
        interaction = self.player()
        self.combat(interaction, "status")
        self.assertIn("Open Loot Drops in this session:", interaction.text)
        self.assertIn("2 unclaimed across 1 entry", interaction.text)

    def test_ending_combat_closes_the_record_and_offers_the_loot_controls(self) -> None:
        run(self.command("session-start")(self.dm()))
        self.combat(self.dm(), "start")
        interaction = self.dm()
        self.combat(interaction, "end", outcome="the ogre fled")
        self.assertIn("**COMBAT CLOSED**", interaction.text)
        self.assertIn("Outcome: the ogre fled", interaction.text)
        self.assertIn("`!i end`", interaction.text)
        self.assertIn("Spoils:", interaction.text)
        self.assertEqual(self.encounter_statuses(), ["CLOSED"])
        view = interaction.kwargs["view"]
        self.assertEqual(
            {button.label for button in view.children}, {"Record spoils", "Open Loot"}
        )

    def test_ending_combat_without_one_open_offers_no_closeout_controls(self) -> None:
        run(self.command("session-start")(self.dm()))
        interaction = self.dm()
        self.combat(interaction, "end")
        self.assertIn("no open Quartermaster combat to close", interaction.text)
        self.assertIsNone(interaction.kwargs.get("view"))

    def test_ending_the_session_closes_a_combat_left_open(self) -> None:
        run(self.command("session-start")(self.dm()))
        self.combat(self.dm(), "start")
        run(self.command("session-end")(self.dm(), where_ended="The inn"))
        self.assertEqual(self.encounter_statuses(), ["CLOSED"])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT closed_reason FROM combat_encounters"
            ).fetchone()["closed_reason"],
            "SESSION_CLOSED",
        )

    # Deferred workflows --------------------------------------------------

    def test_export_defers_and_returns_a_file(self) -> None:
        run(self.command("grant")(self.dm(), item="Rope", quantity=1))
        interaction = self.dm()
        run(self.command("export")(interaction))
        self.assertTrue(interaction.response.deferred)
        content, kwargs = interaction.followup.messages[-1]
        self.assertEqual(content, "Quartermaster export")
        self.assertIsInstance(kwargs["file"], discord.File)
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM interaction_receipts WHERE response_kind = 'export'"
            ).fetchone()["status"],
            "COMMITTED",
        )

    def test_backup_reports_the_validated_snapshot_name(self) -> None:
        interaction = self.dm()
        run(self.command("backup")(interaction))
        self.assertIn("Backup completed:", interaction.text)
        self.assertIn("validation passed", interaction.text)
        snapshots = list((self.root / "backups").glob("quartermaster-*.sqlite"))
        self.assertEqual(len(snapshots), 1)
        self.assertIn(snapshots[0].name, interaction.text)

    # Launcher ------------------------------------------------------------

    def test_launcher_refuses_a_non_dm_after_deferring(self) -> None:
        interaction = self.bystander()
        run(self.command("quartermaster")(interaction))
        self.assertTrue(interaction.response.deferred)
        self.assertIn("Only configured DM administrators", interaction.text)

    def test_launcher_summarizes_state_and_offers_actions(self) -> None:
        run(self.command("grant")(self.dm(), item="Rope", quantity=1))
        run(self.command("session-start")(self.dm()))
        interaction = self.dm()
        run(self.command("quartermaster")(interaction))
        self.assertIn("Party Stash · 1 entries", interaction.text)
        self.assertIn("Session 1 active", interaction.text)
        self.assertIsInstance(interaction.kwargs["view"], QuartermasterLauncherView)

    def test_launcher_more_opens_the_admin_actions(self) -> None:
        launcher = QuartermasterLauncherView(
            self.services, self.settings, self.characters, self.currency, self.loot
        )
        more_button = next(item for item in launcher.children if item.label == "More…")
        interaction = self.dm()
        run(more_button.callback(interaction))
        self.assertIsInstance(interaction.kwargs["view"], LauncherMoreView)

    def test_launcher_health_button_reports_health(self) -> None:
        more = LauncherMoreView(
            self.services, self.settings, self.characters, self.currency, self.loot
        )
        health_button = next(item for item in more.children if item.label == "Health")
        interaction = self.dm()
        run(health_button.callback(interaction))
        self.assertIn("Quartermaster health:", interaction.text)
        self.assertIn("- database: OK", interaction.text)

    def test_launcher_actions_refuse_a_non_dm(self) -> None:
        more = LauncherMoreView(
            self.services, self.settings, self.characters, self.currency, self.loot
        )
        for item in more.children:
            with self.subTest(action=item.label):
                interaction = self.bystander()
                run(item.callback(interaction))
                self.assertIn("Only configured DM administrators", interaction.text)

    def test_every_launcher_action_works_for_a_dm(self) -> None:
        self.register_player_character()
        run(self.command("grant")(self.dm(), item="Rope", quantity=2))
        run(self.command("loot-drop")(self.dm(), item="Loot Gem", quantity=1))
        more = LauncherMoreView(
            self.services, self.settings, self.characters, self.currency, self.loot
        )
        expected = {
            "Stash": "**PARTY STASH**",
            "Open Loot": "**OPEN LOOT**",
            "Treasury": "Treasury: ",
            "Characters": "Tamsin",
            "Export": "Quartermaster export",
            "Backup": "Backup completed:",
            "Health": "Quartermaster health:",
        }
        self.assertEqual({item.label for item in more.children}, set(expected))
        for item in more.children:
            with self.subTest(action=item.label):
                interaction = self.dm()
                run(item.callback(interaction))
                self.assertIn(expected[item.label], interaction.text)
                self.assertTrue(interaction.kwargs["ephemeral"])

    def test_launcher_grant_modal_grants_the_item(self) -> None:
        modal = GrantLootModal(self.inventory, self.settings)
        modal.item_name._value = "Silvered Dagger"
        modal.quantity._value = "3"
        modal.provenance._value = "Dragon hoard"
        interaction = self.dm()
        run(modal.on_submit(interaction))
        self.assertEqual(interaction.text, "Granted 3 Silvered Dagger. Total: 3.")
        self.assertEqual(self.stash_quantities(), {"Silvered Dagger": 3})

    def test_launcher_grant_modal_rejects_a_non_numeric_quantity(self) -> None:
        modal = GrantLootModal(self.inventory, self.settings)
        modal.item_name._value = "Silvered Dagger"
        modal.quantity._value = "lots"
        interaction = self.dm()
        run(modal.on_submit(interaction))
        self.assertIn("Quantity must be a positive whole number.", interaction.text)
        self.assertEqual(self.stash_quantities(), {})

    def test_launcher_grant_modal_rejects_a_non_positive_quantity(self) -> None:
        modal = GrantLootModal(self.inventory, self.settings)
        modal.item_name._value = "Silvered Dagger"
        modal.quantity._value = "0"
        interaction = self.dm()
        run(modal.on_submit(interaction))
        self.assertIn("Quantity must be a positive whole number.", interaction.text)
        self.assertEqual(self.stash_quantities(), {})

    def test_launcher_session_button_starts_a_session(self) -> None:
        launcher = QuartermasterLauncherView(
            self.services, self.settings, self.characters, self.currency, self.loot
        )
        session_button = next(item for item in launcher.children if item.label == "Session")
        interaction = self.dm()
        run(session_button.callback(interaction))
        self.assertEqual(interaction.text, "Session 1 started.")

        again = self.dm()
        run(session_button.callback(again))
        self.assertIn("Session 1 is still active", again.text)


if __name__ == "__main__":
    unittest.main()
