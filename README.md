# Quartermaster

Quartermaster is a SQLite-backed foundation for a Discord D&D table continuity companion.

The current implementation covers the runtime/recovery and projection slices from [the implementation plan](docs/implementation-plan.md): configuration validation, migrations, transaction helpers, FAST and DEFERRED receipts, response-state handling, opaque handles, Session and Party Stash domain operations, fair state projections, FIFO event delivery, recovery, and human-readable export.

## Set up the Python environment

Install [uv](https://docs.astral.sh/uv/), then create and synchronize the project environment:

```powershell
uv venv .venv
uv sync
```

Activate it when working interactively:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Run the checks

```powershell
uv run ruff check .
uv run pytest -q
```

The suite also runs under `uv run python -m unittest discover -s tests -v`. Both checks run in CI on every pull request.

## Run the export CLI

```powershell
$env:PYTHONPATH = "src"
uv run python -m quartermaster --db .\quartermaster.sqlite export
```

## Run the Discord adapter

Configure `QM_GUILD_ID`, `QM_DISCORD_TOKEN`, `QM_PARTY_INVENTORY_CHANNEL_ID`, `QM_SESSION_LOG_CHANNEL_ID`, and optionally `QM_DM_ROLE_IDS` (comma-separated Discord role IDs), then run:

```powershell
$env:QM_GUILD_ID = "..."
$env:QM_DISCORD_TOKEN = "..."
$env:QM_PARTY_INVENTORY_CHANNEL_ID = "..."
$env:QM_SESSION_LOG_CHANNEL_ID = "..."
$env:QM_DATABASE_PATH = ".\quartermaster.sqlite"
uv run python -m quartermaster --db .\quartermaster.sqlite run
```

Quartermaster registers exactly one guild-scoped command. `/quartermaster` opens an ephemeral
home panel — session, Party Stash, open loot, treasury, and who you are playing — and every
action after that is a control on a panel rather than a command with arguments to remember.
Pressing a control replaces the panel in place, so the table is always looking at one thing
that changes rather than a column of ephemeral replies.

Players get Party Stash, Open Loot, My Items, Treasury, Characters, and Combat. DM
administrators additionally get DM Tools: granting loot, opening and closing Loot Drops,
running the session, and maintenance — export, backup, and health. A panel renders only the
controls its caller may press, and every DM control checks again when it is pressed, because
a view outlives the render that built it.

Nothing is identified by hand. A character is chosen from a select menu, a player from
Discord's own user picker, an item from what you are actually holding — the surface never asks
anyone to paste a UUID.

A panel does not stay open forever, and it says so when it closes. A view that has timed out
no longer answers its own controls, so instead of leaving buttons that produce Discord's bare
"This interaction failed", it replaces them with the reason they are gone and an **Open
again** control that renders the panel from current state — new handles included, because the
old ones were single-use. A panel that has already been replaced on screen retires quietly:
the notice belongs to the view the player is looking at, not the three they navigated through
to reach it.

**Party Stash → Take something…** offers `Take 1` per stack and `Take all` for stacks above
one, using opaque single-use handles. A `Take all` whose quantity moved after the view was
rendered asks for explicit confirmation of the current amount rather than silently taking a
different number.

Taking from the Party Stash transfers ownership to the taker's registered active character,
exactly as a Loot Drop claim does; an actor with no active registered character cannot take. A
Discord user may have at most one active character at a time, which is what makes an even
treasury split even.

Possession moves both ways. **My Items** is the way back: a player hands a stack they are
holding to the Party Stash — the default — or on to another active character, without a DM in
the loop and without minting anything. Granting creates an item, so undoing a mistaken take
with it would inflate the campaign's inventory rather than move it. `Give all` carries the
same relative handle `Take all` does, so a quantity that moved between rendering and pressing
is confirmed rather than silently substituted.

Coin moves both ways on the same terms. The Treasury panel names what your character is
carrying, and **My coin…** sends it back to the treasury or hands it to another active
character. Adjusting the treasury only touches the party's own balance, so using it to undo
a mistyped `Give to…` would mint the difference rather than return it — the same reason
granting is not how you undo a take.

Items can also leave the campaign, which is the one thing giving cannot do. **My Items → an
item → Use…** spends a quantity of what you are carrying — a potion drunk, twenty arrows
fired — and **DM Tools → Correct stash…** removes a quantity from the Party Stash, which is
the repair for a grant of fifty that was meant to be five. Both are debits and neither hands
anything to anyone, so neither is a way to move an item; both are refused for anything the
owner is not actually holding, and both write a ledger line naming who, how many, and why.
You may use up what you carry; only a DM may remove what the party shares, and that is
checked again in the transaction rather than only at the control.

Every message Quartermaster sends is rendered within Discord's 2000-character limit. List
surfaces — the Party Stash projection and the stash, loot and character panels — drop whole
entries from the end and say how many they dropped rather than letting Discord reject the
message; the export is the complete record. One component view carries twenty-five controls,
two of which are Refresh and the way back, and a stack above one needs two more, so a listing
that runs out of buttons names the entries it has no control for instead of showing them with
nothing to press.

The export is what those surfaces point at, so it holds what they cannot: every item with
the character holding it named, every open Loot Drop with what is still unclaimed, the full
character roster, and the played session's history read as sentences rather than as the
payloads they are stored as — the same rendering the session log uses, from the same table,
because two copies of it are two chances to disagree about what an event means.

The Discord transport is intentionally an adapter boundary at this stage. No network calls occur in the core package or in a database transaction. The adapter is split into `discord_common` (services, authorization, response helpers), `discord_views` (the leaf controls that act, and the modals they open), `discord_panels` (the panels those controls sit on, and the navigation between them), `discord_commands` (the one entry point), and `discord_adapter` (bot assembly and the runtime loop).

For managed Windows startup, backup/restore, health, maintenance, dead-lettered events, and degraded operation, see [the operator runbook](docs/runbook.md).
