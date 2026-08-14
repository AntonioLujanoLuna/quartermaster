# Next-session handoff

Updated: 2026-08-14

## Verified current state

- Python project managed with `uv` and `.venv`.
- SQLite canonical state is at schema version 9 in the current live database and source migrations.
- FAST and DEFERRED interaction receipts are implemented.
- Opaque single-use mutation handles are implemented.
- Bot startup now recovers interrupted DEFERRED receipts and runs transient-state maintenance before accepting interactions.
- Party Stash Grant/Take flows are implemented. A Take transfers ownership to the taker's registered active character and requires one, matching the Loot Drop claim rule; it no longer removes the quantity from the world.
- Relative Take-all staleness now offers explicit confirmation of the current quantity.
- Loot Drops support creation, claim handles, manual close, session-close, and absolute expiry.
- Minimal Session lifecycle is implemented.
- State projection scheduling and FIFO event delivery are implemented; session projections bind to durable per-session Discord threads.
- The `discord.py` adapter exposes guild-scoped `/stash`, `/grant`, `/session-start`, `/session-end`, `/loot`, `/loot-drop`, `/loot-close`, and DM-admin `/export`.
- The `discord.py` adapter also exposes DM/admin `/quartermaster`, an ephemeral launcher summarizing Party Stash/session state with Grant loot, Session, Stash, Loot, Treasury, Characters, Export, Backup, and Health actions backed by the existing workflows.
- Discord projection delivery and bounded local interaction work run asynchronously after database commit, with deadline-aware response deferral at the configured soft deadline. `/export` and admin-only `/backup` use durable `PROCESSING -> COMMITTED/FAILED` workflows; the CLI remains available for operator-triggered backups.
- Online backups create timestamped validated snapshots, optionally copy them to a secondary directory, apply retention, and record their paths and outcome for health checks.
- The projection runner now creates a validated timestamped backup immediately at startup and repeats it on the configurable `QM_BACKUP_INTERVAL_SECONDS` schedule, using the configured primary/off-device directories and retention count.
- Runtime health now records Discord surface reachability for the configured Party Inventory and Session Log channels; missing, failed, or stale checks report `DEGRADED`.
- Local aggregate metrics have been removed: at this table's interaction volume the percentiles could not carry meaning. The schema-8 `local_metric_buckets` table remains in migration history and is now unused; the `metrics` CLI command and launcher Metrics action are gone.
- The first Avrae integration slice is implemented and live in schema 9: provider operations are reserved atomically with DEFERRED receipts, finalized with provider outcomes, and interrupted requests become visible `UNKNOWN` outcomes. No HP, initiative, attack, spell, save, or other mechanics are mirrored in Quartermaster.
- Guild-scoped `/combat` is now the hosted-Avrae fallback front door: it requires an active Quartermaster session and renders native Avrae handoff cards for start, join, turn, attack, cast, check, save, end, and status. It is intentionally read-only until an Avrae-side execution adapter exists.
- A disposable self-hosted Avrae extension scaffold now exists at `integrations/avrae/quartermaster_cog.py` with `/qm-combat-probe`. It preserves the real Avrae interaction identity, calls native `Combat.from_ctx(inter)`, and records a Quartermaster correlation receipt when loaded; it has not yet been loaded or executed in an Avrae deployment.
- Adapter acceptance coverage now includes configured DM-role/manage-guild authorization, pin-permission failures, Discord 429 retry translation, and the adapter FAST-to-DEFERRED acknowledgement path.
- The first treasury/currency slice is implemented: schema-backed integer balances, DM-only treasury adjustments with FAST receipts, non-negative validation, ledger/events, Party Stash/export visibility, and guild-scoped `/treasury` plus `/treasury-adjust` commands. Electrum remains schema-supported but disabled by default.
- Absolute treasury split and treasury-to-active-character transfers are implemented atomically and reject non-active recipients; `/treasury-split` and `/treasury-give` are registered. Per specification 33.1 the indivisible per-denomination remainder stays in the treasury; only the distributed total is debited.
- Explicit character registration and lifecycle transitions are implemented with ACTIVE/DEAD/RETIRED/DEPARTED invariants; `/characters`, `/character-add`, and `/character-lifecycle` are now registered. Lifecycle changes do not move inventory or currency.
- Loot Drop claims now require an active registered character mapped to the Discord actor. Relative treasury split handles snapshot treasury version and active recipient IDs, and require explicit confirmation after either changes.
- Explicit non-active belongings resolution now moves inventory and currency atomically to Party Stash or an active character without changing lifecycle; `/character-resolve` is registered.
- Admin-only Discord `/backup` now runs the validated scheduled-backup path through a durable `PROCESSING -> COMMITTED/FAILED` receipt and reports the resulting snapshot filename ephemerally.
- Session state and event destinations now remain bound to the correct durable session thread across session transitions; recreated Party Stash projections are re-pinned.
- Operational commands now cover `health`, `maintenance`, `backup`, and safe restore validation.
- Managed Windows process wrappers are in `scripts/start-quartermaster.ps1` and `scripts/stop-quartermaster.ps1`.
- Operator procedures for backup/restore and degraded operation are documented in `docs/runbook.md`.
- 69 automated tests pass with `uv`; `ruff check` is clean.

## Correction pass on 2026-08-14

A review found two conservation defects in domain arithmetic. Both are fixed, and both are now covered by
invariant tests rather than example assertions:

- Treasury split debited the full requested amount but distributed only `floor(amount / recipients)` to each
  character, destroying the remainder. Specification 33.1 requires the remainder to stay with the source. The
  previous test asserted the destroying behaviour, so the suite ratified it.
- Party Stash Take decremented the stash and credited nobody, deleting the taken quantity outright, while a
  Loot Drop claim of the same item credited the claimant. Take now transfers ownership through the same helper
  the claim path uses.

`test_currency_operations_conserve_every_denomination` and
`test_item_operations_conserve_quantities_across_owners_and_drops` assert that totals across every owner are
unchanged by any operation that only moves value. Add to them whenever a new movement operation lands.

Also corrected in the same pass:

- `health_report` read canonical state without the connection lock every other reader takes, so a health read
  during an open write transaction could observe uncommitted state. Reads now go through `SQLiteStore.read()`.
- Restoring a backup taken before a later migration failed outright, because validation required exact schema
  equality; restore also opened and migrated the archived snapshot in place. Restore now copies first, migrates
  the copy, and leaves the archive at the schema it was taken at.
- `/characters` and the launcher Characters action bypassed the deadline-aware acknowledgement path used by
  every other read.
- The open-loot renderer emitted a mis-encoded bullet character in Discord.
- The 1344-line `discord_adapter.py` is split into `discord_common`, `discord_views`, `discord_commands`, and
  `discord_adapter`. Ruff caught four names the split dropped from command bodies that no test covers, which is
  itself the coverage signal: the slash command bodies are exercised only by the registration test.

Tooling added: `ruff` and `pytest` as dev dependencies, configuration in `pyproject.toml`, and a GitHub Actions
workflow running both on every pull request.

### Needs live verification

None of the changes below have been exercised against the live guild. Before the next session:

1. `/stash` -> `Browse` -> `Take` as a player whose character is registered, and confirm the item appears under
   that character in `export`.
2. `/stash` -> `Take` as an actor with no registered active character, and confirm the refusal message reads
   acceptably at the table.
3. `/treasury-split` with an amount that does not divide evenly, and confirm the remainder stays in the
   treasury.
4. `/characters` and `/loot`, confirming the acknowledgement timing and the corrected bullet.

## Live Discord setup

- Server: `Quartermaster Test`
- Guild ID: `1536121899388506222`
- Party inventory channel: `#party-inventory` (`1536122527892635809`)
- Session log channel: `#session-log` (`1536122560322863224`)
- Quartermaster application/bot ID: `1536120052871602256`
- The bot can view, send, edit, read, and pin the configured Party Stash projection in `#party-inventory`; the permanent projection is currently pinned.
- The bot is currently being run through the managed Windows supervisor wrapper; the wrapper records the supervisor PID, restarts an unexpected bot exit, and stops the full supervised process tree cleanly.
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

Signed-in browser verification completed on 2026-08-11: `/treasury` was offered by the guild command picker and returned the current treasury response in about 7 seconds end-to-end; the permanent Party Stash pin was visible through Discord's pinned-messages view.

Signed-in browser verification completed on 2026-08-11: after restarting the managed process, admin-only `/backup` returned `Backup completed` with schema 7 validation; the durable receipt was `COMMITTED` and the timestamped snapshot `quartermaster-20260811-085908Z.sqlite` exists. The first attempt occurred while the recorded PID was stale and correctly exposed that the bot was not running.

Managed-process resilience verification completed on 2026-08-11: killing the validated bot process caused the supervisor to restart a new `uv`/Python tree; the intentional stop path removed the supervisor and all descendants without leaving orphan Quartermaster processes, and a clean restart returned health to `HEALTHY`.

Signed-in browser verification completed on 2026-08-11: `/quartermaster` appeared in the guild command picker, returned the expected ephemeral launcher, and `More…` opened the Stash, Open Loot, Treasury, Characters, Export, Backup, and Health actions.

Schema-8 live verification completed on 2026-08-11: the managed restart applied the local metrics migration, health returned `HEALTHY`, `/quartermaster` returned after the deadline-safe acknowledgement change, and the CLI metrics report contained deferred launcher and fast action ACK samples.

Degraded-fallback verification completed on 2026-08-11: with supervised Discord delivery stopped, canonical SQLite still produced a 40-line/2,490-byte export and a validated local schema-8 backup at `backups/quartermaster-fallback-20260811.sqlite`. Health reported `DEGRADED` when the Discord freshness budget was forced to one second, while database, schema, receipts, outbox, projections, and backup checks remained healthy. Restarting the supervisor restored `HEALTHY` with zero pending events, zero dirty projections, and a fresh Discord surface check.

Schema-9 and hosted-Avrae fallback verification completed on 2026-08-11: the managed restart applied schema 9, created a fresh validated backup, returned `HEALTHY` with zero unresolved provider operations, and synchronized the guild command surface. Discord’s signed-in command picker displayed `/combat` with all nine action choices; invoking `Start combat` returned the expected read-only `AVRAE HANDOFF` response because no Quartermaster session was active.

## Deliberate test state still present

- Party Stash contains `Smoke-Test Potion x2`.
- The former smoke-test Loot Drop is closed; its `Live Loot Token x2` is back in Party Stash.
- Treat these items as acceptance fixtures retained for cleanup/audit, not campaign data.

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

- `uv run pytest -q` -> 55 passed.
- `python -m compileall -q src tests` and `git diff --check` passed.
- `health` -> `HEALTHY`; database, schema, backup, receipts, outbox, session, and state-projection checks all pass.
- Online backup -> integrity and schema validation passed.
- Restore to a new database -> integrity, schema, health, and export validation passed.
- `maintenance` -> completed with no due drops or retained transient rows to remove.

The live test data intentionally remains visible for cleanup/audit: Party Stash contains `Handoff Loot Gem x1`, `Handoff Acceptance Elixir x1`, `Live Loot Token x2`, and the pre-existing `Smoke-Test Potion x2`; the test claimant has `Handoff Loot Gem x1`.

## Next implementation priorities

The core implementation and live acceptance checks are complete. The current deployment decision is local-only backup storage: leave `QM_BACKUP_OFF_DEVICE_DIRECTORY` unset. Off-device backup support remains available for a later deployment, but it is not a blocker for this local setup.

The next combat implementation gate is the live Avrae-side extension spike. The Quartermaster boundary, hosted fallback, and source scaffold are ready; no hosted-Avrae command execution is claimed:

1. Decide whether self-hosting/forking Avrae is acceptable and choose the Avrae-side extension shape.
2. Install the Quartermaster core into a self-hosted Avrae environment, load `integrations/avrae/quartermaster_cog.py`, and run `/qm-combat-probe` in a disposable guild with an active Quartermaster session.
3. Prove one authenticated, harmless native Avrae state change through the provider boundary before adding launcher combat controls.
4. Add active/closed combat reference projections only after the Avrae-side reference semantics are proven; do not mirror mechanics.
5. Choose evidence-based latency and freshness budgets from observed play if the current estimates need refinement.

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
