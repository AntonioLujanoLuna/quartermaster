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
uv run python -m quartermaster --db $env:QM_DATABASE_PATH metrics
uv run python -m quartermaster --db $env:QM_DATABASE_PATH maintenance
uv run python -m quartermaster --db $env:QM_DATABASE_PATH export > .\quartermaster-export.md
```

`health` checks SQLite integrity, schema version, the one-active-session invariant, receipt recovery state, outbox backlog, dirty projections, expired Loot Drops, the last transient-maintenance outcome, backup freshness, and the most recent Discord surface reachability check. A missing, failed, or stale Discord surface check is `DEGRADED`; pass `--discord-surface-max-age-seconds` to change the freshness window. The bot runs startup recovery, transient maintenance, scheduled backups, and surface checks while projection delivery is running; the `maintenance` command remains available for operator-triggered cleanup. It expires due drops and removes terminal receipts and consumed/expired handles after their configured retention periods.

Configured DM administrators can use `/quartermaster` as the compact Discord control surface. It summarizes Party Stash and session state and provides Grant loot, Session, Stash, Open Loot, Treasury, Characters, Export, Backup, Health, and Metrics actions. The launcher is ephemeral and uses the same authorization and durable workflows as the individual commands.

`metrics` reports local aggregate ACK latency and projection dirty-duration percentiles for the recent window. It stores hourly bounded histograms only; actor and interaction identities are not retained. Use `--metric-window-hours` to change the default 24-hour window.

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

## Degraded operation

If `health` is `DEGRADED` or `FAILED`:

1. Stop the bot if it is repeatedly failing or the database check is not `OK`.
2. Run `export` from the canonical database and keep the output with the session notes.
3. If the database is readable, take a backup and validate it.
4. Continue play manually; do not edit SQLite tables by hand.
5. Inspect `logs/quartermaster.stderr.log`, fix configuration or Discord permissions, then restart once.
6. Re-run `health` and confirm the Party Stash and session projections converge without duplicate messages.

If SQLite integrity fails, do not run maintenance or attempt an in-place repair. Preserve the database files, restore a validated backup to a new path, and compare exports before resuming.
