# Next-session handoff

Updated: 2026-08-11

## Verified current state

- Python project managed with `uv` and `.venv`.
- SQLite canonical state is at schema version 7.
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
- The projection runner now creates a validated timestamped backup immediately at startup and repeats it on the configurable `QM_BACKUP_INTERVAL_SECONDS` schedule, using the configured primary/off-device directories and retention count.
- Runtime health now records Discord surface reachability for the configured Party Inventory and Session Log channels; missing, failed, or stale checks report `DEGRADED`.
- Adapter acceptance coverage now includes configured DM-role/manage-guild authorization, pin-permission failures, Discord 429 retry translation, and the adapter FAST-to-DEFERRED acknowledgement path.
- The first treasury/currency slice is implemented: schema-backed integer balances, DM-only treasury adjustments with FAST receipts, non-negative validation, ledger/events, Party Stash/export visibility, and guild-scoped `/treasury` plus `/treasury-adjust` commands. Electrum remains schema-supported but disabled by default.
- Session state and event destinations now remain bound to the correct durable session thread across session transitions; recreated Party Stash projections are re-pinned.
- Operational commands now cover `health`, `maintenance`, `backup`, and safe restore validation.
- Managed Windows process wrappers are in `scripts/start-quartermaster.ps1` and `scripts/stop-quartermaster.ps1`.
- Operator procedures for backup/restore and degraded operation are documented in `docs/runbook.md`.
- 46 automated tests pass with `uv`.

## Live Discord setup

- Server: `Quartermaster Test`
- Guild ID: `1536121899388506222`
- Party inventory channel: `#party-inventory` (`1536122527892635809`)
- Session log channel: `#session-log` (`1536122560322863224`)
- Quartermaster application/bot ID: `1536120052871602256`
- The bot can view, send, edit, read, and pin the configured Party Stash projection in `#party-inventory`; the permanent projection is currently pinned.
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
- Post-restart verification succeeded for Session 2:
  - `/session-start` created a `Session 2` thread.
  - `/grant` delivered its event to the Session 2 thread.
  - `/stash` -> `Browse` -> `Take` returned `You took 1 Live Restart Token. 0 remain.`.
  - `/loot` returned `There are no open Loot Drops.`.
  - `/session-end` returned `Session 2 closed.`.
- The permanent Party Stash projection is pinned after the narrow channel-permission fix, and the projection worker cleared its dirty state.
- Current live session state: no active session.

The post-restart command, session-thread, and pin checks are complete in the signed-in `Quartermaster Test` server. The adapter response helper was corrected after `discord.py` raised on an explicit `view=None`; the patched flow now responds normally. Operational health is `HEALTHY` after the projection retry.

The scheduled-operations verification completed on 2026-08-11: the managed process restarted with the new runner, Gateway connection and guild command synchronization succeeded, an automatic timestamped backup was created, and `health` reported `HEALTHY` with `discord_surfaces: OK`.

## Deliberate test state still present

- Party Stash contains `Smoke-Test Potion x2`.
- The former smoke-test Loot Drop is closed; its `Live Loot Token x2` is back in Party Stash.
- Treat these items as acceptance fixtures, not campaign data, until the live verification is complete.

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
- Final live state has no open Loot Drops or pending events, and canonical state remains intact; operational health is `HEALTHY`.

The post-restart runtime and routing pass completed successfully:

1. The bot restarted across the schema-6 migration with canonical state intact.
2. Session 2 created a `Session 2` thread in `#session-log`.
3. Session events routed into that thread and Party Stash remained pinned.
4. `/stash`, `/loot`, and a mutation button acknowledged safely after restart.
5. Session 2 state and events did not route into the previous session thread.

The local operational acceptance checks are complete:

- `uv run pytest -q` -> 46 passed.
- `python -m compileall -q src tests` and `git diff --check` passed.
- `health` -> `HEALTHY`; database, schema, backup, receipts, outbox, session, and state-projection checks all pass.
- Online backup -> integrity and schema validation passed.
- Restore to a new database -> integrity, schema, health, and export validation passed.
- `maintenance` -> completed with no due drops or retained transient rows to remove.

The live test data intentionally remains visible for cleanup/audit: Party Stash contains `Handoff Loot Gem x1`, `Handoff Acceptance Elixir x1`, `Live Loot Token x2`, and the pre-existing `Smoke-Test Potion x2`; the test claimant has `Handoff Loot Gem x1`.

## Next implementation priorities

After the completed live verification:

1. Perform target-host measurements for acknowledgement latency and live permission/pin reachability; the local adapter acceptance coverage is now in place.
2. Decide whether backup should also receive a Discord-facing durable `PROCESSING -> COMMITTED/FAILED` workflow; `/export` is covered and backup now has scheduled and operator-initiated validated paths.
3. Continue Phase 4 with absolute/relative treasury split semantics and atomic Give/transfer recipient-set validation; keep character lifecycle changes separate from possession movement.

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
