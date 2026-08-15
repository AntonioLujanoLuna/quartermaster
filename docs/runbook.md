# Quartermaster operator runbook

SQLite is canonical. Discord messages are disposable projections. If Discord is unavailable, use the export and health commands below and continue the session without the bot.

## Start, stop, and restart

The supported Windows process wrapper imports the required user-level environment values, starts a hidden supervisor, writes bot stdout/stderr to `logs/`, records the supervisor process ID in `quartermaster.pid`, and restarts an unexpected bot exit:

```powershell
.\scripts\start-quartermaster.ps1
.\scripts\stop-quartermaster.ps1
.\scripts\start-quartermaster.ps1
```

Do not run a second ad-hoc bot process against the same database. The database path is read from `QM_DATABASE_PATH`; the explicit CLI `--db` value does not override that configured environment value when the Discord adapter starts.

Use the stop wrapper for intentional shutdown. It signals the supervisor, stops the full supervised process tree, and removes the PID/stop markers. If the bot exits unexpectedly, inspect `logs/quartermaster.supervisor.log` before restarting manually.

## Health and maintenance

Run these commands from the repository root:

```powershell
uv run python -m quartermaster --db $env:QM_DATABASE_PATH health
uv run python -m quartermaster --db $env:QM_DATABASE_PATH maintenance
uv run python -m quartermaster --db $env:QM_DATABASE_PATH export > .\quartermaster-export.md
```

`health` checks SQLite integrity, schema version, the one-active-session invariant, receipt recovery state, outbox backlog, dead-lettered events, dirty and stuck projections, expired Loot Drops, the last transient-maintenance outcome, backup freshness, and the most recent Discord surface reachability check. A missing, failed, or stale Discord surface check is `DEGRADED`; pass `--discord-surface-max-age-seconds` to change the freshness window. The bot runs startup recovery, transient maintenance, scheduled backups, and surface checks while projection delivery is running; the `maintenance` command remains available for operator-triggered cleanup. It expires due drops and removes terminal receipts and consumed/expired handles after their configured retention periods.

Configured DM administrators can use `/quartermaster` as the compact Discord control surface. It summarizes Party Stash and session state and provides Grant loot, Session, Stash, Open Loot, Treasury, Characters, Export, Backup, and Health actions. The launcher is ephemeral and uses the same authorization and durable workflows as the individual commands.

## Truncated Discord surfaces

Discord refuses any message over 2000 characters, so every Quartermaster surface renders within that limit and says when it had to stop. A pinned Party Stash ending in

```
… and 42 more Party Stash entries not shown here. The Quartermaster export holds the full record.
```

is working as intended, not damaged: the stash outgrew one Discord message. `export` is the complete record — items with the character holding them named, open Loot Drops with what is still unclaimed, and the roster — and `/stash` → `Browse` still reaches individual stacks. The same note can appear on `/loot` and `/characters`. Nothing is dropped from canonical state — only from the disposable projection.

A browse or claim listing can also end with

```
The last 8 entries above have no take control here. Take what is showing and open this again.
```

One Discord view carries twenty-five controls and a stack above one offers both `Take 1` and `Take all`, so a long listing runs out of buttons before it runs out of items. The controls always cover the top of the list; taking what is showing brings the rest into reach.

## The Party Stash projection is not pinned

Pinning needs Manage Messages, and a Discord channel holds fifty pins. When the pin fails the projection still delivers — the message is the surface, the pin is only where it sits — and the log says so on every attempt:

```
WARNING quartermaster.discord_projection: could not pin the Party Stash projection in #party-inventory: 403 Forbidden (error code: 50013): Missing Permissions. Grant Manage Messages, or unpin something if the channel is at its pin limit.
```

Grant the permission or free a pin slot; the surface pins itself on the next delivery, with no operator action and no lost updates in between. `health` will not report this — the projection is current, it is just scrolling with the channel.

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
the previous build, resolve each named player with `/character-lifecycle`, then upgrade. The
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

Configured DM administrators can also invoke `/backup` in the Discord guild. It uses the same validated timestamped path, records a durable `PROCESSING -> COMMITTED/FAILED` receipt, and reports the resulting snapshot filename ephemerally. The Discord command does not attach the SQLite file; use the CLI or the configured backup directory to retrieve it.

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
