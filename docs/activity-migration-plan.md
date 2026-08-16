# Moving the table surface into a Discord Activity

Status: Stages 1, 2, 3, and 4 implemented, and none of them has been run against
the guild. Stage 0 — hosting — is answered: a Cloudflare tunnel in front of the
loopback API, with the process, the database, and the backups staying on the
machine they are on today. The procedure is in [the runbook](runbook.md); what
remains is to run it. Stages 5 and 6 are proposed and should wait for the first
real launch.

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
The existing behaviour — a replayed key returns the stored receipt rather than re-running
the mutation — becomes retry-safety for a flaky socket, which is a better fit than it ever
was for buttons. The key must be generated when the user acts, not per request, or a retry
mints a second operation.

Built, with one correction to "passes it straight through". Discord supplied an
interaction id nobody could choose; a client chooses this one, so the key the receipts
table sees is `activity:<actor>:<key>` — namespaced by the actor the token proved, with
the separator kept out of the key's alphabet. Unscoped, one player could quote another's
key and be handed their receipt. The key is also bounded and checked against that
alphabet, because it becomes a primary key in `interaction_receipts`.

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

Built, in `api_live.py`, with four decisions the sketch above did not settle:

- **The feed is woken, not polled.** `SQLiteStore.add_commit_listener` reports that a write
  transaction committed — that it did, and nothing about what was in it — so an idle table
  costs no queries and a busy one costs one indexed read per commit. A timer instead would
  contend with the gateway for the single connection all evening, which is the cost
  `expire_due_drops` was changed to stop paying. A 30-second poll remains as a safety net,
  and only ticks while somebody is listening.
- **The token travels in the socket's first frame.** A browser cannot set an `Authorization`
  header on a WebSocket, and the alternative — a query string — is a bearer credential in
  every access log between here and the player.
- **A client that falls behind is reset, not buffered.** Each subscriber has a bounded
  queue; past it the backlog is dropped and the client is told to read everything again,
  which is what it would do on reconnect anyway. A resume whose gap is wider than the replay
  bound gets the same answer.
- **One pump for the process.** Six players at one table would otherwise be six queries per
  change against the connection the bot is also writing through.

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

What also changes is when they are minted, and this is the one thing Stage 4 had to decide
rather than port. The panel minted handles when it rendered a message, because the server
was the thing doing the rendering. Here it is not: a `prepare` call mints when the player
acts, so the read set means "what was true when you pressed" rather than "what was on the
message". The stale *screen* is prevented upstream by the live feed instead of being caught
downstream by a confirmation — and the race this section is actually about, two players
taking all of one stack in the same tick, happens inside that round trip and is still
caught there. The alternative, minting against every read so a handle exists for every
listed stack before anyone presses anything, is the component budget again: it would mint
handles for a forty-stack stash on every change, for every client at the table.

A claim is the exception, and mints inside the request that spends it. Its handle carries
`remaining_quantity` and nothing compares it to anything — the claim is absolute and
re-checks the remainder in the transaction — so there is no relative meaning for a read set
to preserve. What the handle does there is bind the claim to one actor and one use, and
minting it in the request satisfies both.

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

Each takes `Idempotency-Key`; each derives `actor_id` from the token. A `prepare` mints a
handle and is not itself an action, so it takes no key: a retry costs an unspent handle
rather than a receipt for something nobody completed.

| Endpoint | Backed by | DM |
| --- | --- | --- |
| `POST /api/stash/take/prepare` | `InventoryService.create_take_handle` | |
| `POST /api/stash/take` | `InventoryService.take_interaction` | |
| `POST /api/stash/take/confirm` | `InventoryService.confirm_take_interaction` | |
| `POST /api/items/give/prepare` | `InventoryService.create_give_handles` | |
| `POST /api/items/give` | `InventoryService.give_with_handle_interaction` | |
| `POST /api/items/give/confirm` | `InventoryService.confirm_give_with_handle_interaction` | |
| `POST /api/items/give/some` | `InventoryService.give_interaction` | |
| `POST /api/items/use` | `InventoryService.consume_interaction` | |
| `POST /api/loot/claim` | `LootDropService.create_claim_handle`, `claim_interaction` | |
| `POST /api/treasury/return` | `CurrencyService.give_from_character_interaction` | |
| `POST /api/treasury/give` | `CurrencyService.give_to_character_interaction` | ✓ |
| `POST /api/characters` | `CharacterService.create_interaction` | ✓ |
| `POST /api/characters/transition` | `CharacterService.transition_interaction` | ✓ |

Three rows carry a check mark this table did not originally give them. Treasury → a
character, registering a character, and moving one through its lifecycle are all behind
`_require_dm` on the panel, and an API that grants authority the surface it replaces does
not grant is not a migration of it. `prepare_take_view` left the table for the same kind of
reason: it exists to mint against a component budget, and there is no budget here.

`/api/items/give/some` is new to the table and was always on the panel — a give whose
quantity the player types needs no handle, because the number came from the person giving
rather than from a render.
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

**Stage 0 — Hosting.** Not code, and it was the real blocker. *Decided: a named Cloudflare
tunnel in front of the API, which keeps binding loopback.* The alternative was a VPS, and
the reason it lost is the second half of the question rather than the first: an origin is
easy to buy, but moving the process moves the SQLite file off the machine the entire
backup, restore, and supervisor story is written against, and that rewrite is the expensive
part of the decision. The tunnel answers the origin and leaves the database, the schedule,
and the runbook exactly where they are — and if the table decides after one session that
the Activity is not what it wants, it is uninstalled rather than decommissioned.

What that costs, stated rather than discovered: a third-party dependency on the play path,
and uptime bounded by one machine being on. Both are acceptable for a table that plays on
a schedule and already depends on that machine for the bot. If the Activity turns out to be
the surface the table lives in, a VPS becomes worth its migration; that is a decision to
make with evidence rather than before.

The procedure — building the bundle, the named tunnel, the ingress rule that must not match
on hostname, and what the table sees when the tunnel is down — is in
[the runbook](runbook.md). *Exit: an origin exists and serves a static page through the
Discord proxy. Not yet run.*

**Stage 1 — API layer, no frontend.** *Done.* `api_auth` (session tokens, identity),
`api_app` (routes), `api_server` (serving next to the bot), covered by `tests/test_api.py`.
Every read in the table above is reachable, `actor_id` comes only from a signed token, and
the home composition moved to `snapshots` so both surfaces read one of it. *Exit met:
`pytest -q` and `ruff check .` green.*

**Stage 2 — Walking skeleton.** *Built, not yet launched.* `activity/` holds the SDK
handshake and one read-only Party Stash screen with the instance roster beside it. The
build is served by the API itself under `QM_ACTIVITY_DIST`, so the page and its data share
an origin and one URL mapping covers both. *Exit still open: it needs a real launch in the
guild, which is now a procedure to run rather than a question to answer. Enabling
Activities in the portal creates the Entry Point command that launches it; `/quartermaster`
keeps opening the panel.*

**Stage 3 — Live feed.** *Built, not yet launched.* `api_live` holds the pump and the
subscriptions, `/api/live` is the socket, and `activity/src/live.js` is the client that
reconnects from its cursor and answers a notification by refetching. The screen says whether
it is live, because a surface that reads live has to say when it has stopped. *Exit still
open: `tests/test_api.py` proves a grant made through the service layer reaches an open
socket, which is the criterion as far as a test can carry it — the rest of it is a grant
issued from the bot in the guild appearing on a screen in Discord, and that needs a launch.*

Session tokens last an hour and an evening does not, so the client now re-runs the handshake
when one is refused rather than going quiet — on the socket and on the reads both. That gap
existed from Stage 1; a live screen is what made it visible, because a screen that stops
reading still looks connected.

**Stage 4 — Player mutations.** *Built, not yet launched.* Take, give, use, claim, coin,
character registration — the mutation table above, `activity/src/actions.js` as the client
half, and four screens to act on. The first stage where a handle round-trips through the
new transport, and the stage that decided when a handle is minted at all.

The refusals are the part worth stating. A domain refusal reaches the client as a status
and a code rather than only as a sentence, because three of them mean different things to
do: `STALE` is a question for the player and is answered on a confirm route, `HANDLE` means
the control was spent or expired and the action has to be prepared again, and `REFUSED` is
the domain's answer and the end of it. The sentence still travels in `detail`, where the
reads already put theirs.

*Exit still open: a full session's player actions running through the Activity with a
ledger indistinguishable from a bot-driven session needs a session played on it.
`tests/test_api.py` proves the ledger half — that a take through the API lands the same
rows a take through a panel does, that a quantity moving under a press is asked about
rather than substituted, that one player's handle and one player's idempotency key are not
another's, and that a take made on the Activity reaches the other screens at the table.*

**Stage 5 — DM surface.** Grant, drops, session start/end, combat, corrections,
maintenance. *Held until Stages 2 to 4 have been launched and played on.* Mutations were
cheap to build blind because they add no domain code and every route is a call a panel
already makes. The DM surface is not that: it is a judgement about what a DM should have in
front of them mid-session, and the only thing that answers it is watching one hesitate.
Building it now would be guessing at a layout for a screen nobody has seen.
*Exit: a DM runs a session end to end without opening a panel.*

**Stage 6 — Retire the panels.** Delete what Stage 4 and 5 replaced. Keep the entry point,
the projection, and a deliberately small async surface (see below). *Exit:
`discord_panels.py` and `discord_views.py` are gone or reduced to the retained surface.*

Stages 1–3 are the ones worth doing before committing to the rest. If the proxy, the
handshake, or the hosting turns out to be intolerable, that is knowable by the end of
Stage 2 and costs no domain changes to abandon.

Stage 4 was nevertheless built before Stage 0 was answered, which was out of order and is
worth keeping on the record now that the order has stopped mattering. The objection stood
at the time — "Stage 4 adds mutations to a transport nobody has launched" — and the reason
it was tolerable is that its cost was bounded on purpose: no domain code, no changed
service, every route a call a panel already makes, so an answer of "not this" deleted the
whole of it without touching anything that stores an item. Stage 0 came back "this", so the
bet is paid and the questions only a transport can get wrong — where a handle is minted,
what a refusal looks like to a client, whose idempotency key is whose — were answered
before a session rather than during one. That does not generalise, which is why Stage 5 is
held: the same argument does not survive contact with a stage whose cost is a layout
judgement rather than a route table.

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

**Mobile.** *Retired as a risk.* This table plays on PC, so the cramped-viewport constraint
does not gate anything and the layout is not designed around it. The mobile checks stay on
the verification list because a phone is what somebody reaches for when they are not at
their desk, but a layout that is wrong there is a defect to fix later rather than a reason
to hold a stage. Nothing in the screens assumes a wide viewport on purpose; nothing has
confirmed one is not assumed by accident either.

**Stack surface.** This adds a JavaScript toolchain, a web server, TLS, and a deployment
story to a project that currently has one dependency and runs from a shell. That cost is
paid once, but it is paid by whoever operates it, and the runbook grows accordingly. The
tunnel is the newest part of it and the part most likely to be the thing that is wrong on
an evening when nothing else is: it is a second service to install, to keep running, and to
suspect.

**Verification.** Only required to be publicly discoverable in the App Launcher. A single
campaign server installs it directly and skips this. Worth confirming before assuming
otherwise.
