# Moving the table surface into a Discord Activity

Status: Stages 1 and 2 implemented, and neither has been run against the guild.
Stage 0 — hosting — is still open, and is what Stage 2 is waiting on. Stages 3
to 6 are proposed.

## Why

Quartermaster's surface is a chat message pretending to be an application. The
consequences are written down in the README as if they were design decisions, but each
one is a platform limit leaking through:

- List surfaces "drop whole entries from the end and say how many they dropped" because a
  Discord message stops at 2000 characters.
- "A listing that runs out of buttons names the entries it has no control for" because one
  component view carries twenty-five controls.
- Panels expire, and the adapter has to render a notice explaining that its own buttons are
  gone, with an **Open again** control to rebuild them.
- Every player holds a private ephemeral panel. The table is not looking at one thing; it is
  looking at six copies of one thing, each as stale as the last time its owner pressed a
  button.

None of these are problems with the domain. They are problems with rendering an inventory
into a text message. A Discord **Activity** — a web application Discord loads in an iframe
inside its own client — removes all four at once, because it is a real UI with scrolling,
layout, and a live connection.

## What an Activity is

An Activity is a web app served over HTTPS that Discord embeds in an iframe on desktop,
web, and mobile. It is launched from the App Launcher in a voice channel or from an
application's Entry Point command. The [Embedded App SDK][sdk] gives the page an RPC
channel to the Discord client.

Three properties matter for this project:

**The party joins by itself.** Everyone who launches the Activity in the same channel
lands in the same *instance*, keyed by an `instanceId` from the SDK.
`getInstanceConnectedParticipants()` and its update event give the live roster of who is
present. Quartermaster does not build joining, lobbies, or presence — it reads them.

**Discord synchronizes nothing else.** There is no shared state, no server, no
persistence. Every client is independent and keeping them in agreement is entirely our
backend's job. This is the work the platform hands back to us in exchange for the UI.

**The client is sandboxed.** All traffic is forced through Discord's proxy at
`<app_id>.discordsays.com`, with paths prefixed `/.proxy/` and each endpoint declared in
the developer portal's [URL Mappings][mappings]. This hides player IPs. It also means the
frontend cannot call anything we have not mapped.

[sdk]: https://github.com/discord/embedded-app-sdk
[mappings]: https://github.com/discord/embedded-app-sdk/blob/main/patch-url-mappings.md

## The boundary this lands on

The README claims the Discord transport is "intentionally an adapter boundary at this
stage." This plan is the test of that claim, and the claim mostly holds.

**Unchanged.** `db.py`, `operations.py`, `inventory.py`, `currency.py`, `loot.py`,
`characters.py`, `combat.py`, `sessions.py`, `projections.py`, `events.py`, `receipts.py`,
`handles.py`, `export.py`, `narrative.py`, `rendering.py`, `recovery.py`, `config.py`.
Roughly 7000 lines of domain, schema, and durability logic that does not know what a
button is. The service methods an Activity needs are the same ones the panels already call.

**Replaced.** `discord_panels.py` (1563 lines) and `discord_views.py` (1262 lines) — the
panel-and-button plumbing that exists only because messages are a bad toolkit. This is the
bulk of what is painful to change today.

**New.** An HTTP + WebSocket layer, and a frontend.

**Kept, permanently.** `discord_projection.py`, `discord_adapter.py`, and a much smaller
`discord_commands.py`. The pinned Party Stash projection and the session log are messages
in channels on purpose — they are the record a player scrolls past without launching
anything. Those stay.

## Architecture

### One process, two servers

The bot and the API run in the same process, on the same asyncio loop, against the same
`SQLiteStore`.

This is not a preference, it is a constraint. SQLite has a single writer; the recovery
model in `recovery.py` assumes one runtime releasing one set of projection claims at
startup; `ProjectionRunner` claims targets on the assumption that it is the only claimant.
Splitting the API into its own process means two writers, two recovery paths, and a claim
protocol that currently has no need to be correct across processes. Run `uvicorn` as a task
next to the bot task in `run_bot`, and none of that changes.

### Trust boundary — the important one

Today the adapter trusts `interaction.user.id` because Discord signed the interaction. That
is gone. **The Activity frontend is untrusted code running on a player's machine.**

The rule: `actor_id` is *never* read from a request body. The backend derives it from the
session token it issued during the OAuth handshake, and passes that to the service layer.
Every existing `actor_id` parameter keeps its meaning; only its provenance changes. A
request that names a character, a stack, or a quantity is a request; a request that names
an actor is a bug.

The same rule catches a subtler case. Using an item and correcting the Party Stash are the
same call — `consume_interaction` — separated only by `party_authorized`, which the DM-only
modal passes as `True`. Over HTTP that flag is set by the backend from the DM check, never
accepted from the body, or "use up what you carry" becomes "remove what the party shares"
for anyone who can edit a request.

Authorization follows the same line. `_is_dm` currently reads the member's roles off the
interaction. Inside an Activity there is no member object, so the backend looks the member
up with the bot token — the bot is already in the guild — and checks against
`settings.dm_role_ids`. The re-check-on-press discipline the README describes survives as
re-check-on-request, which is where it belonged anyway.

The handshake:

1. Client calls `discordSdk.commands.authorize()` with scopes `identify` and
   `guilds.members.read`, receiving a code.
2. Client posts the code to `/.proxy/api/token`.
3. Backend exchanges it for an access token using the client secret, reads the Discord user
   id, confirms `guildId` matches `QM_GUILD_ID`, resolves DM status via the bot, and issues
   a short-lived session token bound to that user and instance.
4. Client calls `discordSdk.commands.authenticate()` and holds the session token for
   subsequent calls.

### Idempotency without a Discord interaction id

`ReceiptRepository.execute_fast` and `begin_deferred` key on `interaction_id`, and today
Discord supplies it. An Activity has no interaction ids.

The client generates a UUIDv4 per user action and sends it as an `Idempotency-Key` header.
The backend passes it straight through as `interaction_id`. The existing behaviour — a
replayed key returns the stored receipt rather than re-running the mutation — becomes
retry-safety for a flaky socket, which is a better fit than it ever was for buttons. The
key must be generated when the user acts, not per request, or a retry mints a second
operation.

### Live state

There is already a monotonic sequence: `domain_events.sequence`, assigned in
`append_event`. That is the cursor.

A client opens `/.proxy/api/live?since=<sequence>`, the backend replays anything newer and
then streams as events land. Reconnection is the same call with the last sequence seen, so
a dropped socket is a gap to fill rather than a state to rebuild. Clients hold a sequence
number and refetch the affected read when they see an event type they care about; the
socket carries change notifications, not state.

This deliberately does not touch `event_outbox`, which belongs to the Discord projection
transport and its FIFO delivery guarantees. The Activity feed is a second reader of
`domain_events`, not a second writer to the outbox.

### What happens to handles

Opaque single-use handles stay, and stop being visible.

Their purpose is stated in the handoff doc: mutations are addressed by handles "carrying
the read set they were rendered against," so a `Take all` whose quantity moved is caught
rather than silently substituted. That check is about concurrency, not about buttons, and a
live UI does not eliminate the race — it only shortens the window. Two players pressing
*Take all* on the same stack within the same tick is still two requests against one read
set.

What changes is the experience. Today a stale handle produces a confirmation prompt the
player has to reason about. With a live feed the quantity on screen is current, so the
prompt fires rarely, and when it does it is a genuine conflict rather than an artifact of a
panel rendered ninety seconds ago. `SemanticStaleness` and
`CurrencySemanticStaleness` keep meaning exactly what they mean now.

## API surface

All paths are relative to the mapped proxy root. Every endpoint requires the session token;
DM-only endpoints re-check role membership per request.

### Reads

| Endpoint | Backed by |
| --- | --- |
| `GET /api/home` | composed: session state, `CurrencyService.view_treasury`, active character |
| `GET /api/stash` | `InventoryService.browse` |
| `GET /api/me/items` | `InventoryService.holdings` |
| `GET /api/loot` | `LootDropService.list_open` |
| `GET /api/loot/claimable` | `LootDropService.prepare_claim_view` |
| `GET /api/treasury` | `CurrencyService.view_treasury`, `CurrencyService.purse` |
| `GET /api/characters` | `CharacterService.list_characters` |
| `GET /api/combat` | `CombatService.status` |
| `GET /api/session/continuity` | `SessionService.continuity` |
| `GET /api/export` | `render_export` |

The `limit=25` defaults on `holdings` and `prepare_claim_view` exist to fit twenty-five
components. Over HTTP they become real pagination, or nothing at all.

### Mutations

Each takes `Idempotency-Key`; each derives `actor_id` from the token.

| Endpoint | Backed by | DM |
| --- | --- | --- |
| `POST /api/stash/take/prepare` | `InventoryService.prepare_take_view`, `create_take_handle` | |
| `POST /api/stash/take` | `InventoryService.take_interaction` | |
| `POST /api/stash/take/confirm` | `InventoryService.confirm_take_interaction` | |
| `POST /api/items/give/prepare` | `InventoryService.create_give_handles` | |
| `POST /api/items/give` | `InventoryService.give_with_handle_interaction` | |
| `POST /api/items/give/confirm` | `InventoryService.confirm_give_with_handle_interaction` | |
| `POST /api/items/use` | `InventoryService.consume_interaction` | |
| `POST /api/loot/claim` | `LootDropService.create_claim_handle`, `claim_interaction` | |
| `POST /api/treasury/give` | `CurrencyService.give_to_character_interaction` | |
| `POST /api/treasury/return` | `CurrencyService.give_from_character_interaction` | |
| `POST /api/characters` | `CharacterService.create_interaction` | |
| `POST /api/characters/transition` | `CharacterService.transition_interaction` | |
| `POST /api/stash/grant` | `InventoryService.grant_interaction` | ✓ |
| `POST /api/stash/correct` | `InventoryService.consume_interaction`, `party_authorized=True` | ✓ |
| `POST /api/loot/drops` | `LootDropService.create_drop_interaction` | ✓ |
| `POST /api/loot/drops/close` | `LootDropService.close_drop_interaction` | ✓ |
| `POST /api/treasury/adjust` | `CurrencyService.adjust_treasury_interaction` | ✓ |
| `POST /api/treasury/split/preview` | `CurrencyService.preview_split`, `prepare_split` | ✓ |
| `POST /api/treasury/split` | `CurrencyService.split_relative_interaction` | ✓ |
| `POST /api/session/start` | `SessionService.start_interaction` | ✓ |
| `POST /api/session/end` | `SessionService.end_interaction` | ✓ |
| `POST /api/combat/open` | `CombatService.open_interaction` | ✓ |
| `POST /api/combat/close` | `CombatService.close_interaction` | ✓ |
| `POST /api/characters/estate` | `CharacterService.resolve_belongings_interaction` | ✓ |
| `POST /api/maintenance/*` | `run_maintenance`, backup, health | ✓ |

The mapping is close to one-to-one because the panels are already thin over the services.
Where it is not one-to-one — the composed `/api/home`, the split preview and commit pair —
the panel was already doing the composing.

### New configuration

`QM_DISCORD_CLIENT_ID`, `QM_DISCORD_CLIENT_SECRET`, `QM_API_BIND`, `QM_ACTIVITY_ORIGIN`.
All validated in `Settings.from_environment` alongside the existing keys, and all required
only when the Activity is enabled — the export CLI must keep running without them.

## Staged migration

Each stage is independently shippable and leaves the bot working.

**Stage 0 — Hosting.** Not code, and the real blocker. Today Quartermaster is one process
with a SQLite file and a Windows startup script (`docs/runbook.md`). An Activity needs a
publicly reachable HTTPS origin with a real certificate. Decide where it runs and how the
database is backed up there before writing frontend code. *Exit: an origin exists and
serves a static page through the Discord proxy.*

**Stage 1 — API layer, no frontend.** *Done.* `api_auth` (session tokens, identity),
`api_app` (routes), `api_server` (serving next to the bot), covered by `tests/test_api.py`.
Every read in the table above is reachable, `actor_id` comes only from a signed token, and
the home composition moved to `snapshots` so both surfaces read one of it. *Exit met:
`pytest -q` and `ruff check .` green.*

**Stage 2 — Walking skeleton.** *Built, not yet launched.* `activity/` holds the SDK
handshake and one read-only Party Stash screen with the instance roster beside it. The
build is served by the API itself under `QM_ACTIVITY_DIST`, so the page and its data share
an origin and one URL mapping covers both. *Exit still open: it needs an https origin and
a real launch in the guild — see Stage 0. The Entry Point command is not registered yet
either; `/quartermaster` still opens the panel.*

**Stage 3 — Live feed.** The `domain_events.sequence` WebSocket, with reconnect-from-cursor.
*Exit: a grant issued from the bot appears on an open Activity screen without a refresh.*

**Stage 4 — Player mutations.** Take, give, use, claim, coin, character registration. The
first stage where a handle round-trips through the new transport. *Exit: a full session's
player actions run through the Activity; the ledger is indistinguishable from a bot-driven
session.*

**Stage 5 — DM surface.** Grant, drops, session start/end, combat, corrections,
maintenance. *Exit: a DM runs a session end to end without opening a panel.*

**Stage 6 — Retire the panels.** Delete what Stage 4 and 5 replaced. Keep the entry point,
the projection, and a deliberately small async surface (see below). *Exit:
`discord_panels.py` and `discord_views.py` are gone or reduced to the retained surface.*

Stages 1–3 are the ones worth doing before committing to the rest. If the proxy, the
handshake, or the hosting turns out to be intolerable, that is knowable by the end of
Stage 2 and costs no domain changes to abandon.

## What the bot keeps forever

An Activity lives in a channel session. A player checking their inventory on a Tuesday
afternoon, or a DM granting loot found in their notes between games, is not in a voice
channel and should not have to enter one.

So the target is not "replace the bot." It is:

- **Activity** — everything at the table, live, during play.
- **Bot** — the pinned Party Stash projection, the session log, `/quartermaster` as the
  launcher, and a small read-mostly panel set for asynchronous use. My Items and Party
  Stash browse are the obvious keepers; grant is worth keeping for the DM.

That retained surface is why Stage 6 says "reduced to," not "deleted." Guessing its exact
shape now is premature — it should be decided after a real session on the Activity, when
it is clear what people actually reach for outside one.

## Risks

**Two surfaces, one domain.** Every retained bot panel is a second renderer of state the
Activity also renders. The README's own argument against duplicated rendering — "two copies
of it are two chances to disagree" — applies here. Keeping the retained surface small is
not tidiness, it is the mitigation.

**Mobile.** Activities run on mobile Discord, but in a cramped viewport. If a meaningful
share of the table plays from a phone, the layout constraint is real and should be
established in Stage 2 rather than discovered in Stage 5.

**Stack surface.** This adds a JavaScript toolchain, a web server, TLS, and a deployment
story to a project that currently has one dependency and runs from a shell. That cost is
paid once, but it is paid by whoever operates it, and the runbook grows accordingly.

**Verification.** Only required to be publicly discoverable in the App Launcher. A single
campaign server installs it directly and skips this. Worth confirming before assuming
otherwise.
