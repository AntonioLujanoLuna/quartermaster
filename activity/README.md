# The Quartermaster Activity

The web surface Discord embeds in its own client. Stages 1 and 2 of
[the migration plan](../docs/activity-migration-plan.md): the read API, the
OAuth handshake, and one read-only screen — the Party Stash, with the instance
roster beside it.

This is a walking skeleton on purpose. It proves the handshake, the proxy, and
the hosting, which are the parts that can only be discovered against real
Discord. Nothing here mutates anything; the panels remain the way to act.

## What it is made of

| File | What it does |
| --- | --- |
| `src/main.js` | The boot sequence: SDK ready, authorize, exchange, authenticate, read, subscribe |
| `src/api.js` | The session token and every call that carries it |
| `src/render.js` | The screen, built with `createElement` rather than `innerHTML` |
| `src/style.css` | A palette that follows the client's theme rather than choosing one |

The API it talks to is `src/quartermaster/api_app.py`.

## Configure the application in Discord's developer portal

1. **OAuth2 → Redirects**: no redirect is needed; the Activity uses the
   code grant through the SDK rather than a browser redirect.
2. **Activities → Settings**: enable Activities for the application.
3. **Activities → URL Mappings**: map the root prefix `/` to the host serving
   this app. Everything the client fetches goes through
   `<application_id>.discordsays.com`, which is why every request in `api.js`
   is prefixed `/.proxy/`.
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
knowing which it is in. Point it somewhere else with `QM_API_URL`.

## Build and serve

```powershell
cd activity
npm run build          # emits activity/dist
```

Then set `QM_ACTIVITY_DIST=activity/dist` and run the bot. The API serves the
built page at `/` and its own routes under `/api`, mounted so that `/api/...`
is never shadowed by a file.

## What is not here yet

Mutations (Stage 4), the DM surface (Stage 5), and the live feed over
`domain_events.sequence` (Stage 3). Until Stage 3 lands, the screen reflects
state as of the moment it loaded.
