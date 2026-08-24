# Quartermaster operator runbook

SQLite is canonical. Discord messages are disposable projections. If Discord is unavailable, use the export and health commands below and continue the session without the bot.

## Start, stop, and restart

The supported Windows process wrapper imports the required user-level environment values, starts a hidden supervisor, writes bot stdout/stderr to `logs/`, records the supervisor process ID in `quartermaster.pid`, and restarts an unexpected bot exit:

```powershell
.\scripts\start-quartermaster.ps1
.\scripts\stop-quartermaster.ps1
.\scripts\start-quartermaster.ps1
```

Do not run a second ad-hoc bot process against the same database. The database path is read from `QM_DATABASE_PATH`; `run` refuses a `--db` that disagrees with it rather than quietly starting against the configured value.

Use the stop wrapper for intentional shutdown. It signals the supervisor, stops the full supervised process tree, and removes the PID/stop markers. If the bot exits unexpectedly, inspect `logs/quartermaster.supervisor.log` before restarting manually.

## Serving the Activity without paying for hosting

The Activity needs a publicly reachable HTTPS origin with a real certificate. It does not need a rented server, and on this project it should not have one.

Quartermaster is one process: the bot, the API, and a single SQLite writer on one asyncio loop. The free tiers of the usual hosts are the wrong shape for that. They sleep an idle web service — which drops the gateway connection the bot is holding — and they give it an ephemeral disk, which loses the campaign on the next deploy. Paying for a host later solves both; paying nothing and moving the database solves neither.

So the bot keeps running where it already runs, and only the origin is rented. That is the part that can be free: a tunnel gives a public HTTPS hostname that terminates TLS at the provider and forwards to `QM_API_BIND` on the loopback interface. Nothing is exposed on the LAN, no port is opened on the router, and the database does not move.

### Which tunnel

**Cloudflare quick tunnel — for the first launch.** No account, no domain, no configuration:

```powershell
cloudflared tunnel --url http://127.0.0.1:8080
```

It prints a random `https://<words>.trycloudflare.com` hostname with a valid certificate. The hostname is new on every restart, and the Discord URL mapping has to be re-entered each time, so this is a smoke test rather than a way to run a campaign. WebSockets pass through it. Its one documented streaming caveat — the `trycloudflare` edge buffers `text/event-stream` — does not apply here, because the live feed is a WebSocket and not SSE.

**Tailscale Funnel — for everything after that.** Free on the personal plan, and the hostname is stable across restarts, which is what makes it worth the ten minutes of setup:

```powershell
tailscale funnel --bg 8080
tailscale funnel status
tailscale funnel off
```

The syntax has shifted across client versions — older ones want `tailscale funnel 8080 on` — so confirm with `tailscale funnel --help` before assuming either. The public hostname is `https://<machine>.<tailnet>.ts.net`; there is no custom domain. Funnel listens publicly on 443, 8443, or 10000 only, but the local target is any port, so `QM_API_BIND` does not have to change. The first invocation provisions the HTTPS certificate and updates the tailnet policy file; if the `funnel` node attribute does not permit the account, Funnel refuses until the policy file grants it.

Either way the machine running the bot has to be online for the Activity to load, which is already true of the bot itself.

### Bringing it up

Install the Activity extra. The bot does not need it and the export CLI never does, so a machine that has only ever run the bot does not have it:

```powershell
uv sync --extra activity
```

This is not optional decoration. It carries `websockets`, and without a WebSocket implementation uvicorn serves every route normally and answers the upgrade on `/api/live` with a 404 — so the screen loads, says it is connecting, backs off, and shows numbers that stopped moving. The API now refuses to serve rather than start into that, and says so in the log.

Build the frontend once, and again whenever `activity/src` changes:

```powershell
cd .\activity
npm install
npm run build
cd ..
```

`VITE_DISCORD_CLIENT_ID` has to be set for that build to mean anything — Vite replaces `import.meta.env` at build time, so without it the client id is statically undefined and the boot sequence would return on its first branch. The Vite config now fails the build before emitting that unusable bundle. `preflight` below still checks the built page and its assets, because a stale bundle or wrong distribution directory can otherwise look like a hosting failure inside Discord.

Set the Activity configuration alongside the existing values. The application ID is in the Developer Portal under General Information, the secret under OAuth2:

```powershell
$env:QM_DISCORD_CLIENT_ID = "..."
$env:QM_DISCORD_CLIENT_SECRET = "..."
$env:QM_API_BIND = "127.0.0.1:8080"
$env:QM_ACTIVITY_DIST = ".\activity\dist"
# Optional rollback while retiring the old bot panels. The default is retained.
$env:QM_DISCORD_SURFACE = "retained"
```

These can be set user-level like the backup values, and `start-quartermaster.ps1` imports every `QM_` value it finds there. It previously imported only the five required ones, which meant an Activity configured user-level reached the Developer Portal but never reached the process; if that has already been set up against an older copy of the script, take this one.

Leave the bind on the loopback address. The tunnel connects from the same machine, so the API never needs to be reachable on the network, and the session token is a bearer credential that should not be answerable to anything else. The API starts only when both `QM_DISCORD_CLIENT_ID` and `QM_DISCORD_CLIENT_SECRET` are set; with either missing the bot starts exactly as it does today and logs that the Activity is disabled. The export CLI never needs them.

Then check this machine's half of it before involving Discord at all:

```powershell
uv run python -m quartermaster preflight
```

`preflight` serves the real application on `QM_API_BIND` for a few seconds and asks it the questions the first launch will ask: is the Activity configured, can the live feed be served, was the page built and built with a client id, does the origin answer on both path forms, does the page find its own assets, is an unauthenticated read refused, does `/api/live` upgrade and refuse a token this process did not sign. It runs against a throwaway database — nothing it asks is a question about the campaign — so it is safe beside a live bot, though it will report the bind as taken; pass `--bind 127.0.0.1:8099` to check a build on another port.

Every check has to pass before a hostname is worth putting in a URL mapping. Each failure names its own remedy, and the point of all of them is that these are the failures that otherwise turn up as a blank frame inside a Discord client, which is the worst place to debug any of them.

Start the tunnel, then the bot, then configure the Developer Portal:

1. **Settings → enable Activities.**
2. **OAuth2 → Redirects** → add `https://127.0.0.1` and save it. Discord requires a registered redirect URI for the Activity OAuth code grant; the SDK handles the return to the embedded frame.
3. **URL Mappings → root mapping `/`** → the tunnel hostname, without the scheme. One mapping is enough: the built page and the API share an origin, because the API serves `QM_ACTIVITY_DIST` at `/` and its own routes under `/api`.

   The client addresses every call as `/.proxy/api/...`, which the proxy forwards to `/api/...` here; since 2025-07-30 the prefix is optional and an unprefixed path is forwarded the same way. The API answers on both, so the mapping does not depend on which behaviour is live — and so the built page can be opened straight from the bind for a smoke test, with no proxy in front of it.
4. **Entry Point command.** Discord creates a "Launch" command automatically when Activities is enabled, so there is nothing to register. It is a global command and the bot syncs guild commands only, so `/quartermaster` and the panels are unaffected in both directions.

Confirm the origin before opening Discord at all:

```powershell
curl https://<hostname>/api/health
```

`{"status":"ok","schema_version":<n>}` means the tunnel, the bind, and the process agree. That route is unauthenticated on purpose and states nothing about the campaign; every other route requires the session token the OAuth handshake issues, and no route reads an actor from a request body.

Then launch it from the App Launcher in a voice channel.

### Verified live smoke test (2026-08-23)

The first Discord launch was completed against the configured guild using a
Cloudflare Quick Tunnel. The live acceptance verified:

- the public health endpoint and the Activity page through the Developer Portal URL mapping;
- the OAuth code exchange through `/api/token`;
- a live `/api/live` WebSocket connection;
- successful reads for Party Stash, My Items, Loot, and Treasury; and
- a complete player mutation: after registering an active Discord-bound character,
  `Take 1` moved one `Handoff Loot Gem` from Party Stash to My Items.

The expected refusal also worked before registration: taking from Party Stash
without an active character returned `422 Unprocessable Content`. The successful
mutation returned `200 OK`, and My Items showed the held item without a page reload.

### Follow-up Dice acceptance (2026-08-23)

The initial smoke run did not include Dice. After restarting the stale pre-Dice bot
process onto the current checkout, the Activity verified a public `d20+5`, a public
advantage roll, and a DM-only `d20`. The public results appeared in the Session 3
history thread with their breakdowns; the DM-only result was shown as not recorded in
the session log. A second-client Dice propagation check remains open.

The quick-tunnel hostname is temporary. Recreate the URL mapping after a tunnel
restart; use Tailscale Funnel or another stable HTTPS origin for a longer-running
campaign.

### What remains after the first live launch

The smoke test covered the first launch and one positive player mutation. The
following still need a broader table session or additional clients:

- two clients receiving a bot-side change simultaneously;
- reconnect and token-refresh behaviour after interruption or expiry;
- mobile layout and backgrounding behaviour;
- the full DM workflow without opening `/quartermaster`; and
- a complete evening, including session, loot, treasury, combat, and continuity flows.

A failure in any of these costs no domain code and no stored state, but they
should be answered before retiring the existing panel surface.

## Health and maintenance

Run these commands from the repository root:

```powershell
uv run python -m quartermaster --db $env:QM_DATABASE_PATH health
uv run python -m quartermaster --db $env:QM_DATABASE_PATH maintenance
uv run python -m quartermaster --db $env:QM_DATABASE_PATH export > .\quartermaster-export.md
```

`--db` may be left off entirely: it defaults to `QM_DATABASE_PATH`. Only `run` and `restore` create a database file. Every other command refuses a path with no database at it and names the path it looked for, so a shell that never imported the user-level variables — or a command run from the wrong directory — reports a missing database instead of answering about an empty one it just created.

`health` checks SQLite integrity, schema version, the one-active-session invariant, receipt recovery state, outbox backlog, dead-lettered events, dirty and stuck projections, expired Loot Drops, the last transient-maintenance outcome, backup freshness, and the most recent Discord surface reachability check. A missing, failed, or stale Discord surface check is `DEGRADED`; pass `--discord-surface-max-age-seconds` to change the freshness window. The bot runs startup recovery, transient maintenance, scheduled backups, and surface checks while projection delivery is running; the `maintenance` command remains available for operator-triggered cleanup. It expires due drops and removes terminal receipts and consumed/expired handles after their configured retention periods.

Every interaction logs what its acknowledgement cost, naming the control that was pressed:

```
INFO quartermaster.discord_common: acknowledged qm:stash:take in 84ms
WARNING quartermaster.discord_common: acknowledgement for qm:stash:take took 2600ms, past the 2500ms internal hard deadline
```

Discord's own deadline is about three seconds, so the warning is the signal that the host is close to losing an acknowledgement — not that anything was lost. One in isolation is worth noting; a run of them means the budget in `QM_INTERNAL_HARD_DEADLINE_SECONDS` or the host itself needs attention.

`/quartermaster` is the whole Discord surface. It opens an ephemeral home panel that states session, Party Stash, open loot, treasury, and who the caller is playing, and every action past that point is a button, a select menu, or a modal on a panel. The panel a caller sees is the panel they may use: DM Tools appears only for a configured DM administrator, and every DM control re-checks authorization when it is pressed, because a panel outlives the render that built it. Grant loot, Loot Drops, Session, and Maintenance — export, backup, health — all live under DM Tools.

## Truncated Discord surfaces

Discord refuses any message over 2000 characters, so every Quartermaster surface renders within that limit and says when it had to stop. A pinned Party Stash ending in

```
… and 42 more Party Stash entries not shown here. The Quartermaster export holds the full record.
```

is working as intended, not damaged: the stash outgrew one Discord message. `export` is the complete record — items with the character holding them named, open Loot Drops with what is still unclaimed, and the roster — and **Party Stash → Take something…** still reaches individual stacks. The same note can appear on the Open Loot and Characters panels. Nothing is dropped from canonical state — only from the disposable projection.

A browse or claim listing can also end with

```
The last 8 entries above have no take control here. Take what is showing and open this again.
```

One Discord view carries twenty-five controls, two of which are Refresh and the way back, and a stack above one offers both `Take 1` and `Take all`, so a long listing runs out of buttons before it runs out of items. The controls always cover the top of the list; taking what is showing and pressing Refresh brings the rest into reach.

## The Party Stash projection is not pinned

Pinning needs Manage Messages, and a Discord channel holds fifty pins. When the pin fails the projection still delivers — the message is the surface, the pin is only where it sits — and the log says so on every attempt:

```
WARNING quartermaster.discord_projection: could not pin the Party Stash projection in #party-inventory: 403 Forbidden (error code: 50013): Missing Permissions. Grant Manage Messages, or unpin something if the channel is at its pin limit.
```

Grant the permission or free a pin slot; the surface pins itself on the next delivery, with no operator action and no lost updates in between. `health` will not report this — the projection is current, it is just scrolling with the channel.

## Upgrading to schema 13

Schema 13 adds versioned `character_dossiers` snapshots. The first source is a DM-only
typed manual import; the Activity reads the snapshot but never uses it to authorize or
calculate a mechanic. The migration needs no operator action.

## Upgrading to schema 12

Schema 12 adds Quartermaster's `combat_encounters` record. It stores the session,
channel, open/closed state, timestamps, and the DM's outcome note; it does not store
Avrae mechanics such as HP, initiative, conditions, resources, or combatants. The
migration needs no operator action. Opening or closing a Quartermaster combat remains
separate from running the native Avrae combat, and ending a session closes any
Quartermaster encounter left open.

## Upgrading to schema 11

Schema 11 adds a failure counter to `projection_targets`. It takes a default and needs no operator action.

## Upgrading to schema 10

Schema 10 adds the rule that a Discord user may hold at most one active character. If the
live database already breaks it, the migration refuses and the bot will not start; the error
names the players and characters involved:

```
schema migration 10 failed: UNIQUE constraint failed: characters.discord_user_id.
Quartermaster now allows one active character per Discord user. Mark the extras DEAD,
RETIRED, or DEPARTED on the previous build, then upgrade: Discord user 123 has Aria, Borin
```

Which character to stand down is a campaign decision, so the migration will not guess. Start
the previous build, resolve each named player from **Characters → Lifecycle…**, then upgrade. The
database is not modified by the failed attempt.

The same migration also recomputes `normalized_name` on existing stacks and merges any that
collide once they agree — two stacks of the same item that differed only by capitalization or
internal spacing become one, with their quantities added.

## Dead-lettered events

Event delivery is FIFO per destination, so an event that can never be delivered would otherwise hold up every later event to the same channel or session thread. After eight consecutive hard failures — a deleted thread, a revoked permission, a destination that no longer resolves — the event is marked `FAILED` and delivery moves on. Rate limits are not hard failures: they are retried at the delay Discord asks for and never spend that budget.

A dead letter is permanent until an operator clears it, so `health` reports `event_dead_letters: FAILED` rather than folding it into the ordinary outbox backlog, which drains on its own:

```powershell
uv run python -m quartermaster --db $env:QM_DATABASE_PATH health
```

Nothing is lost from canonical state — the ledger and `domain_events` still hold the event; only the disposable Discord notification was dropped. Repair the destination first (recreate the thread, restore the permission), then requeue:

```powershell
uv run python -m quartermaster --db $env:QM_DATABASE_PATH requeue-events
uv run python -m quartermaster --db $env:QM_DATABASE_PATH requeue-events --destination-key "session:<session-id>"
```

Requeueing before fixing the destination just spends the failure budget again. If the destination is gone for good and the notification no longer matters, leaving the row `FAILED` is a valid end state — but health will keep reporting it, so prefer requeueing once the surface is healthy.

## Stuck state projections

A state projection is not queued behind anything, so it is never dead-lettered: the Party Stash message reflects current state, and one successful delivery makes it correct again no matter how many failed before it. It does back off. Hard failures double the wait up to five minutes, so a surface that is failing on something structural — the channel deleted, the bot's permission to it revoked — costs one attempt every five minutes rather than one every second. Rate limits wait exactly as long as Discord asks and do not count.

After eight consecutive failures `health` stops calling it a backlog:

```
- state_projections: FAILED
- projection party-stash: 8 consecutive failures: 403 Forbidden (error code: 50013): Missing Permissions
```

Fix what the error names — the channel, the permission — and the next delivery clears the count on its own. Nothing has to be requeued. A `DEGRADED` `state_projections` with no `projection` line under it is an ordinary backlog and drains without help.

## Backup and restore

The bot creates and validates a timestamped online backup immediately after its projection runner starts, then repeats it at `QM_BACKUP_INTERVAL_SECONDS` (default 24 hours). It uses `QM_BACKUP_DIRECTORY` (default `backups`), optionally copies to `QM_BACKUP_OFF_DEVICE_DIRECTORY`, and retains `QM_BACKUP_RETENTION_COUNT` snapshots (default seven). Local-only storage is the current deployment policy; leave the off-device setting unset unless a second destination is intentionally configured. The command below remains available for an immediate operator-triggered backup:

```powershell
uv run python -m quartermaster --db $env:QM_DATABASE_PATH backup
```

It writes into the same `QM_BACKUP_DIRECTORY` with the same `QM_BACKUP_RETENTION_COUNT` as the scheduled backup, because the two rotate one set of files and health reports on whichever snapshot was written last. `--destination`, `--off-device-directory`, and `--retention-count` override the configured values for one run.

Configured DM administrators can also take one from **DM Tools → Maintenance → Backup** in the Discord guild. It uses the same validated timestamped path, records a durable `PROCESSING -> COMMITTED/FAILED` receipt, and reports the resulting snapshot filename ephemerally. The Discord control does not attach the SQLite file; use the CLI or the configured backup directory to retrieve it.

For a second mounted or network-backed destination, copy the validated snapshot there and apply the same retention count:

```powershell
uv run python -m quartermaster --db $env:QM_DATABASE_PATH backup --off-device-directory D:\quartermaster-backups --retention-count 14
```

To configure the scheduled path, set these user-level environment values before starting the bot:

```powershell
$env:QM_BACKUP_DIRECTORY = ".\backups"
# Optional future second destination:
# $env:QM_BACKUP_OFF_DEVICE_DIRECTORY = "D:\quartermaster-backups"
$env:QM_BACKUP_RETENTION_COUNT = "14"
$env:QM_BACKUP_INTERVAL_SECONDS = "86400"
```

When configured, the off-device path must be a different directory from the primary backup directory. Backup health always verifies the primary snapshot; it also verifies the recorded secondary file when an off-device path is configured, and an overdue or missing configured copy degrades health.

Restore is deliberately non-destructive by default. Stop the bot first, preserve the current database, then restore to a new path and verify its export before changing `QM_DATABASE_PATH`:

```powershell
.\scripts\stop-quartermaster.ps1
Copy-Item -LiteralPath $env:QM_DATABASE_PATH -Destination .\backups\before-restore.sqlite
uv run python -m quartermaster --source .\backups\known-good.sqlite --db .\restored.sqlite restore
uv run python -m quartermaster --db .\restored.sqlite health
uv run python -m quartermaster --db .\restored.sqlite export
```

Only use `restore --replace` when the destination has been explicitly chosen and the current database has been backed up. Never overwrite the only copy of campaign state.

A backup taken before a later schema migration is still restorable. Restore copies the snapshot first and brings the copy up to the current schema, so the archived file keeps the schema version it was taken at; the restored database is validated at the current version. A snapshot from a schema this build does not know is refused rather than migrated.

## Degraded operation

The bot can be stopped without losing canonical state. While Discord delivery is unavailable, continue with `export` and a local `backup`; after restart, the projection runner recreates or edits the disposable Discord surfaces. A stale Discord surface check degrades health only after the configured freshness window; database, receipts, outbox, and backup checks remain independently visible.

If `health` is `DEGRADED` or `FAILED`:

1. Stop the bot if it is repeatedly failing or the database check is not `OK`.
2. Run `export` from the canonical database and keep the output with the session notes.
3. If the database is readable, take a backup and validate it.
4. Continue play manually; do not edit SQLite tables by hand.
5. Inspect `logs/quartermaster.stderr.log`, fix configuration or Discord permissions, then restart once.
6. Re-run `health` and confirm the Party Stash and session projections converge without duplicate messages.

If SQLite integrity fails, do not run maintenance or attempt an in-place repair. Preserve the database files, restore a validated backup to a new path, and compare exports before resuming.
