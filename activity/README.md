# The Quartermaster Activity

The web surface Discord embeds in its own client. Stages 1 to 4 of
[the migration plan](../docs/activity-migration-plan.md): the API, the OAuth
handshake, the live feed that keeps the screen current, and four screens a
player can act on — Party Stash, My Items, Loot, and Treasury, with the instance
roster beside them.

None of it has been framed by Discord once. What is left of Stage 0 is a tunnel,
a URL mapping, and a launch; it needs no paid hosting, because the bot keeps
running where it runs and only the origin is rented.
[Serving the Activity](../docs/runbook.md#serving-the-activity-without-paying-for-hosting)
has the two free ways to get one, what to set, and what the first launch is
really testing.

Everything about Stage 0 that does not involve Discord is a command:

```powershell
uv run python -m quartermaster preflight
```

It serves the real application on `QM_API_BIND` for a few seconds and checks the
things that otherwise surface as a blank frame inside a Discord client — most of
all a bundle built without a client id, which compiles, serves, and contains
nothing.

## What it is made of

| File | What it does |
| --- | --- |
| `src/main.js` | The boot sequence and the state the screens read: SDK ready, authorize, exchange, authenticate, connect, read, subscribe |
| `src/api.js` | The session token, every call that carries it, the idempotency key on a mutation, and getting another token when this one expires |
| `src/actions.js` | What a press means: prepare, one key per action, and what to do with a refusal |
| `src/live.js` | The socket, its cursor, and the reconnect that resumes from it |
| `src/render.js` | The screens, built with `createElement` rather than `innerHTML` |
| `src/format.js` | The few things the screens and the action results both have to say the same way |
| `src/style.css` | A palette that follows the client's theme rather than choosing one |

The API it talks to is `src/quartermaster/api_app.py`; the feed behind it is
`src/quartermaster/api_live.py`.

## What a press costs

A mutation addressed by a handle is two calls: `prepare` mints the handle, and
the action spends it. The handle carries the read set the action was decided
against, which is what lets `Take all` mean a number rather than "whatever is
there when this arrives" — and it is minted when the player presses rather than
when the screen rendered, because a browser renders itself.

Every mutation carries an `Idempotency-Key`, generated once per action and
reused across every retry of it. The receipt behind the mutation answers a
replayed key with what it already did, so a retry over a bad connection is a
retry rather than a second take. A key per *request* would break exactly that.

A refusal comes back as a code and a sentence. `STALE` means the number moved
between deciding and pressing, and is put to the player as a question rather
than resolved for them. `HANDLE` means the control was already spent; nothing
happened, and the screen is read again. `REFUSED` is the domain's answer, and it
is already a sentence.

Nothing on this side sends an actor id. The server reads that from the token it
signed, and a client that offered one would be ignored.

## How the screen stays current

`/.proxy/api/live` is a WebSocket. It carries change notifications — a
sequence, an event type, when it landed — and never state, so the answer to
"something happened" is to ask for the reads on screen again rather than to
render what arrived. That keeps one renderer per fact.

The token goes in the socket's first frame, because a browser cannot set an
`Authorization` header on a WebSocket and a query string would put a bearer
credential in every log on the way.

The socket opens *before* the first read. A change landing between a read and a
connection would otherwise fall in the gap between them; connecting first can
only cost a redundant refresh. Every notice carries the sequence it reached, and
reconnecting asks to resume from the last one seen — so a dropped connection is
a gap to fill, and only a gap too wide to replay costs a full reload.

The header says `Live`, `Connecting…`, or `Reconnecting…`. A surface that reads
live has to say when it has stopped, or the table trusts a number that stopped
moving ten minutes ago.

## Configure the application in Discord's developer portal

1. **OAuth2 → Redirects**: no redirect is needed; the Activity uses the
   code grant through the SDK rather than a browser redirect.
2. **Activities → Settings**: enable Activities for the application.
3. **Activities → URL Mappings**: map the root prefix `/` to the host serving
   this app. Everything the client fetches goes through
   `<application_id>.discordsays.com`, which is why every request in `api.js`
   is prefixed `/.proxy/`. The proxy forwards that to `/api/...` on the mapped
   target; since 2025-07-30 an unprefixed path is forwarded identically. The API
   answers on both, so this does not depend on which behaviour is live — and it
   is what lets the built page be opened straight from the bind for a smoke
   test, with no proxy in front of it.
4. **Entry Point Command**: the launcher. `/quartermaster` continues to open
   the panel until Stage 6 decides what the bot keeps.

Copy the application's **Client ID** and **Client Secret** — the backend needs
both, and the frontend needs the id.

## Environment

Vite reads its environment from the repository root (`envDir: ".."`), so one
`.env` serves both halves:

```
VITE_DISCORD_CLIENT_ID=...      # the frontend, to identify itself to Discord
QM_DISCORD_CLIENT_ID=...        # the backend, for the token exchange
QM_DISCORD_CLIENT_SECRET=...    # the backend; never reaches the client
QM_API_BIND=127.0.0.1:8080
QM_ACTIVITY_ORIGIN=https://...  # only when the frontend is served separately
QM_ACTIVITY_DIST=activity/dist  # serve the built page from the API's own origin
```

`QM_ACTIVITY_DIST` is what makes one URL mapping enough: the page and the API
answer on the same origin, so the client's fetches are same-origin and
`QM_ACTIVITY_ORIGIN` (which exists only to open CORS for a split origin) can
stay unset.

The Activity stays off until both `QM_DISCORD_CLIENT_ID` and
`QM_DISCORD_CLIENT_SECRET` are set. The bot and the export CLI run without them.

## Develop

Discord will only frame an `https` origin, so local development needs a tunnel.

```powershell
# 1. the bot and the API, in one process
uv run python -m quartermaster --db .\quartermaster.sqlite run

# 2. the frontend, proxying /.proxy/api to the API above
cd activity
npm install
npm run dev

# 3. a public https origin pointing at the dev server
cloudflared tunnel --url http://localhost:5173
```

Put the tunnel's `https://…` hostname in the portal's URL Mappings, then launch
the Activity from a voice channel in the configured guild.

The dev server rewrites `/.proxy/api/*` to the API and strips the prefix, so the
same paths work behind the real proxy and in development without the client
knowing which it is in. It carries the WebSocket upgrade on the same prefix, so
the live feed works in development too. Point it somewhere else with
`QM_API_URL`.

The backend needs the `activity` extra installed for the socket (`uv sync --extra
activity`): uvicorn speaks HTTP without a WebSocket implementation and answers an
upgrade with a 404, which reads as a missing route rather than a missing
dependency. The test suite does not need it — Starlette's test client runs a
socket in-process — so a green suite is not evidence that the feed can be served.
The API refuses to start rather than serve a feed nothing can connect to, and
`preflight` checks for it first.

## Build and serve

```powershell
cd activity
npm run build          # emits activity/dist
```

Then set `QM_ACTIVITY_DIST=activity/dist` and run the bot. The API serves the
built page at `/` and its own routes under `/api`, mounted so that `/api/...`
is never shadowed by a file.

`VITE_DISCORD_CLIENT_ID` has to be set for the build to mean anything. Vite
replaces `import.meta.env` at build time, so without it the client id is
statically undefined, the boot sequence returns on its first branch, and the
bundler removes the entire application as unreachable — a successful build of
nothing. CI sets a placeholder for this reason.

## What is not here yet

The DM surface (Stage 5): grants, Loot Drops, sessions, combat, corrections, and
maintenance are still panels. What a DM can do here is what Stage 4 named —
register a character, and give coin from the treasury.
