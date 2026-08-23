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

The table can pick up where it stopped. While no session is running, home names the last one
and where it ended — the single sentence End Session asks a DM to type — and **Last time**
opens the continuity panel: that endpoint, then the end of what happened, read as sentences
through the same renderer the session log uses rather than as a second account of the evening.
It is open to everyone at the table, links to the session log where one is configured, and
says how many earlier lines it is not showing, because the export is the full record.

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

That boundary is also what makes a different surface thinkable. [The Activity migration plan](docs/activity-migration-plan.md) proposes moving play into a Discord Activity — a web UI embedded in the client, where the party joins by launching it rather than by each holding a private panel — and keeping the bot for the pinned projection, the session log, and asynchronous use.

Its first five stages are implemented. Every read a panel performs and every mutation either a player or a DM makes is available over HTTP from `api_app`, and [the Activity itself](activity/README.md) is five screens a player acts on — Party Stash, My Items, Loot, Treasury, and Dice — with the instance roster Discord supplies rather than one Quartermaster builds, and a sixth only a DM is shown. Each DM control sits on the screen showing what it changes; the DM screen holds what has no such screen, which is the session, the fight, the roster's lifecycle, and the operator's controls. The API runs on the bot's own loop against the bot's own store, because SQLite has one writer and recovery assumes one runtime. It stays off until `QM_DISCORD_CLIENT_ID` and `QM_DISCORD_CLIENT_SECRET` are configured, and FastAPI is an optional extra, so a table that has not enabled it runs exactly as before.

The first live Discord smoke acceptance completed on 2026-08-23 through a temporary
Cloudflare Quick Tunnel. The Activity loaded from a voice channel, completed the OAuth
code exchange, opened its live WebSocket, rendered the player screens, and took one
Party Stash item into **My Items** after an active character was registered. A full
evening, multi-client propagation, mobile layout, and a stable hostname remain live
follow-ups; the acceptance details and repeatable setup are in [the Activity runbook](docs/runbook.md#verified-live-smoke-test-2026-08-23).

The next product layer is deliberately separate from the v2.6 continuity core:
[Dice, Character Sheets, and Explainable Mechanics](docs/dice-and-mechanics-plan.md) now has
its first Dice slice implemented locally, with live acceptance still open. It starts with a
server-authoritative Dice view, then adds read-only character explanations and only later
considers provider-backed attacks, spells, and effects. The current Avrae boundary remains
authoritative for D&D mechanics.

The screen is live rather than a snapshot. `/api/live` is a WebSocket keyed on `domain_events.sequence` — the cursor the ledger already assigns inside the transaction that made the change — so a grant issued from the bot reaches every open screen at the table. It carries notifications rather than state: a client is told a sequence and an event type and answers by asking for the read it has on screen again, because a payload on the socket would be a second rendering of a fact the reads already answer for. The feed is woken by the store rather than polled on a timer, so an idle table costs no queries, and a client that cannot keep up is told to read everything again rather than allowed to hold the feed. Reconnecting from the last sequence seen makes a dropped socket a gap to fill instead of a screen to rebuild.

The trust boundary is the part that moved rather than the reads. The bot could read `interaction.user.id` as a fact because Discord signed the interaction; a web page cannot be trusted the same way, so identity is established once against Discord and afterwards carried in a token Quartermaster signed and re-verifies. No route takes an actor from a request, and the socket presents that same token in its first frame rather than in a query string, which would put a bearer credential in every log between here and the player.

Acting through it is the same shape as pressing a button, with the two things Discord used to supply now supplied deliberately. An interaction id was a number nobody could choose, and a browser's idempotency key is not, so the key a receipt is stored under is namespaced by the actor the token proved — otherwise one player could quote another's key and be handed their receipt. And a handle's read set was minted when the server rendered a message, which a browser does for itself, so it is minted when the player presses instead: `Take all` still means a number rather than "whatever is there by the time this arrives", and two players taking all of one stack at once still collides into a question rather than a surprise. Everything a DM does beyond registering a character and giving coin is still a panel.
