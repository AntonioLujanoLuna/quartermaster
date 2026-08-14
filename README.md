# Quartermaster

Quartermaster is a SQLite-backed foundation for a Discord D&D table continuity companion.

The current implementation covers the runtime/recovery and projection slices from [the implementation plan](docs/implementation-plan.md): configuration validation, migrations, transaction helpers, FAST and DEFERRED receipts, response-state handling, opaque handles, Session and Party Stash domain operations, fair state projections, FIFO event delivery, recovery, and human-readable export.

## Set up the Python environment

Install [uv](https://docs.astral.sh/uv/), then create and synchronize the project environment:

```powershell
uv venv .venv
uv sync
```

Activate it when working interactively:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Run the checks

```powershell
uv run ruff check .
uv run pytest -q
```

The suite also runs under `uv run python -m unittest discover -s tests -v`. Both checks run in CI on every pull request.

## Run the export CLI

```powershell
$env:PYTHONPATH = "src"
uv run python -m quartermaster --db .\quartermaster.sqlite export
```

## Run the Discord adapter

Configure `QM_GUILD_ID`, `QM_DISCORD_TOKEN`, `QM_PARTY_INVENTORY_CHANNEL_ID`, `QM_SESSION_LOG_CHANNEL_ID`, and optionally `QM_DM_ROLE_IDS` (comma-separated Discord role IDs), then run:

```powershell
$env:QM_GUILD_ID = "..."
$env:QM_DISCORD_TOKEN = "..."
$env:QM_PARTY_INVENTORY_CHANNEL_ID = "..."
$env:QM_SESSION_LOG_CHANNEL_ID = "..."
$env:QM_DATABASE_PATH = ".\quartermaster.sqlite"
uv run python -m quartermaster --db .\quartermaster.sqlite run
```

The adapter registers guild-scoped `/stash`, `/grant`, `/session-start`, and `/session-end` commands. `/stash` opens a component-backed browse/take flow using opaque single-use handles.

Taking from the Party Stash transfers ownership to the taker's registered active character, exactly as a Loot Drop claim does; an actor with no active registered character cannot take.

The Discord transport is intentionally an adapter boundary at this stage. No network calls occur in the core package or in a database transaction. The adapter is split into `discord_common` (services, authorization, response helpers), `discord_views` (components and launcher actions), `discord_commands` (slash command registration), and `discord_adapter` (bot assembly and the runtime loop).

For managed Windows startup, backup/restore, health, maintenance, and degraded operation, see [the operator runbook](docs/runbook.md).
