"""Behavioural coverage for the Quartermaster panel surface.

There is one command. Everything else is a control on a panel, so these tests
drive the panels the way the table does: open Quartermaster, press what is on
screen, and check what the player reads and what canonical state is left
behind. Authorization is checked at the control rather than at the command,
because a view outlives the render that built it.
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

from quartermaster.avrae_handoff import AvraeHandoffService
from quartermaster.characters import CharacterService
from quartermaster.combat import CombatService
from quartermaster.config import Settings
from quartermaster.currency import CurrencyService
from quartermaster.db import SQLiteStore
from quartermaster.discord_adapter import create_bot
from quartermaster.discord_common import BotServices, Quartermaster
from quartermaster.discord_panels import (
    CombatCloseoutView,
    CombatEndModal,
    DMToolsView,
    EstateView,
    HomeView,
    LifecycleView,
    LootAdminView,
    MaintenanceView,
    SessionView,
    TreasuryGiveView,
    TreasuryView,
    open_give_coin,
    open_home,
)
from quartermaster.discord_views import (
    CharacterAddModal,
    ExpiredView,
    GiveConfirmationView,
    GiveCurrencyModal,
    GiveQuantityModal,
    GrantLootModal,
    LootDropModal,
    SessionEndModal,
    StashRemoveModal,
    TakeConfirmationView,
    TreasuryAdjustModal,
    TreasuryGiveModal,
    TreasurySplitConfirmationView,
    TreasurySplitModal,
    UseItemModal,
)
from quartermaster.handles import HandleRepository
from quartermaster.inventory import InventoryError, InventoryService
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
#: Every component interaction in these tests comes off the same ephemeral
#: message, because navigation replaces the panel in place.
SCREEN_ID = 7777

_interaction_ids = itertools.count(100_000)
_message_ids = itertools.count(700_000)


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

    async def edit_message(self, *, content: str | None = None, view: object = None) -> None:
        if self._done:
            raise RuntimeError("interaction was already acknowledged")
        self._done = True
        self.messages.append((content, {"view": view, "edited": True}))

    async def defer(self, *, ephemeral: bool = False) -> None:
        if self._done:
            raise RuntimeError("interaction was already acknowledged")
        self._done = True
        self.deferred = True

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._done = True
        self.modal = modal


class FakeMessage:
    """A message that has already been sent, and can still be edited.

    A view retires its own controls when it expires, which means editing the
    message it landed on rather than answering an interaction. Both routes
    Discord offers for that — the interaction's original response and a
    followup webhook message — end here.
    """

    def __init__(self) -> None:
        self.id = next(_message_ids)
        self.edits: list[tuple[str | None, dict]] = []

    async def edit(self, *, content: str | None = None, view: object = None) -> FakeMessage:
        self.edits.append((content, {"view": view}))
        return self


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict]] = []
        self.sent: list[FakeMessage] = []

    async def send(
        self, content: str | None = None, *, wait: bool = False, **kwargs: object
    ) -> FakeMessage | None:
        self.messages.append((content, dict(kwargs)))
        message = FakeMessage()
        self.sent.append(message)
        return message if wait else None


class FakeInteraction:
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int = GUILD_ID,
        owner_id: int = OWNER_ID,
        user: object | None = None,
        component: bool = False,
    ) -> None:
        self.id = next(_interaction_ids)
        self.guild_id = guild_id
        self.channel_id = CHANNEL_ID
        self.user = user if user is not None else SimpleNamespace(id=user_id)
        self.guild = SimpleNamespace(id=guild_id, owner_id=owner_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.client = SimpleNamespace()
        # A component interaction carries the message its control belongs to,
        # which is what lets a panel replace itself instead of stacking a new one.
        self.message = SimpleNamespace(id=SCREEN_ID) if component else None
        self.edits: list[tuple[str | None, dict]] = []

    async def edit_original_response(
        self, *, content: str | None = None, view: object = None
    ) -> FakeMessage:
        """Edit what this interaction already answered with, as Discord allows."""
        self.edits.append((content, {"view": view}))
        return FakeMessage()

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

    @property
    def view(self) -> discord.ui.View:
        view = self.kwargs.get("view")
        if view is None:
            raise AssertionError(f"the interaction produced no view: {self.text!r}")
        return view


def run(coroutine) -> object:
    return asyncio.run(coroutine)


class SurfaceTestCase(unittest.TestCase):
    """Everything a panel test needs: a live store, and a way to press things."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteStore(self.root / "quartermaster.sqlite").open()
        self.receipts = ReceiptRepository(self.store)
        self.handles = HandleRepository(self.store)
        self.inventory = InventoryService(self.store, self.receipts, self.handles)
        self.loot = LootDropService(self.store, self.receipts, self.handles)
        self.combat = CombatService(self.store, self.receipts)
        self.sessions = SessionService(self.store, self.receipts, self.loot, self.combat)
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
            combat=self.combat,
        )
        self.settings = Settings(
            guild_id=str(GUILD_ID),
            database_path=self.root / "quartermaster.sqlite",
            backup_directory=self.root / "backups",
            soft_deadline_seconds=5.0,
        )
        self.bot = create_bot(self.settings, self.services)
        self.context = self._context(self.settings)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _context(self, settings: Settings) -> Quartermaster:
        return Quartermaster(
            services=self.services,
            settings=settings,
            characters=self.characters,
            currency=self.currency,
            loot=self.loot,
            combat=self.combat,
            handoff=AvraeHandoffService(self.store),
        )

    # Callers -------------------------------------------------------------

    def dm(self, *, component: bool = False) -> FakeInteraction:
        return FakeInteraction(user_id=OWNER_ID, component=component)

    def player(self, *, component: bool = False) -> FakeInteraction:
        return FakeInteraction(user_id=PLAYER_ID, component=component)

    def bystander(self, *, component: bool = False) -> FakeInteraction:
        return FakeInteraction(user_id=BYSTANDER_ID, component=component)

    def elsewhere(self, *, component: bool = False) -> FakeInteraction:
        return FakeInteraction(
            user_id=OWNER_ID, guild_id=OTHER_GUILD_ID, owner_id=OWNER_ID, component=component
        )

    def caller(self, who: str, *, component: bool = False) -> FakeInteraction:
        return {
            "dm": self.dm,
            "player": self.player,
            "bystander": self.bystander,
            "elsewhere": self.elsewhere,
        }[who](component=component)

    # Pressing things -----------------------------------------------------

    def command(self, name: str):
        for command in self.bot.tree.get_commands(guild=discord.Object(id=GUILD_ID)):
            if command.name == name:
                return command.callback
        raise AssertionError(f"no registered command named {name}")

    def control(self, view: discord.ui.View, label: str):
        for item in view.children:
            if getattr(item, "label", None) == label:
                return item
        labels = [getattr(item, "label", None) for item in view.children]
        raise AssertionError(f"no control labelled {label!r}; the panel offers {labels}")

    def select(self, view: discord.ui.View, custom_id: str) -> discord.ui.Select:
        for item in view.children:
            if getattr(item, "custom_id", None) == custom_id:
                return item
        raise AssertionError(f"no select {custom_id!r} on this panel")

    def home(self, who: str = "player") -> FakeInteraction:
        interaction = self.caller(who)
        run(self.command("quartermaster")(interaction))
        return interaction

    def dispatch(self, view: discord.ui.View, item, interaction: FakeInteraction) -> FakeInteraction:
        """Press one control the way discord.py does: check, then call.

        `interaction_check` is where a view learns which message it is on, and
        a harness that skipped it would let an expired panel wipe the one that
        replaced it without any test noticing.
        """

        async def dispatched() -> None:
            if await view.interaction_check(interaction):
                await item.callback(interaction)

        run(dispatched())
        return interaction

    def press(self, view: discord.ui.View, label: str, who: str = "player") -> FakeInteraction:
        interaction = self.caller(who, component=True)
        return self.dispatch(view, self.control(view, label), interaction)

    def choose(
        self,
        view: discord.ui.View,
        custom_id: str,
        values: list,
        who: str = "player",
    ) -> FakeInteraction:
        select = self.select(view, custom_id)
        select._values = list(values)
        interaction = self.caller(who, component=True)
        return self.dispatch(view, select, interaction)

    def walk(self, *labels: str, who: str = "player") -> FakeInteraction:
        """Open Quartermaster and press a path of controls, as a player would."""
        interaction = self.home(who)
        for label in labels:
            interaction = self.press(interaction.view, label, who)
        return interaction

    def submit(self, modal: discord.ui.Modal, who: str = "dm", **fields: object) -> FakeInteraction:
        for name, value in fields.items():
            getattr(modal, name)._value = "" if value is None else str(value)
        interaction = self.caller(who, component=True)
        run(modal.on_submit(interaction))
        return interaction

    # Fixtures ------------------------------------------------------------

    def grant(self, item: str = "Rope", quantity: int = 1, provenance: str | None = None, who: str = "dm"):
        return self.submit(
            GrantLootModal(self.context),
            who,
            item_name=item,
            quantity=quantity,
            provenance=provenance,
        )

    def open_drop(self, item: str = "Loot Gem", quantity: int = 1, expiry_hours: object = None):
        return self.submit(
            LootDropModal(self.context),
            "dm",
            item_name=item,
            quantity=quantity,
            expiry_hours=expiry_hours,
            provenance=None,
        )

    def start_session(self) -> FakeInteraction:
        return self.press(SessionView(self.context, active=None), "Start session", "dm")

    def end_session(self, where_ended: str = "The inn") -> FakeInteraction:
        return self.submit(SessionEndModal(self.context), "dm", where_ended=where_ended)

    def register(self, name: str = "Tamsin", user_id: int | None = PLAYER_ID) -> str:
        self.submit(
            CharacterAddModal(
                self.context,
                None if user_id is None else str(user_id),
                "the player" if user_id is None else f"<@{user_id}>",
            ),
            "dm",
            name=name,
        )
        row = self.store.connection.execute("SELECT id FROM characters WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise AssertionError(f"{name} was not registered")
        return str(row["id"])

    def adjust_treasury(self, **coins: int) -> FakeInteraction:
        return self.submit(TreasuryAdjustModal(self.context), "dm", reason=None, **coins)

    def set_lifecycle(self, character_id: str, lifecycle: str) -> FakeInteraction:
        view = LifecycleView(self.context, self.characters.list_characters())
        self.choose(view, "qm:lifecycle:character", [character_id], "dm")
        self.choose(view, "qm:lifecycle:state", [lifecycle], "dm")
        return self.press(view, "Apply", "dm")

    # State ---------------------------------------------------------------

    def stash_quantities(self) -> dict[str, int]:
        return {
            row["item_name"]: row["quantity"]
            for row in self.store.connection.execute(
                "SELECT item_name, quantity FROM inventory_stacks WHERE owner_type = 'PARTY'"
            )
        }

    def held(self, character_id: str) -> dict[str, int]:
        return {
            row["item_name"]: row["quantity"]
            for row in self.store.connection.execute(
                "SELECT item_name, quantity FROM inventory_stacks WHERE owner_type = 'CHARACTER' AND owner_id = ?",
                (character_id,),
            )
        }

    def take_panel(self, who: str = "player") -> FakeInteraction:
        return self.walk("Party Stash", "Take something…", who=who)

    def take_controls(self, who: str = "player") -> list:
        return list(self.take_panel(who).view.children)


class NavigationTests(SurfaceTestCase):
    def test_the_entry_point_opens_the_home_panel(self) -> None:
        interaction = self.home("player")
        self.assertIn("**QUARTERMASTER**", interaction.text)
        self.assertIsInstance(interaction.view, HomeView)
        self.assertTrue(interaction.kwargs["ephemeral"])

    def test_home_states_what_the_caller_can_act_on(self) -> None:
        self.register()
        self.grant("Rope", 2)
        self.start_session()
        self.adjust_treasury(gp=7)
        interaction = self.home("player")
        self.assertIn("Session 1 · in progress", interaction.text)
        self.assertIn("Party Stash · 1 stack", interaction.text)
        self.assertIn("Treasury · 0 cp · 0 sp · 7 gp · 0 pp", interaction.text)
        self.assertIn("You are playing **Tamsin**, carrying nothing.", interaction.text)

    def test_home_names_the_missing_character_rather_than_refusing_later(self) -> None:
        """A player with no character can press Take and be refused, or be told now."""
        interaction = self.home("player")
        self.assertIn("no active character registered", interaction.text)

    def test_pressing_a_panel_control_replaces_the_panel_in_place(self) -> None:
        """Navigation is one panel, not a column of ephemeral messages.

        The whole point of the surface is that the player is looking at one
        thing that changes. A control that sends a new message instead leaves
        them scrolling for the panel they started from.
        """
        home = self.home("player")
        stash = self.press(home.view, "Party Stash")
        self.assertTrue(stash.kwargs.get("edited"))
        self.assertIn("**PARTY STASH**", stash.text)

    def test_the_first_panel_is_sent_because_there_is_nothing_to_replace(self) -> None:
        interaction = self.home("player")
        self.assertNotIn("edited", interaction.kwargs)
        self.assertTrue(interaction.kwargs["ephemeral"])

    def test_every_panel_offers_the_way_back(self) -> None:
        for path in [
            ("Party Stash",),
            ("Open Loot",),
            ("My Items",),
            ("Treasury",),
            ("Characters",),
            ("Combat",),
        ]:
            with self.subTest(panel=path[-1]):
                panel = self.walk(*path)
                labels = [getattr(item, "label", None) for item in panel.view.children]
                self.assertTrue(
                    any(label and label.startswith("◀") for label in labels),
                    f"{path[-1]} offers no way back: {labels}",
                )

    def test_a_player_is_not_shown_dm_controls(self) -> None:
        player_labels = {getattr(item, "label", None) for item in self.home("player").view.children}
        dm_labels = {getattr(item, "label", None) for item in self.home("dm").view.children}
        self.assertNotIn("DM Tools", player_labels)
        self.assertIn("DM Tools", dm_labels)

    def test_an_unexpected_component_failure_still_answers_the_player(self) -> None:
        """A control that raises must not leave Discord's bare failure notice.

        Component callbacks have no equivalent of `bot.tree.error`, so anything
        a callback does not name would surface as "This interaction failed" and
        the player could not tell whether their take had committed.
        """
        view = HomeView(self.context, is_dm=False)
        interaction = self.player(component=True)
        run(view.on_error(interaction, RuntimeError("database is locked"), view.children[0]))
        self.assertIn("could not complete that action", interaction.text)
        self.assertTrue(interaction.kwargs["ephemeral"])

    def test_a_modal_failure_still_answers(self) -> None:
        modal = GrantLootModal(self.context)
        interaction = self.dm(component=True)
        run(modal.on_error(interaction, RuntimeError("database is locked")))
        self.assertIn("could not record that", interaction.text)


class ExpiryTests(SurfaceTestCase):
    """What a panel does when it stops listening.

    A view has a timeout, and past it discord.py never sees the press: nothing
    acknowledges the interaction and Discord shows its bare "This interaction
    failed" — the same sentence a crash produces, which is exactly the
    ambiguity every other path in this surface exists to avoid.
    """

    def expire(self, view: discord.ui.View) -> None:
        run(view.on_timeout())

    def test_an_expired_panel_retires_its_controls_and_says_why(self) -> None:
        home = self.home("player")
        self.expire(home.view)
        content, kwargs = home.edits[-1]
        self.assertEqual(content, "This view has expired.")
        self.assertIsInstance(kwargs["view"], ExpiredView)

    def test_the_way_back_out_of_an_expired_panel_is_current_state(self) -> None:
        """Reopening renders the panel again rather than restoring the old one."""
        self.register()
        home = self.home("player")
        self.expire(home.view)
        expired = home.edits[-1][1]["view"]

        self.grant("Rope", 2)
        reopened = self.press(expired, "Open again")
        self.assertIn("**QUARTERMASTER**", reopened.text)
        self.assertIn("Party Stash · 1 stack", reopened.text)

    def test_an_expired_panel_never_wipes_the_panel_that_replaced_it(self) -> None:
        """Navigation replaces a panel in place, so several views share a message.

        Each of them times out on its own schedule long after the player moved
        on. A retirement notice written over the panel they are actually
        looking at would be a worse failure than the dead control it replaces.
        """
        home = self.home("player")
        stash = self.press(home.view, "Party Stash")
        self.expire(home.view)
        self.assertEqual(home.edits, [])

        # The panel on screen still retires itself when its own turn comes.
        self.expire(stash.view)
        self.assertEqual(stash.edits[-1][0], "This view has expired.")

    def test_an_expired_take_panel_reopens_with_handles_that_work(self) -> None:
        """Its controls were single-use, so the way back has to mint new ones."""
        character_id = self.register()
        self.grant("Rope", 2)
        panel = self.take_panel()
        spent = {item.custom_id for item in panel.view.children}

        self.expire(panel.view)
        expired = panel.edits[-1][1]["view"]
        reopened = self.press(expired, "Open again")
        renewed = {item.custom_id for item in reopened.view.children}
        self.assertNotEqual(spent, renewed)

        taken = self.press(reopened.view, "Take 1 Rope")
        self.assertEqual(taken.text, "You took 1 Rope. 1 remain.")
        self.assertEqual(self.held(character_id), {"Rope": 1})

    def test_an_expired_confirmation_names_the_command_it_has_no_control_for(self) -> None:
        """A confirmation is not a place; there is nothing to reopen it as."""
        self.register()
        self.grant("Rope", 2)
        panel = self.take_panel()
        self.grant("Rope", 5)
        stale = self.press(panel.view, "Take all Rope")
        confirmation = stale.view
        self.assertIsInstance(confirmation, TakeConfirmationView)

        self.expire(confirmation)
        content, kwargs = stale.edits[-1]
        self.assertEqual(content, "This view has expired. Run `/quartermaster` to open it again.")
        self.assertIsNone(kwargs["view"])
        self.assertEqual(self.stash_quantities(), {"Rope": 7})

    def test_a_view_that_was_never_sent_expires_quietly(self) -> None:
        """There is no message to write on, and nothing to tell anyone about."""
        self.expire(HomeView(self.context, is_dm=False))


class AuthorizationTests(SurfaceTestCase):
    def dm_controls(self) -> dict[str, tuple[discord.ui.View, str]]:
        """Every control that writes canonical state, and where it is pressed."""
        return {
            "DM Tools → Grant loot…": (DMToolsView(self.context), "Grant loot…"),
            "DM Tools → Loot Drops": (DMToolsView(self.context), "Loot Drops"),
            "DM Tools → Session": (DMToolsView(self.context), "Session"),
            "DM Tools → Correct stash…": (DMToolsView(self.context), "Correct stash…"),
            "DM Tools → Maintenance": (DMToolsView(self.context), "Maintenance"),
            "Loot admin → New drop…": (LootAdminView(self.context, []), "New drop…"),
            "Session → Start session": (SessionView(self.context, active=None), "Start session"),
            "Session → End session…": (SessionView(self.context, active=1), "End session…"),
            "Maintenance → Export": (MaintenanceView(self.context), "Export"),
            "Maintenance → Backup": (MaintenanceView(self.context), "Backup"),
            "Maintenance → Health": (MaintenanceView(self.context), "Health"),
            "Treasury → Adjust…": (TreasuryView(self.context, is_dm=True), "Adjust…"),
            "Treasury → Split…": (TreasuryView(self.context, is_dm=True), "Split…"),
            "Split preview → Split the treasury": (
                TreasurySplitConfirmationView(self.context, "any-handle", {"gp": 1}),
                "Split the treasury",
            ),
            "Treasury → Give to…": (TreasuryView(self.context, is_dm=True), "Give to…"),
            "Closeout → Record spoils": (CombatCloseoutView(self.context), "Record spoils"),
        }

    def test_every_dm_control_refuses_a_non_dm_and_changes_nothing(self) -> None:
        for name, (view, label) in self.dm_controls().items():
            with self.subTest(control=name):
                interaction = self.press(view, label, "bystander")
                self.assertIn("Only configured DM administrators", interaction.text)
                self.assertTrue(interaction.kwargs["ephemeral"])
        self.assertEqual(self.stash_quantities(), {})
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_every_dm_modal_refuses_a_non_dm_and_changes_nothing(self) -> None:
        modals = {
            "grant": (GrantLootModal(self.context), {"item_name": "Crown", "quantity": 1, "provenance": None}),
            "loot drop": (
                LootDropModal(self.context),
                {"item_name": "Crown", "quantity": 1, "expiry_hours": None, "provenance": None},
            ),
            "session end": (SessionEndModal(self.context), {"where_ended": "The inn"}),
            "character add": (CharacterAddModal(self.context, str(PLAYER_ID), "player"), {"name": "Interloper"}),
            "treasury adjust": (TreasuryAdjustModal(self.context), {"gp": 5, "reason": None}),
            "treasury split": (TreasurySplitModal(self.context), {"gp": 5}),
            "treasury give": (TreasuryGiveModal(self.context, "whoever", "Nobody"), {"gp": 5}),
            "combat end": (CombatEndModal(self.context), {"outcome": None}),
            "stash removal": (
                StashRemoveModal(self.context, {"id": "any-stack", "item_name": "Crown", "quantity": 4}),
                {"quantity": 1, "reason": None},
            ),
        }
        for name, (modal, fields) in modals.items():
            with self.subTest(modal=name):
                interaction = self.submit(modal, "bystander", **fields)
                self.assertIn("Only configured DM administrators", interaction.text)
        self.assertEqual(self.stash_quantities(), {})
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM characters").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_the_stateful_admin_panels_refuse_a_non_dm(self) -> None:
        character_id = self.register()
        self.set_lifecycle(character_id, "DEAD")
        roster = self.characters.list_characters()

        lifecycle = LifecycleView(self.context, roster)
        self.choose(lifecycle, "qm:lifecycle:character", [character_id], "bystander")
        self.choose(lifecycle, "qm:lifecycle:state", ["ACTIVE"], "bystander")
        refusal = self.press(lifecycle, "Apply", "bystander")
        self.assertIn("Only configured DM administrators", refusal.text)

        estate = EstateView(self.context, roster)
        self.choose(estate, "qm:estate:source", [character_id], "bystander")
        refusal = self.press(estate, "Resolve", "bystander")
        self.assertIn("Only configured DM administrators", refusal.text)

        self.assertEqual(
            self.store.connection.execute(
                "SELECT lifecycle FROM characters WHERE id = ?", (character_id,)
            ).fetchone()["lifecycle"],
            "DEAD",
        )

    def test_selecting_a_recipient_for_treasury_refuses_a_non_dm(self) -> None:
        character_id = self.register()
        view = TreasuryGiveView(self.context, self.characters.list_characters())
        interaction = self.choose(view, "qm:treasury:recipient", [character_id], "bystander")
        self.assertIn("Only configured DM administrators", interaction.text)
        self.assertIsNone(interaction.response.modal)

    def test_closing_a_loot_drop_refuses_a_non_dm(self) -> None:
        self.open_drop("Loot Gem", 2)
        drops = self.loot.list_open()
        view = LootAdminView(self.context, drops)
        interaction = self.choose(view, "qm:lootadmin:close", [str(drops[0]["drop_id"])], "bystander")
        self.assertIn("Only configured DM administrators", interaction.text)
        self.assertEqual(len(self.loot.list_open()), 1)

    def test_panels_refuse_interactions_from_another_guild(self) -> None:
        panels = {
            "home": open_home,
        }
        for name, panel in panels.items():
            with self.subTest(panel=name):
                interaction = self.elsewhere()
                run(panel(interaction, self.context))
                self.assertIn("configured for a different guild", interaction.text)

    def test_every_home_control_refuses_another_guild(self) -> None:
        home = HomeView(self.context, is_dm=False)
        for item in home.children:
            with self.subTest(control=item.label):
                interaction = self.elsewhere(component=True)
                run(item.callback(interaction))
                self.assertIn("configured for a different guild", interaction.text)

    def test_dm_controls_from_another_guild_refuse_with_the_authorization_message(self) -> None:
        # DM controls fold the guild and DM checks into one refusal, so the
        # wrong guild reads as "not an administrator" rather than "wrong guild".
        interaction = self.press(DMToolsView(self.context), "Grant loot…", "elsewhere")
        self.assertIn("Only configured DM administrators", interaction.text)

    def test_a_configured_dm_role_can_use_dm_controls(self) -> None:
        member = MagicMock(spec=discord.Member)
        member.id = BYSTANDER_ID
        member.guild_permissions.manage_guild = False
        member.roles = [SimpleNamespace(id=77)]
        settings = Settings(
            guild_id=str(GUILD_ID),
            database_path=self.root / "quartermaster.sqlite",
            dm_role_ids=("77",),
            soft_deadline_seconds=5.0,
        )
        context = self._context(settings)
        modal = GrantLootModal(context)
        modal.item_name._value = "Banner"
        modal.quantity._value = "1"
        modal.provenance._value = ""
        interaction = FakeInteraction(user_id=BYSTANDER_ID, user=member, component=True)
        run(modal.on_submit(interaction))
        self.assertEqual(interaction.text, "Granted 1 Banner. Total: 1.")


class PartyStashTests(SurfaceTestCase):
    def test_granting_adds_to_the_stash_and_reports_the_running_total(self) -> None:
        first = self.grant("Silvered Dagger", 2)
        self.assertEqual(first.text, "Granted 2 Silvered Dagger. Total: 2.")

        # Case and internal spacing merge into the existing stack, while the
        # reply echoes back what the DM actually typed.
        second = self.grant("silvered  dagger", 3)
        self.assertEqual(second.text, "Granted 3 silvered  dagger. Total: 5.")
        self.assertEqual(self.stash_quantities(), {"Silvered Dagger": 5})

    def test_granting_refuses_a_blank_item_name(self) -> None:
        interaction = self.grant("   ", 1)
        self.assertIn("item name is required", interaction.text)
        self.assertEqual(self.stash_quantities(), {})

    def test_granting_refuses_a_quantity_that_is_not_one(self) -> None:
        for quantity in ["lots", "0", "-3"]:
            with self.subTest(quantity=quantity):
                interaction = self.grant("Crown", quantity)
                self.assertIn("Quantity must be a positive whole number.", interaction.text)
        self.assertEqual(self.stash_quantities(), {})

    def test_the_stash_panel_renders_the_contents_and_offers_the_take_control(self) -> None:
        self.grant("Rope", 1)
        panel = self.walk("Party Stash")
        self.assertIn("**PARTY STASH**", panel.text)
        self.assertIn("• Rope x1", panel.text)
        self.control(panel.view, "Take something…")

    def test_an_empty_stash_says_so_and_offers_nothing_to_press(self) -> None:
        panel = self.walk("Party Stash")
        self.assertIn("Nothing is recorded yet.", panel.text)
        labels = [getattr(item, "label", None) for item in panel.view.children]
        self.assertNotIn("Take something…", labels)

    def test_a_stash_too_large_for_one_message_still_answers_and_says_so(self) -> None:
        """The panel must survive the campaign that fills it.

        Discord rejects content over 2000 characters outright, so an unbounded
        listing does not read badly — it fails the interaction, and the player
        sees only that Quartermaster did not respond.
        """
        for index in range(120):
            self.grant(f"Relic of the Sunken Court {index}", 1)
        panel = self.walk("Party Stash")
        self.assertLessEqual(len(panel.text), DISCORD_MESSAGE_LIMIT)
        self.assertIn("**PARTY STASH**", panel.text)
        self.assertIn("not shown here", panel.text)

    def test_taking_says_when_it_holds_only_part_of_the_stash(self) -> None:
        """A capped snapshot read as the whole stash is worse than a short one."""
        for index in range(40):
            self.grant(f"Relic {index}", 1)
        panel = self.take_panel()
        self.assertIn("Showing 25 of 40 stacks", panel.text)
        self.assertIn("The last 2 entries above have no take control here", panel.text)
        self.assertLessEqual(len(panel.text), DISCORD_MESSAGE_LIMIT)

    def test_taking_names_the_stacks_it_has_no_control_for(self) -> None:
        """A stack above one costs two controls, and a view holds twenty-five.

        Two of those are Refresh and the way back, which are not optional: a
        panel of spent handles with no way to renew them is a dead end.
        """
        self.register()
        for index in range(20):
            self.grant(f"Relic {index}", 3)
        panel = self.take_panel()
        view = panel.view
        self.assertLessEqual(len(view.children), 25)
        self.assertIn("have no take control here", panel.text)
        self.assertLessEqual(len(panel.text), DISCORD_MESSAGE_LIMIT)
        listed = [line for line in panel.text.splitlines() if line.startswith("• ")]
        uncontrolled = int(panel.text.split("The last ")[1].split(" ")[0])
        take_buttons = [item for item in view.children if str(item.label).startswith("Take ")]
        self.assertEqual(len(take_buttons), 2 * (len(listed) - uncontrolled))

    def test_the_take_panel_can_renew_itself_and_go_back(self) -> None:
        self.register()
        self.grant("Rope", 1)
        panel = self.take_panel()
        labels = [getattr(item, "label", None) for item in panel.view.children]
        self.assertEqual(labels, ["Take 1 Rope", "Refresh", "◀ Party Stash"])

        refreshed = self.press(panel.view, "Refresh")
        self.assertIn("• Rope x1", refreshed.text)
        back = self.press(refreshed.view, "◀ Party Stash")
        self.assertIn("**PARTY STASH**", back.text)

    def test_taking_transfers_the_item_to_the_players_character(self) -> None:
        character_id = self.register()
        self.grant("Rope", 2)
        panel = self.take_panel()
        self.assertEqual(
            [item.label for item in panel.view.children[:2]], ["Take 1 Rope", "Take all Rope"]
        )

        taken = self.press(panel.view, "Take 1 Rope")
        self.assertEqual(taken.text, "You took 1 Rope. 1 remain.")
        self.assertEqual(self.held(character_id), {"Rope": 1})
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_taking_without_a_registered_character_is_refused(self) -> None:
        self.grant("Rope", 1)
        panel = self.take_panel()
        refusal = self.press(panel.view, "Take 1 Rope")
        self.assertIn("active registered character is required", refusal.text)
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_a_consumed_take_handle_is_refused_on_a_second_press(self) -> None:
        self.register()
        self.grant("Rope", 2)
        panel = self.take_panel()
        self.press(panel.view, "Take 1 Rope")
        second = self.press(panel.view, "Take 1 Rope")
        self.assertIn("could not be completed", second.text)
        self.assertIn("HANDLE_CONSUMED", second.text)
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_take_all_moves_the_whole_stack(self) -> None:
        character_id = self.register()
        self.grant("Rope", 4)
        panel = self.take_panel()
        taken = self.press(panel.view, "Take all Rope")
        self.assertEqual(taken.text, "You took 4 Rope. 0 remain.")
        self.assertEqual(self.stash_quantities(), {})
        self.assertEqual(self.held(character_id), {"Rope": 4})

    def test_a_single_item_stack_offers_no_take_all_control(self) -> None:
        self.register()
        self.grant("Rope", 1)
        labels = [item.label for item in self.take_controls() if str(item.label).startswith("Take")]
        self.assertEqual(labels, ["Take 1 Rope"])

    def test_take_all_asks_for_confirmation_when_the_quantity_moved(self) -> None:
        """The point of the relative handle: 'all' must not silently mean a new number."""
        character_id = self.register()
        self.grant("Rope", 2)
        panel = self.take_panel()

        # The DM adds more rope after the player rendered the panel.
        self.grant("Rope", 5)

        stale = self.press(panel.view, "Take all Rope")
        self.assertIn("The quantity changed.", stale.text)
        confirmation = stale.view
        self.assertIsInstance(confirmation, TakeConfirmationView)
        self.assertEqual(self.stash_quantities(), {"Rope": 7})

        confirmed = self.press(confirmation, "Confirm current quantity")
        self.assertEqual(confirmed.text, "You took 7 Rope. 0 remain.")
        self.assertEqual(self.held(character_id), {"Rope": 7})

    def test_declining_the_stale_confirmation_leaves_the_stash_alone(self) -> None:
        self.register()
        self.grant("Rope", 2)
        panel = self.take_panel()
        self.grant("Rope", 5)
        self.press(panel.view, "Take all Rope")
        # Walking away from the confirmation is simply never pressing it.
        self.assertEqual(self.stash_quantities(), {"Rope": 7})


class GivingTests(SurfaceTestCase):
    def hold(self, item: str = "Rope", quantity: int = 4, who: str = "player") -> None:
        """Put a quantity into the caller's character by taking it from the stash."""
        self.grant(item, quantity)
        panel = self.take_panel(who)
        label = f"Take all {item}" if quantity > 1 else f"Take 1 {item}"
        self.press(panel.view, label, who)

    def give_panel(self, who: str = "player") -> FakeInteraction:
        items = self.walk("My Items", who=who)
        stack_id = self.select(items.view, "qm:items:pick").options[0].value
        return self.choose(items.view, "qm:items:pick", [stack_id], who)

    def test_my_items_says_so_when_there_is_no_character(self) -> None:
        panel = self.walk("My Items")
        self.assertIn("no active character registered", panel.text)

    def test_my_items_says_so_when_the_character_holds_nothing(self) -> None:
        self.register()
        panel = self.walk("My Items")
        self.assertIn("not holding anything", panel.text)

    def test_my_items_lists_what_the_character_holds(self) -> None:
        self.register()
        self.hold("Rope", 4)
        panel = self.walk("My Items")
        self.assertIn("TAMSIN'S ITEMS", panel.text)
        self.assertIn("• Rope x4", panel.text)

    def test_giving_everything_back_to_the_party(self) -> None:
        character_id = self.register()
        self.hold("Rope", 4)
        panel = self.give_panel()
        self.assertIn("You hold 4.", panel.text)

        given = self.press(panel.view, "Give all")
        self.assertEqual(given.text, "Tamsin gave 4 Rope to the Party Stash. 0 still held.")
        self.assertEqual(self.stash_quantities(), {"Rope": 4})
        self.assertEqual(self.held(character_id), {})

    def test_giving_one_leaves_the_rest_held(self) -> None:
        character_id = self.register()
        self.hold("Rope", 4)
        given = self.press(self.give_panel().view, "Give 1")
        self.assertEqual(given.text, "Tamsin gave 1 Rope to the Party Stash. 3 still held.")
        self.assertEqual(self.held(character_id), {"Rope": 3})

    def test_giving_some_takes_the_quantity_from_a_modal(self) -> None:
        character_id = self.register()
        self.hold("Rope", 4)
        panel = self.give_panel()
        opened = self.press(panel.view, "Give some…")
        modal = opened.response.modal
        self.assertIsInstance(modal, GiveQuantityModal)

        given = self.submit(modal, "player", quantity=3)
        self.assertEqual(given.text, "Tamsin gave 3 Rope to the Party Stash. 1 still held.")
        self.assertEqual(self.held(character_id), {"Rope": 1})

    def test_giving_some_refuses_a_quantity_that_is_not_one(self) -> None:
        self.register()
        self.hold("Rope", 4)
        panel = self.give_panel()
        modal = self.press(panel.view, "Give some…").response.modal
        refusal = self.submit(modal, "player", quantity="all of them")
        self.assertIn("Quantity must be a positive whole number.", refusal.text)
        self.assertEqual(self.stash_quantities(), {})

    def test_giving_to_another_character_names_the_recipient(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        recipient_id = self.register("Berrian", BYSTANDER_ID)
        self.hold("Rope", 2)
        panel = self.give_panel()

        chosen = self.choose(panel.view, "qm:give:destination", [recipient_id])
        self.assertIn("Give → Berrian.", chosen.text)

        given = self.press(panel.view, "Give all")
        self.assertEqual(given.text, "Tamsin gave 2 Rope to Berrian. 0 still held.")
        self.assertEqual(self.held(recipient_id), {"Rope": 2})
        self.assertEqual(self.stash_quantities(), {})

    def test_the_destination_list_never_offers_the_giver_themselves(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        self.register("Berrian", BYSTANDER_ID)
        self.hold("Rope", 2)
        panel = self.give_panel()
        options = {option.label for option in self.select(panel.view, "qm:give:destination").options}
        self.assertEqual(options, {"The Party Stash", "Berrian"})

    def test_a_non_active_character_cannot_be_chosen_as_a_recipient(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        berrian = self.register("Berrian", BYSTANDER_ID)
        self.hold("Rope", 2)
        self.set_lifecycle(berrian, "DEAD")
        panel = self.give_panel()
        options = {option.label for option in self.select(panel.view, "qm:give:destination").options}
        self.assertEqual(options, {"The Party Stash"})

    def test_give_all_asks_for_confirmation_when_the_holding_moved(self) -> None:
        """A button says 'all'; between rendering and pressing, 'all' can change.

        A typed give names its own number, so nothing on screen can go stale.
        The component path has exactly the take-all hazard, and the relative
        handle is what makes the difference visible instead of silent.
        """
        character_id = self.register("Tamsin", PLAYER_ID)
        berrian = self.register("Berrian", BYSTANDER_ID)
        self.hold("Rope", 2, "player")
        panel = self.give_panel("player")

        # Berrian hands Tamsin three more of the same rope after the panel rendered.
        self.grant("Rope", 3)
        berrian_panel = self.take_panel("bystander")
        self.press(berrian_panel.view, "Take all Rope", "bystander")
        berrian_give = self.give_panel("bystander")
        self.choose(berrian_give.view, "qm:give:destination", [character_id], "bystander")
        self.press(berrian_give.view, "Give all", "bystander")
        self.assertEqual(self.held(character_id), {"Rope": 5})
        self.assertEqual(self.held(berrian), {})

        stale = self.press(panel.view, "Give all", "player")
        self.assertIn("holding a different number", stale.text)
        self.assertIsInstance(stale.view, GiveConfirmationView)
        self.assertEqual(self.held(character_id), {"Rope": 5})

        confirmed = self.press(stale.view, "Confirm current quantity", "player")
        self.assertEqual(confirmed.text, "Tamsin gave 5 Rope to the Party Stash. 0 still held.")
        self.assertEqual(self.stash_quantities(), {"Rope": 5})

    def test_a_consumed_give_handle_is_refused_on_a_second_press(self) -> None:
        self.register()
        self.hold("Rope", 4)
        panel = self.give_panel()
        self.press(panel.view, "Give 1")
        second = self.press(panel.view, "Give 1")
        self.assertIn("could not be given", second.text)
        self.assertIn("HANDLE_CONSUMED", second.text)
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_a_single_held_item_offers_no_give_all_control(self) -> None:
        self.register()
        self.hold("Rope", 1)
        labels = [getattr(item, "label", None) for item in self.give_panel().view.children]
        self.assertIn("Give 1", labels)
        self.assertNotIn("Give all", labels)


class LootDropTests(SurfaceTestCase):
    def test_a_loot_drop_can_be_opened_listed_and_claimed(self) -> None:
        character_id = self.register()
        self.start_session()
        created = self.open_drop("Loot Gem", 2)
        self.assertRegex(created.text, r"^Loot Drop `[0-9a-f]{8}` created with 2 Loot Gem\.$")

        listing = self.walk("Open Loot")
        self.assertIn("**OPEN LOOT**", listing.text)
        self.assertIn("• Loot Gem x2", listing.text)

        claimed = self.press(listing.view, "Take Loot Gem")
        self.assertEqual(claimed.text, "You claimed 1 Loot Gem. 1 remain.")
        self.assertEqual(self.held(character_id), {"Loot Gem": 1})

    def test_a_loot_drop_expires_when_the_modal_says(self) -> None:
        """An empty expiry box means the default, not no expiry at all."""
        self.open_drop("Short Gem", 1, expiry_hours=1)
        self.open_drop("Default Gem", 1)
        expiries = {
            row["item_name"]: row["expires_at"]
            for row in self.store.connection.execute(
                """SELECT loot_drop_items.item_name, loot_drops.expires_at
                    FROM loot_drops JOIN loot_drop_items ON loot_drop_items.drop_id = loot_drops.id"""
            )
        }
        self.assertIsNotNone(expiries["Default Gem"])
        self.assertLess(expiries["Short Gem"], expiries["Default Gem"])

    def test_a_loot_drop_refuses_an_expiry_that_is_not_hours(self) -> None:
        interaction = self.open_drop("Loot Gem", 1, expiry_hours="soon")
        self.assertIn("Expiry must be a positive whole number of hours.", interaction.text)
        self.assertEqual(len(self.loot.list_open()), 0)

    def test_a_loot_drop_refuses_an_expiry_beyond_the_limit(self) -> None:
        interaction = self.open_drop("Loot Gem", 1, expiry_hours=721)
        self.assertIn("cannot stay open for more than 720 hours", interaction.text)
        self.assertEqual(len(self.loot.list_open()), 0)

    def test_the_loot_panel_names_items_it_has_no_claim_control_for(self) -> None:
        """One view carries a bounded number of buttons; the rest must be admitted.

        Listing an item with no control and saying nothing leaves the player
        waiting for a button that is never going to appear.
        """
        self.register()
        for index in range(30):
            self.open_drop(f"Loot Gem {index}", 1)
        listing = self.walk("Open Loot")
        self.assertLessEqual(len(listing.text), DISCORD_MESSAGE_LIMIT)
        self.assertIn("have no claim control here", listing.text)
        self.assertLessEqual(len(listing.view.children), 25)

    def test_the_loot_panel_is_explicit_when_there_is_nothing_open(self) -> None:
        listing = self.walk("Open Loot")
        self.assertIn("There are no open Loot Drops.", listing.text)

    def test_closing_a_drop_returns_the_remainder_to_the_stash(self) -> None:
        self.open_drop("Loot Gem", 3)
        admin = self.walk("DM Tools", "Loot Drops", who="dm")
        drop_id = self.select(admin.view, "qm:lootadmin:close").options[0].value

        closed = self.choose(admin.view, "qm:lootadmin:close", [drop_id], "dm")
        self.assertIn(f"Loot Drop `{drop_id[:8]}` closed.", closed.text)
        self.assertEqual(self.stash_quantities(), {"Loot Gem": 3})

    def test_the_close_control_describes_what_is_still_unclaimed(self) -> None:
        self.open_drop("Loot Gem", 3)
        admin = self.walk("DM Tools", "Loot Drops", who="dm")
        option = self.select(admin.view, "qm:lootadmin:close").options[0]
        self.assertEqual(option.description, "3 unclaimed across 1 entry")

    def test_closing_a_drop_that_is_already_gone_reports_the_failure(self) -> None:
        self.open_drop("Loot Gem", 1)
        admin = self.walk("DM Tools", "Loot Drops", who="dm")
        interaction = self.choose(admin.view, "qm:lootadmin:close", ["not-a-drop"], "dm")
        self.assertIn("loot drop not found", interaction.text)

    def test_the_admin_panel_offers_no_close_control_with_nothing_open(self) -> None:
        admin = self.walk("DM Tools", "Loot Drops", who="dm")
        self.assertIn("There are no open Loot Drops.", admin.text)
        with self.assertRaises(AssertionError):
            self.select(admin.view, "qm:lootadmin:close")


class SessionTests(SurfaceTestCase):
    def test_a_session_starts_and_ends_from_the_panel(self) -> None:
        started = self.start_session()
        self.assertEqual(started.text, "Session 1 started.")

        ended = self.end_session("The Sunken Tomb")
        self.assertEqual(ended.text, "Session 1 closed.")
        self.assertEqual(
            self.store.connection.execute("SELECT where_ended FROM sessions").fetchone()["where_ended"],
            "The Sunken Tomb",
        )

    def test_the_session_panel_offers_only_the_action_that_applies(self) -> None:
        panel = self.walk("DM Tools", "Session", who="dm")
        self.assertIn("No session is in progress.", panel.text)
        self.assertEqual(
            [getattr(item, "label", None) for item in panel.view.children],
            ["Start session", "Refresh", "◀ DM Tools"],
        )

        self.start_session()
        panel = self.walk("DM Tools", "Session", who="dm")
        self.assertIn("Session 1 is in progress.", panel.text)
        self.assertEqual(
            [getattr(item, "label", None) for item in panel.view.children],
            ["End session…", "Refresh", "◀ DM Tools"],
        )

    def test_starting_a_second_session_points_at_the_open_one(self) -> None:
        self.start_session()
        second = self.start_session()
        self.assertIn("Session 1 is still active", second.text)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_ending_with_no_active_session_says_so(self) -> None:
        self.assertEqual(self.end_session("Nowhere").text, "There is no active session.")


class TreasuryTests(SurfaceTestCase):
    def test_the_treasury_can_be_adjusted_viewed_split_and_given(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        self.register("Beta", BYSTANDER_ID)

        adjusted = self.adjust_treasury(gp=101)
        self.assertEqual(adjusted.text, "Treasury updated: 0 cp · 0 sp · 101 gp · 0 pp.")

        panel = self.walk("Treasury")
        self.assertIn("0 cp · 0 sp · 101 gp · 0 pp", panel.text)

        preview = self.submit(TreasurySplitModal(self.context), "dm", cp=0, sp=0, gp=101, pp=0)
        # The preview names who is being paid, and says plainly that it has not paid them.
        self.assertIn("Among 2 active characters: Alpha, Beta", preview.text)
        self.assertIn("Each receives 0 cp · 0 sp · 50 gp · 0 pp", preview.text)
        self.assertIn("Nothing has moved yet.", preview.text)
        self.assertEqual(self.currency.view_treasury()["gp"], 101)

        split = self.press(preview.view, "Split the treasury", "dm")
        self.assertIn("Split among 2 active characters", split.text)
        self.assertIn("50 gp", split.text)
        # Specification 33.1: the indivisible remainder stays with the treasury.
        self.assertEqual(
            self.store.connection.execute(
                "SELECT gp FROM currency_balances WHERE owner_type = 'PARTY'"
            ).fetchone()["gp"],
            1,
        )

        give_panel = self.walk("Treasury", "Give to…", who="dm")
        opened = self.choose(give_panel.view, "qm:treasury:recipient", [alpha], "dm")
        modal = opened.response.modal
        self.assertIsInstance(modal, TreasuryGiveModal)
        given = self.submit(modal, "dm", cp=0, sp=0, gp=1, pp=0)
        self.assertEqual(given.text, "Gave 0 cp · 0 sp · 1 gp · 0 pp to Alpha.")
        self.assertEqual(
            self.store.connection.execute(
                "SELECT gp FROM currency_balances WHERE owner_type = 'PARTY'"
            ).fetchone()["gp"],
            0,
        )

    def test_an_empty_coin_box_means_none_of_that_coin(self) -> None:
        interaction = self.submit(TreasuryAdjustModal(self.context), "dm", cp=None, sp=None, gp=5, pp=None, reason=None)
        self.assertEqual(interaction.text, "Treasury updated: 0 cp · 0 sp · 5 gp · 0 pp.")

    def test_a_coin_box_that_is_not_a_number_is_refused(self) -> None:
        interaction = self.submit(
            TreasuryAdjustModal(self.context), "dm", cp=None, sp=None, gp="lots", pp=None, reason=None
        )
        self.assertIn("gp must be a whole number of coins.", interaction.text)
        self.assertEqual(self.currency.view_treasury()["gp"], 0)

    def test_a_treasury_adjustment_cannot_drive_a_balance_negative(self) -> None:
        self.assertIn("cannot become negative", self.adjust_treasury(gp=-5).text)

    def test_giving_more_than_the_treasury_holds_is_refused(self) -> None:
        character_id = self.register("Alpha", PLAYER_ID)
        interaction = self.submit(
            TreasuryGiveModal(self.context, character_id, "Alpha"), "dm", cp=0, sp=0, gp=10, pp=0
        )
        self.assertIn("does not contain enough currency", interaction.text)

    def test_giving_to_a_non_active_character_is_refused(self) -> None:
        character_id = self.register("Alpha", PLAYER_ID)
        self.adjust_treasury(gp=10)
        self.set_lifecycle(character_id, "DEAD")
        interaction = self.submit(
            TreasuryGiveModal(self.context, character_id, "Alpha"), "dm", cp=0, sp=0, gp=5, pp=0
        )
        self.assertIn("only active characters can receive currency", interaction.text)

    def test_the_recipient_list_holds_only_active_characters(self) -> None:
        self.register("Alpha", PLAYER_ID)
        beta = self.register("Beta", BYSTANDER_ID)
        self.set_lifecycle(beta, "RETIRED")
        panel = self.walk("Treasury", "Give to…", who="dm")
        self.assertEqual({option.label for option in self.select(panel.view, "qm:treasury:recipient").options}, {"Alpha"})

    def test_giving_with_no_active_character_says_so_and_offers_nothing(self) -> None:
        panel = self.walk("Treasury", "Give to…", who="dm")
        self.assertIn("No active character can receive currency yet.", panel.text)
        with self.assertRaises(AssertionError):
            self.select(panel.view, "qm:treasury:recipient")

    def test_splitting_with_no_active_characters_is_refused(self) -> None:
        self.adjust_treasury(gp=10)
        interaction = self.submit(TreasurySplitModal(self.context), "dm", cp=0, sp=0, gp=10, pp=0)
        self.assertIn("at least one active character is required", interaction.text)

    def test_a_death_between_the_preview_and_the_split_asks_again_with_the_new_share(self) -> None:
        """The share is a function of the roster, so the roster is part of the question.

        The DM sees who is being paid and what each of them gets. If somebody
        dies before the button is pressed, the shares the DM agreed to no longer
        exist — so the split refuses, states the new ones, and asks again.
        """
        self.register("Alpha", PLAYER_ID)
        beta = self.register("Beta", BYSTANDER_ID)
        self.adjust_treasury(gp=100)

        preview = self.submit(TreasurySplitModal(self.context), "dm", cp=0, sp=0, gp=100, pp=0)
        self.assertIn("Each receives 0 cp · 0 sp · 50 gp · 0 pp", preview.text)

        self.set_lifecycle(beta, "DEAD")

        stale = self.press(preview.view, "Split the treasury", "dm")
        self.assertIn("changed since that preview", stale.text)
        self.assertIn("Among 1 active character: Alpha", stale.text)
        self.assertIn("Each receives 0 cp · 0 sp · 100 gp · 0 pp", stale.text)
        # Nothing moved on the refusal: the treasury is still whole.
        self.assertEqual(self.currency.view_treasury()["gp"], 100)

        confirmed = self.press(stale.view, "Split the treasury", "dm")
        self.assertIn("Split among 1 active character:", confirmed.text)
        self.assertEqual(self.currency.view_treasury()["gp"], 0)
        balances = {
            row["owner_id"]: row["gp"]
            for row in self.store.connection.execute(
                "SELECT owner_id, gp FROM currency_balances WHERE owner_type = 'CHARACTER'"
            )
        }
        self.assertEqual(balances.get(beta, 0), 0)

    def test_a_split_preview_can_be_left_unconfirmed_and_nothing_happens(self) -> None:
        self.register("Alpha", PLAYER_ID)
        self.adjust_treasury(gp=40)
        preview = self.submit(TreasurySplitModal(self.context), "dm", cp=0, sp=0, gp=40, pp=0)
        self.assertIn("Nothing has moved yet.", preview.text)
        self.assertEqual(self.currency.view_treasury()["gp"], 40)

    def test_a_split_cannot_be_confirmed_twice(self) -> None:
        self.register("Alpha", PLAYER_ID)
        self.adjust_treasury(gp=40)
        preview = self.submit(TreasurySplitModal(self.context), "dm", cp=0, sp=0, gp=40, pp=0)
        self.press(preview.view, "Split the treasury", "dm")
        again = self.press(preview.view, "Split the treasury", "dm")
        self.assertIn("HANDLE_CONSUMED", again.text)
        self.assertEqual(self.currency.view_treasury()["gp"], 0)

    def test_splitting_more_than_the_treasury_holds_is_refused_before_the_preview(self) -> None:
        self.register("Alpha", PLAYER_ID)
        self.adjust_treasury(gp=5)
        interaction = self.submit(TreasurySplitModal(self.context), "dm", cp=0, sp=0, gp=10, pp=0)
        self.assertIn("does not contain enough currency", interaction.text)

    def test_a_player_sees_the_treasury_but_no_controls_over_it(self) -> None:
        self.adjust_treasury(gp=3)
        panel = self.walk("Treasury")
        self.assertIn("0 cp · 0 sp · 3 gp · 0 pp", panel.text)
        labels = {getattr(item, "label", None) for item in panel.view.children}
        self.assertEqual(labels, {"Refresh", "◀ Home"})


class CoinTests(SurfaceTestCase):
    """A player's own coin: that they can see it, and that it can move.

    Currency used to travel one way only. A split and **Give to…** credit a
    living character and nothing debits one — belongings resolution refuses an
    active character on purpose — so a mistyped give was permanent, and the
    balance it created appeared nowhere on the surface. These drive the way
    back, which is the coin counterpart of My Items.
    """

    def purse(self, character_id: str) -> dict[str, int]:
        row = self.store.connection.execute(
            "SELECT cp, sp, gp, pp FROM currency_balances WHERE owner_type = 'CHARACTER' AND owner_id = ?",
            (character_id,),
        ).fetchone()
        return {coin: 0 for coin in ("cp", "sp", "gp", "pp")} if row is None else dict(row)

    def fund(self, character_id: str, name: str, **coins: int) -> None:
        self.adjust_treasury(**coins)
        self.submit(
            TreasuryGiveModal(self.context, character_id, name),
            "dm",
            **{coin: coins.get(coin, 0) for coin in ("cp", "sp", "gp", "pp")},
        )

    def give_panel(self, who: str = "player"):
        return self.walk("Treasury", "My coin…", who=who)

    def test_the_treasury_panel_names_the_players_own_coin(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        self.fund(alpha, "Alpha", gp=90)
        panel = self.walk("Treasury")
        self.assertIn("Alpha is carrying 0 cp · 0 sp · 90 gp · 0 pp.", panel.text)
        self.assertIn("My coin…", {getattr(item, "label", None) for item in panel.view.children})

    def test_home_names_the_players_own_coin(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        self.fund(alpha, "Alpha", sp=4)
        self.assertIn("Your coin · 0 cp · 4 sp · 0 gp · 0 pp", self.home().text)

    def test_a_player_carrying_nothing_is_offered_no_coin_control(self) -> None:
        self.register("Alpha", PLAYER_ID)
        panel = self.walk("Treasury")
        self.assertNotIn("is carrying", panel.text)
        self.assertNotIn("My coin…", {getattr(item, "label", None) for item in panel.view.children})
        self.assertNotIn("Your coin", self.home().text)

    def test_a_player_sends_coin_back_to_the_treasury(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        self.fund(alpha, "Alpha", gp=90)
        self.assertEqual(self.currency.view_treasury()["gp"], 0)

        panel = self.give_panel()
        self.assertIn("Alpha is carrying 0 cp · 0 sp · 90 gp · 0 pp.", panel.text)
        self.assertIn("Going to the treasury.", panel.text)

        opened = self.press(panel.view, "Give coin…")
        modal = opened.response.modal
        self.assertIsInstance(modal, GiveCurrencyModal)
        given = self.submit(modal, "player", cp=0, sp=0, gp=81, pp=0)
        self.assertEqual(
            given.text,
            "Alpha gave 0 cp · 0 sp · 81 gp · 0 pp to the treasury."
            " Still carrying 0 cp · 0 sp · 9 gp · 0 pp.",
        )
        self.assertEqual(self.currency.view_treasury()["gp"], 81)
        self.assertEqual(self.purse(alpha)["gp"], 9)

    def test_a_player_hands_coin_to_another_character(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        beta = self.register("Beta", BYSTANDER_ID)
        self.fund(alpha, "Alpha", sp=40)

        panel = self.give_panel()
        chosen = self.choose(panel.view, "qm:coin:destination", [beta])
        self.assertIn("Going to Beta.", chosen.text)

        opened = self.press(panel.view, "Give coin…")
        given = self.submit(opened.response.modal, "player", cp=0, sp=15, gp=0, pp=0)
        self.assertIn("Alpha gave 0 cp · 15 sp · 0 gp · 0 pp to Beta.", given.text)
        self.assertEqual(self.purse(alpha)["sp"], 25)
        self.assertEqual(self.purse(beta)["sp"], 15)
        # The pinned surface renders the treasury, which this transfer never touched.
        self.assertEqual(self.currency.view_treasury()["sp"], 0)

    def test_the_coin_destination_list_never_offers_the_giver_themselves(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        beta = self.register("Beta", BYSTANDER_ID)
        self.set_lifecycle(self.register("Gamma", None), "RETIRED")
        self.fund(alpha, "Alpha", gp=5)
        options = self.select(self.give_panel().view, "qm:coin:destination").options
        self.assertEqual({option.value for option in options}, {"party", beta})

    def test_giving_more_coin_than_the_character_carries_is_refused(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        self.fund(alpha, "Alpha", gp=5)
        opened = self.press(self.give_panel().view, "Give coin…")
        refused = self.submit(opened.response.modal, "player", cp=0, sp=0, gp=6, pp=0)
        self.assertIn("Alpha is carrying only 0 cp · 0 sp · 5 gp · 0 pp", refused.text)
        self.assertEqual(self.purse(alpha)["gp"], 5)
        self.assertEqual(self.currency.view_treasury()["gp"], 0)

    def test_a_coin_box_that_is_not_a_number_is_refused(self) -> None:
        alpha = self.register("Alpha", PLAYER_ID)
        self.fund(alpha, "Alpha", gp=5)
        opened = self.press(self.give_panel().view, "Give coin…")
        refused = self.submit(opened.response.modal, "player", cp=0, sp=0, gp="most of it", pp=0)
        self.assertIn("gp must be a whole number of coins.", refused.text)
        self.assertEqual(self.purse(alpha)["gp"], 5)

    def test_the_coin_panel_says_so_when_there_is_no_character(self) -> None:
        """The Treasury panel hides the control, but a stale view outlives it."""
        interaction = self.player(component=True)
        run(open_give_coin(interaction, self.context))
        self.assertIn("no active character registered", interaction.text)


class CharacterTests(SurfaceTestCase):
    def test_a_character_is_registered_by_picking_the_player(self) -> None:
        panel = self.walk("Characters", "Register…", who="dm")
        select = self.select(panel.view, "qm:characters:player")
        select._values = [SimpleNamespace(id=PLAYER_ID)]
        opened = self.dm(component=True)
        run(select.callback(opened))
        modal = opened.response.modal
        self.assertIsInstance(modal, CharacterAddModal)

        registered = self.submit(modal, "dm", name="Tamsin")
        self.assertEqual(registered.text, f"Registered Tamsin to <@{PLAYER_ID}>.")
        row = self.store.connection.execute("SELECT discord_user_id FROM characters").fetchone()
        self.assertEqual(row["discord_user_id"], str(PLAYER_ID))

    def test_a_character_can_be_registered_with_no_discord_player(self) -> None:
        panel = self.walk("Characters", "Register…", who="dm")
        opened = self.press(panel.view, "No Discord player", "dm")
        registered = self.submit(opened.response.modal, "dm", name="The Innkeeper")
        self.assertEqual(registered.text, "Registered The Innkeeper to no Discord player.")
        self.assertIsNone(
            self.store.connection.execute("SELECT discord_user_id FROM characters").fetchone()["discord_user_id"]
        )

    def test_the_roster_lists_registered_characters(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        panel = self.walk("Characters")
        self.assertIn("Tamsin", panel.text)
        self.assertIn("ACTIVE", panel.text)

    def test_a_roster_too_large_for_one_message_still_answers(self) -> None:
        for index in range(60):
            self.register(f"Adventurer of the Long Road {index}", None)
        panel = self.walk("Characters")
        self.assertLessEqual(len(panel.text), DISCORD_MESSAGE_LIMIT)
        self.assertIn("not shown here", panel.text)

    def test_registering_a_second_active_character_for_one_player_is_refused(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        refusal = self.submit(CharacterAddModal(self.context, str(PLAYER_ID), "player"), "dm", name="Rowan")
        self.assertIn("Tamsin is already active", refusal.text)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM characters").fetchone()[0], 1)

    def test_a_lifecycle_change_names_both_ends(self) -> None:
        character_id = self.register("Tamsin", PLAYER_ID)
        self.assertEqual(self.set_lifecycle(character_id, "RETIRED").text, "Tamsin moved from ACTIVE to RETIRED.")

    def test_the_lifecycle_panel_states_the_change_before_it_is_applied(self) -> None:
        character_id = self.register("Tamsin", PLAYER_ID)
        panel = self.walk("Characters", "Lifecycle…", who="dm")
        chosen = self.choose(panel.view, "qm:lifecycle:character", [character_id], "dm")
        self.assertIn("Tamsin → choose a state", chosen.text)
        staged = self.choose(panel.view, "qm:lifecycle:state", ["DEAD"], "dm")
        self.assertIn("Tamsin → DEAD", staged.text)
        self.assertEqual(
            self.store.connection.execute("SELECT lifecycle FROM characters").fetchone()["lifecycle"], "ACTIVE"
        )

    def test_applying_a_lifecycle_with_nothing_chosen_asks_for_the_choice(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        panel = self.walk("Characters", "Lifecycle…", who="dm")
        refusal = self.press(panel.view, "Apply", "dm")
        self.assertIn("Choose a character and the state", refusal.text)

    def test_an_impossible_lifecycle_transition_is_refused(self) -> None:
        character_id = self.register("Tamsin", PLAYER_ID)
        self.set_lifecycle(character_id, "DEAD")
        refusal = self.set_lifecycle(character_id, "RETIRED")
        self.assertIn("cannot transition DEAD to RETIRED", refusal.text)

    def test_a_roster_larger_than_one_select_says_what_it_is_showing(self) -> None:
        """A message that runs long says so; a select just stops.

        Twenty-six registered characters is an ordinary campaign after a few
        deaths, and a DM looking for a name the panel silently dropped has no
        way to tell that is what happened.
        """
        for index in range(26):
            self.register(f"Adventurer {index}", None)
        panel = self.walk("Characters", "Lifecycle…", who="dm")
        self.assertIn("Showing the first 25 of 26 characters.", panel.text)
        self.assertEqual(len(self.select(panel.view, "qm:lifecycle:character").options), 25)

    def test_the_lifecycle_panel_says_when_there_is_no_roster(self) -> None:
        panel = self.walk("Characters", "Lifecycle…", who="dm")
        self.assertIn("No characters are registered yet.", panel.text)
        with self.assertRaises(AssertionError):
            self.select(panel.view, "qm:lifecycle:character")

    def test_resolving_belongings_moves_them_to_the_party_stash(self) -> None:
        character_id = self.register("Tamsin", PLAYER_ID)
        self.grant("Rope", 1)
        take = self.take_panel()
        self.press(take.view, "Take 1 Rope")
        self.set_lifecycle(character_id, "DEAD")

        panel = self.walk("Characters", "Resolve estate…", who="dm")
        staged = self.choose(panel.view, "qm:estate:source", [character_id], "dm")
        self.assertIn("goes to the Party Stash", staged.text)
        resolved = self.press(panel.view, "Resolve", "dm")
        self.assertIn("Resolved 1 item stacks", resolved.text)
        self.assertIn("to Party Stash", resolved.text)
        self.assertEqual(self.stash_quantities(), {"Rope": 1})

    def test_an_estate_can_be_left_to_another_character(self) -> None:
        tamsin = self.register("Tamsin", PLAYER_ID)
        berrian = self.register("Berrian", BYSTANDER_ID)
        self.grant("Rope", 1)
        self.press(self.take_panel().view, "Take 1 Rope")
        self.set_lifecycle(tamsin, "DEAD")

        panel = self.walk("Characters", "Resolve estate…", who="dm")
        self.choose(panel.view, "qm:estate:source", [tamsin], "dm")
        staged = self.choose(panel.view, "qm:estate:destination", [berrian], "dm")
        self.assertIn("goes to Berrian", staged.text)
        self.press(panel.view, "Resolve", "dm")
        self.assertEqual(self.held(berrian), {"Rope": 1})

    def test_an_active_character_is_never_offered_as_an_estate(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        panel = self.walk("Characters", "Resolve estate…", who="dm")
        self.assertIn("no estate to resolve", panel.text)
        with self.assertRaises(AssertionError):
            self.select(panel.view, "qm:estate:source")

    def test_a_player_sees_the_roster_but_no_controls_over_it(self) -> None:
        self.register("Tamsin", PLAYER_ID)
        labels = {getattr(item, "label", None) for item in self.walk("Characters").view.children}
        self.assertEqual(labels, {"Refresh", "◀ Home"})


class CombatTests(SurfaceTestCase):
    def encounter_statuses(self) -> list[str]:
        return [
            row["status"]
            for row in self.store.connection.execute("SELECT status FROM combat_encounters ORDER BY opened_at")
        ]

    def test_the_combat_panel_needs_an_active_session(self) -> None:
        panel = self.walk("Combat")
        self.assertIn("No active Quartermaster session", panel.text)

    def test_the_handoff_controls_render_the_native_avrae_command(self) -> None:
        self.start_session()
        panel = self.walk("Combat")
        joined = self.press(panel.view, "Join")
        self.assertIn("**AVRAE HANDOFF · JOIN**", joined.text)
        self.assertIn("`!i join`", joined.text)
        self.assertIn(f"<#{CHANNEL_ID}>", joined.text)
        self.assertIn("Avrae remains authoritative", joined.text)

    def test_the_handoff_controls_stay_open_to_players(self) -> None:
        self.start_session()
        panel = self.walk("Combat")
        for label in ["Join", "Next turn", "Attack", "Cast", "Check", "Save"]:
            with self.subTest(action=label):
                interaction = self.press(panel.view, label)
                self.assertNotIn("Only configured DM administrators", interaction.text)

    def test_a_player_is_not_offered_the_combat_record_controls(self) -> None:
        self.start_session()
        labels = {getattr(item, "label", None) for item in self.walk("Combat").view.children}
        self.assertNotIn("Start combat", labels)
        self.assertNotIn("End combat…", labels)

    def test_opening_combat_records_it_and_hands_the_dm_the_avrae_command(self) -> None:
        self.start_session()
        panel = self.walk("Combat", who="dm")
        opened = self.press(panel.view, "Start combat", "dm")
        self.assertIn("**COMBAT OPEN**", opened.text)
        self.assertIn(f"<#{CHANNEL_ID}>", opened.text)
        self.assertIn("`!i begin`", opened.text)
        self.assertEqual(self.encounter_statuses(), ["OPEN"])

    def test_opening_combat_is_refused_for_a_non_dm(self) -> None:
        self.start_session()
        view = self.walk("Combat", who="dm").view
        for label in ["Start combat", "End combat…"]:
            with self.subTest(control=label):
                refusal = self.press(view, label, "bystander")
                self.assertIn("Only configured DM administrators", refusal.text)
        self.assertEqual(self.encounter_statuses(), [])

    def test_opening_combat_without_a_session_records_nothing(self) -> None:
        panel = self.walk("Combat", who="dm")
        opened = self.press(panel.view, "Start combat", "dm")
        self.assertIn("No active Quartermaster session", opened.text)
        self.assertEqual(self.encounter_statuses(), [])

    def test_opening_a_second_combat_names_the_one_already_open(self) -> None:
        self.start_session()
        panel = self.walk("Combat", who="dm")
        self.press(panel.view, "Start combat", "dm")
        again = self.press(panel.view, "Start combat", "dm")
        self.assertIn("**COMBAT ALREADY OPEN**", again.text)
        self.assertIn("End combat", again.text)
        self.assertEqual(self.encounter_statuses(), ["OPEN"])

    def test_the_combat_panel_reports_quartermaster_state_and_names_avraes(self) -> None:
        self.start_session()
        self.press(self.walk("Combat", who="dm").view, "Start combat", "dm")
        panel = self.walk("Combat")
        self.assertIn("**COMBAT STATUS**", panel.text)
        self.assertIn("Session 1 is active", panel.text)
        self.assertIn(f"Combat is open in <#{CHANNEL_ID}>", panel.text)
        self.assertIn("Avrae holds initiative, HP, conditions", panel.text)

    def test_the_combat_panel_lists_outstanding_loot_for_the_session(self) -> None:
        self.start_session()
        self.open_drop("Crown", 2)
        panel = self.walk("Combat")
        self.assertIn("Open Loot Drops in this session:", panel.text)
        self.assertIn("2 unclaimed across 1 entry", panel.text)

    def test_ending_combat_closes_the_record_and_offers_the_loot_controls(self) -> None:
        self.start_session()
        panel = self.walk("Combat", who="dm")
        self.press(panel.view, "Start combat", "dm")
        opened = self.press(panel.view, "End combat…", "dm")
        modal = opened.response.modal
        self.assertIsInstance(modal, CombatEndModal)

        closed = self.submit(modal, "dm", outcome="the ogre fled")
        self.assertIn("**COMBAT CLOSED**", closed.text)
        self.assertIn("Outcome: the ogre fled", closed.text)
        self.assertIn("`!i end`", closed.text)
        self.assertEqual(self.encounter_statuses(), ["CLOSED"])
        self.assertIsInstance(closed.view, CombatCloseoutView)
        self.assertEqual(
            {getattr(item, "label", None) for item in closed.view.children},
            {"Record spoils", "Open Loot", "◀ Home"},
        )

    def test_recording_spoils_from_the_closeout_grants_to_the_stash(self) -> None:
        closeout = CombatCloseoutView(self.context)
        opened = self.press(closeout, "Record spoils", "dm")
        granted = self.submit(opened.response.modal, "dm", item_name="Ogre Tooth", quantity=2, provenance=None)
        self.assertEqual(granted.text, "Granted 2 Ogre Tooth. Total: 2.")
        self.assertEqual(self.stash_quantities(), {"Ogre Tooth": 2})

    def test_ending_combat_without_one_open_offers_no_closeout_controls(self) -> None:
        self.start_session()
        closed = self.submit(CombatEndModal(self.context), "dm", outcome=None)
        self.assertIn("no open Quartermaster combat to close", closed.text)
        self.assertIsNone(closed.kwargs.get("view"))

    def test_ending_the_session_closes_a_combat_left_open(self) -> None:
        self.start_session()
        self.press(self.walk("Combat", who="dm").view, "Start combat", "dm")
        self.end_session("The inn")
        self.assertEqual(self.encounter_statuses(), ["CLOSED"])
        self.assertEqual(
            self.store.connection.execute("SELECT closed_reason FROM combat_encounters").fetchone()["closed_reason"],
            "SESSION_CLOSED",
        )


class MaintenanceTests(SurfaceTestCase):
    def test_export_defers_and_returns_a_file(self) -> None:
        self.grant("Rope", 1)
        interaction = self.press(MaintenanceView(self.context), "Export", "dm")
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
        interaction = self.press(MaintenanceView(self.context), "Backup", "dm")
        self.assertIn("Backup completed:", interaction.text)
        self.assertIn("validation passed", interaction.text)
        snapshots = list((self.root / "backups").glob("quartermaster-*.sqlite"))
        self.assertEqual(len(snapshots), 1)
        self.assertIn(snapshots[0].name, interaction.text)

    def test_health_reports_what_the_runtime_can_see(self) -> None:
        interaction = self.press(MaintenanceView(self.context), "Health", "dm")
        self.assertIn("Quartermaster health:", interaction.text)
        self.assertIn("- database: OK", interaction.text)

    def test_every_dm_tool_reaches_its_panel(self) -> None:
        self.register()
        self.grant("Rope", 2)
        self.open_drop("Loot Gem", 1)
        expected = {
            "Loot Drops": "**OPEN LOOT**",
            "Session": "**SESSION**",
            "Correct stash…": "**CORRECT THE PARTY STASH**",
            "Maintenance": "**MAINTENANCE**",
        }
        tools = self.walk("DM Tools", who="dm")
        for label, marker in expected.items():
            with self.subTest(tool=label):
                self.assertIn(marker, self.press(tools.view, label, "dm").text)


class UsingUpTests(SurfaceTestCase):
    """The way out of the campaign, from both ends of possession.

    Until these controls existed every item path was a mint or a transfer, so
    a drunk potion stayed in the stash for the length of the campaign and a
    mistyped grant of fifty could not be taken back by anything.
    """

    def hold(self, item: str = "Potion of Healing", quantity: int = 3, who: str = "player") -> None:
        self.grant(item, quantity)
        panel = self.take_panel(who)
        self.press(panel.view, f"Take all {item}" if quantity > 1 else f"Take 1 {item}", who)

    def item_panel(self, who: str = "player") -> FakeInteraction:
        items = self.walk("My Items", who=who)
        stack_id = self.select(items.view, "qm:items:pick").options[0].value
        return self.choose(items.view, "qm:items:pick", [stack_id], who)

    def correction_panel(self) -> FakeInteraction:
        return self.walk("DM Tools", "Correct stash…", who="dm")

    def test_a_player_uses_up_what_they_are_carrying(self) -> None:
        character_id = self.register()
        self.hold("Potion of Healing", 3)
        panel = self.item_panel()
        self.assertIn("Use → gone from the campaign", panel.text)

        opened = self.press(panel.view, "Use…")
        modal = opened.response.modal
        self.assertIsInstance(modal, UseItemModal)
        used = self.submit(modal, "player", quantity=2, reason="Drunk in the tomb")
        self.assertEqual(used.text, "You used 2 Potion of Healing. 1 still held.")
        self.assertEqual(self.held(character_id), {"Potion of Healing": 1})
        # It went nowhere: this is the one path that is not a transfer.
        self.assertEqual(self.stash_quantities(), {})
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM ledger_entries WHERE event_type = 'ITEM_CONSUMED'"
            ).fetchone()[0],
            1,
        )

    def test_using_more_than_is_held_is_refused_with_the_real_number(self) -> None:
        character_id = self.register()
        self.hold("Rope", 2)
        modal = self.press(self.item_panel().view, "Use…").response.modal
        refusal = self.submit(modal, "player", quantity=5, reason=None)
        self.assertIn("holds only 2", refusal.text)
        self.assertEqual(self.held(character_id), {"Rope": 2})

    def test_the_dm_removes_a_mistyped_grant_from_the_party_stash(self) -> None:
        self.grant("Trail Ration", 50)
        panel = self.correction_panel()
        self.assertIn("• Trail Ration x50", panel.text)
        self.assertIn("hands nothing back to anyone", panel.text)

        stack_id = self.select(panel.view, "qm:correct:pick").options[0].value
        chosen = self.choose(panel.view, "qm:correct:pick", [stack_id], "dm")
        modal = chosen.response.modal
        self.assertIsInstance(modal, StashRemoveModal)
        removed = self.submit(modal, "dm", quantity=45, reason="Meant five")
        self.assertIn("Removed 45 Trail Ration from the Party Stash. 5 remain.", removed.text)
        self.assertEqual(self.stash_quantities(), {"Trail Ration": 5})

    def test_a_player_cannot_remove_from_the_shared_stash(self) -> None:
        """The gate is on the control, and the domain refuses without it.

        A view outlives the render that built it, so the control checks again
        when it is pressed — and the mutation refuses a party-owned stack
        unless that check passed, because this is the one operation with no
        way back.
        """
        self.grant("Rope", 4)
        stack_id = str(self.inventory.browse()[0]["id"])
        panel = self.correction_panel()
        refusal = self.choose(panel.view, "qm:correct:pick", [stack_id], "bystander")
        self.assertIn("Only configured DM administrators", refusal.text)

        with self.assertRaisesRegex(InventoryError, "only a DM administrator"):
            self.inventory.consume_interaction(
                "surface-player-stash-removal",
                actor_id=str(PLAYER_ID),
                stack_id=stack_id,
                quantity=1,
            )
        self.assertEqual(self.stash_quantities(), {"Rope": 4})

    def test_the_correction_panel_says_so_when_there_is_nothing_to_correct(self) -> None:
        panel = self.correction_panel()
        self.assertIn("The Party Stash is empty", panel.text)
        self.assertEqual([item.custom_id for item in panel.view.children if item.custom_id.endswith("pick")], [])


if __name__ == "__main__":
    unittest.main()
