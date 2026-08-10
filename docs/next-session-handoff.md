# Next-session handoff

Updated: 2026-08-10

## Verified current state

- Python project managed with `uv` and `.venv`.
- SQLite canonical state is at schema version 6.
- FAST and DEFERRED interaction receipts are implemented.
- Opaque single-use mutation handles are implemented.
- Bot startup now recovers interrupted DEFERRED receipts and runs transient-state maintenance before accepting interactions.
- Party Stash Grant/Take flows are implemented.
- Relative Take-all staleness now offers explicit confirmation of the current quantity.
- Loot Drops support creation, claim handles, manual close, session-close, and absolute expiry.
- Minimal Session lifecycle is implemented.
- State projection scheduling and FIFO event delivery are implemented; session projections bind to durable per-session Discord threads.
- The `discord.py` adapter exposes guild-scoped `/stash`, `/grant`, `/session-start`, `/session-end`, `/loot`, `/loot-drop`, `/loot-close`, and DM-admin `/export`.
- Discord projection delivery and bounded local interaction work run asynchronously after database commit, with deadline-aware response deferral at the configured soft deadline. `/export` uses the adapter-level durable `PROCESSING -> COMMITTED/FAILED` workflow; backup remains an operator CLI path.
- Online backups create timestamped validated snapshots, optionally copy them to a secondary directory, apply retention, and record their paths and outcome for health checks.
- Operational commands now cover `health`, `maintenance`, `backup`, and safe restore validation.
- Managed Windows process wrappers are in `scripts/start-quartermaster.ps1` and `scripts/stop-quartermaster.ps1`.
- Operator procedures for backup/restore and degraded operation are documented in `docs/runbook.md`.
- 30 automated tests pass with `uv`.

## Live Discord setup

- Server: `Quartermaster Test`
- Guild ID: `1536121899388506222`
- Party inventory channel: `#party-inventory` (`1536122527892635809`)
- Session log channel: `#session-log` (`1536122560322863224`)
- Quartermaster application/bot ID: `1536120052871602256`
- The bot is authorized with view-channel, send-message, manage-message, and read-history permissions.
- The bot is currently being run as an ad-hoc background `uv run` process; its PID is not stable.
- The bot token remains only in the user-level environment variable `QM_DISCORD_TOKEN`.
- Guild, channel, and database configuration is persisted as user-level environment variables.
- `QM_DM_ROLE_IDS` is currently unset. The server owner is accepted as a DM administrator; additional DMs need configured role IDs.

## Live verification completed

- Bot Gateway connection succeeded.
- Guild slash commands synchronized successfully.
- Party Stash state projection and session event projection delivered to Discord.
- Browser verification succeeded after the owner authorization fix:
  - `/session-start` returned `Session 1 started.`
  - `/session-end` returned `Session 1 closed.`
- Current live session state: no active session.

The Discord acceptance pass is now complete in the signed-in `Quartermaster Test` server. The first post-restart command attempts timed out while the fresh bot was completing guild command sync; after adding startup/error logging and restarting through the managed wrapper, commands responded normally.

## Deliberate test state still present

- Party Stash contains `Smoke-Test Potion x2`.
- An open smoke-test Loot Drop contains `Live Loot Token x2`.
- Close or claim the test drop during the next live acceptance pass; do not mistake it for campaign data.

## Acceptance checks completed

The live checks completed from the signed-in browser session before the schema-5/runtime-hardening and schema-6 backup slices:

1. `/grant` a clearly named test item and confirm the response and Party Stash projection.
2. `/stash` -> `Browse` -> `Take`; confirm the quantity and event projection update.
3. `/loot-drop` -> `/loot` -> claim a button; confirm the claim and remaining quantity.
4. `/loot-close` the deliberate smoke-test drop and confirm unclaimed items return to Party Stash.
5. Stop and restart the bot, then confirm state projections are recreated or edited rather than duplicated.

Results:

- `/grant` returned `Granted 2 Handoff Acceptance Elixir. Total: 2.` and the Party Stash projection updated.
- `/stash` -> `Browse` -> `Take` returned `You took 1 Handoff Acceptance Elixir. 1 remain.` and the projection changed to x1.
- `/loot-drop` created `Handoff Loot Gem x2`; `/loot` -> claim returned `You claimed 1 Handoff Loot Gem. 1 remain.`.
- `/loot-close` closed both the deliberate `Live Loot Token` smoke-test drop and the new acceptance drop; remaining items returned to Party Stash.
- The managed stop/start path reconnected, synced guild commands, and edited one existing Party Stash projection rather than creating a duplicate.
- Final live state has no open Loot Drops, no pending events, and canonical health `HEALTHY`.

The next live pass must re-verify the new runtime slice:

1. Restart the bot across the schema-6 migration and confirm the canonical state remains intact.
2. Start a session and confirm a `Session N` thread is created in `#session-log`.
3. Confirm session events route into that thread and Party Stash remains pinned/recreated correctly.
4. Exercise `/stash`, `/loot`, and a mutation button after restart to confirm deferred-capable callbacks still acknowledge safely.

The local operational acceptance checks are complete:

- `uv run pytest -q` -> 30 passed.
- `health` -> `HEALTHY` on the canonical database.
- Online backup -> integrity and schema validation passed.
- Restore to a new database -> integrity, schema, health, and export validation passed.
- `maintenance` -> completed with no due drops or retained transient rows to remove.

The live test data intentionally remains visible for cleanup/audit: Party Stash contains `Handoff Loot Gem x1`, `Handoff Acceptance Elixir x1`, `Live Loot Token x2`, and the pre-existing `Smoke-Test Potion x2`; the test claimant has `Handoff Loot Gem x1`.

## Next implementation priorities

After the new live verification:

1. Extend health to cover Discord surface reachability and establish a scheduled backup invocation for the validated backup/retention path.
2. Add adapter acceptance coverage for permissions, pins, rate limits, and acknowledgement latency on the target host.
3. Decide whether backup should remain an operator CLI path or receive a Discord-facing durable `PROCESSING -> COMMITTED/FAILED` workflow; `/export` is covered and backup now has validated secondary-copy support.
4. Then choose the next product slice: treasury/currency transfers or further character ownership support.

The larger optional domains - Your Pack, Journal, Parking Lot, Downtime, faction clocks, rich continuity, and Undo - remain evidence-gated and should not be started yet.

## Restart command

The IDs and database path are persisted, but a new PowerShell process may need to import the user-level environment values before starting:

```powershell
$env:QM_GUILD_ID = [Environment]::GetEnvironmentVariable('QM_GUILD_ID', 'User')
$env:QM_PARTY_INVENTORY_CHANNEL_ID = [Environment]::GetEnvironmentVariable('QM_PARTY_INVENTORY_CHANNEL_ID', 'User')
$env:QM_SESSION_LOG_CHANNEL_ID = [Environment]::GetEnvironmentVariable('QM_SESSION_LOG_CHANNEL_ID', 'User')
$env:QM_DATABASE_PATH = [Environment]::GetEnvironmentVariable('QM_DATABASE_PATH', 'User')
$env:QM_DISCORD_TOKEN = [Environment]::GetEnvironmentVariable('QM_DISCORD_TOKEN', 'User')
uv run python -m quartermaster --db .\quartermaster.sqlite run
```

The token is intentionally not stored in Git, this document, or chat.
