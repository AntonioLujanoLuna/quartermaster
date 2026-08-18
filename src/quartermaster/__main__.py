"""Small operational CLI for the current implementation slice."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigurationError, Settings
from .db import SQLiteStore
from .export import render_export
from .operations import (
    create_backup,
    health_report,
    render_health,
    requeue_dead_letter_events,
    restore_backup,
    run_maintenance,
)

# `run` bootstraps a table that has never played, and `restore` writes the file
# it is asked for. Every other command reads a campaign that must already
# exist: opening SQLite creates and migrates whatever path it is given, so a
# command run from the wrong directory, or in a shell that never imported
# QM_DATABASE_PATH, would answer about an empty database it had just made.
# `health` reports on it, `export` prints it, and `backup` rotates a valid,
# empty snapshot into the same directory the real ones are kept in — pruning a
# genuine backup to make room, and leaving a restore candidate that passes
# validation and holds nothing.
_COMMANDS_THAT_MAY_CREATE_THE_DATABASE = frozenset({"run", "restore"})

# `preflight` asks about configuration, the built page, and the serving path,
# and nothing about the campaign. It runs against a throwaway database of its
# own so that it stays safe to run beside a live bot — the one thing the
# architecture forbids is a second writer on the real one.
_COMMANDS_THAT_NEED_NO_DATABASE = frozenset({"preflight"})


def _resolve_database_path(argument: Path | None, environment: dict[str, str]) -> Path:
    if argument is not None:
        return argument
    configured = environment.get("QM_DATABASE_PATH", "").strip()
    return Path(configured).expanduser() if configured else Path("quartermaster.sqlite")


def _optional_path(raw: str) -> Path | None:
    value = raw.strip()
    return Path(value).expanduser() if value else None


def _retention_count(raw: str, default: int) -> int:
    value = raw.strip()
    if not value:
        return default
    if not value.isdigit() or int(value) <= 0:
        raise ConfigurationError("QM_BACKUP_RETENTION_COUNT must be a positive integer")
    return int(value)


def _backup_directory(environment: dict[str, str]) -> Path:
    configured = environment.get("QM_BACKUP_DIRECTORY", "").strip()
    return Path(configured).expanduser() if configured else Path("backups")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    environment = dict(os.environ)
    parser = argparse.ArgumentParser(prog="quartermaster")
    try:
        retention_default = _retention_count(environment.get("QM_BACKUP_RETENTION_COUNT", ""), 7)
    except ConfigurationError as error:
        parser.error(str(error))
    # The default is resolved after parsing so that an explicit --db can be
    # told apart from an absent one: the two disagreeing silently is how an
    # operator ends up reading one database and writing another.
    parser.add_argument("--db", default=None, type=Path)
    parser.add_argument(
        "command",
        choices=[
            "export",
            "run",
            "health",
            "maintenance",
            "backup",
            "restore",
            "requeue-events",
            "preflight",
        ],
    )
    parser.add_argument("--destination-key", help="Limit requeue-events to one outbox destination")
    parser.add_argument("--destination", type=Path)
    parser.add_argument(
        "--off-device-directory",
        type=Path,
        default=_optional_path(environment.get("QM_BACKUP_OFF_DEVICE_DIRECTORY", "")),
    )
    parser.add_argument("--retention-count", type=int, default=retention_default)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--discord-surface-max-age-seconds", type=int, default=300)
    parser.add_argument(
        "--bind",
        default=None,
        help="Serve preflight on this host:port instead of QM_API_BIND",
    )
    args = parser.parse_args()

    database_path = _resolve_database_path(args.db, environment)
    configured_path = environment.get("QM_DATABASE_PATH", "").strip()
    # Only `run` is asked to reconcile the two: the adapter reads its database
    # from configuration, so an explicit --db that disagrees is silently
    # ignored. Every other command uses the path resolved here, which is why
    # the runbook can legitimately restore and inspect a copy at --db while the
    # live campaign stays configured in the environment.
    if args.command == "run" and configured_path and args.db is not None:
        if Path(configured_path).expanduser().resolve() != args.db.expanduser().resolve():
            parser.error(
                f"--db {args.db} disagrees with QM_DATABASE_PATH {configured_path}; "
                "the adapter reads the configured value, so pass one or make them match"
            )
    if (
        args.command not in _COMMANDS_THAT_MAY_CREATE_THE_DATABASE
        and args.command not in _COMMANDS_THAT_NEED_NO_DATABASE
        and not database_path.is_file()
    ):
        parser.error(
            f"no Quartermaster database at {database_path}; "
            "pass --db, or set QM_DATABASE_PATH to the campaign database"
        )
    # Every command that builds Settings should mean the database this
    # invocation resolved, so --db and the configured value cannot part company
    # after the check above.
    environment["QM_DATABASE_PATH"] = str(database_path)

    if args.command == "run":
        from .discord_adapter import run_bot

        run_bot(Settings.from_env(environment))
    elif args.command == "preflight":
        # Imported here for the same reason the adapter imports the API layer
        # lazily: FastAPI and uvicorn are an optional extra, and every other
        # command has to keep working without them.
        from .preflight import render_preflight, run_preflight

        settings = Settings.from_env(environment)
        checks, remaining = run_preflight(settings, bind=args.bind)
        print(render_preflight(checks, remaining))
        return 0 if all(check.passed for check in checks) else 1
    elif args.command == "export":
        with SQLiteStore(database_path).open() as store:
            print(render_export(store), end="")
    elif args.command == "health":
        with SQLiteStore(database_path).open() as store:
            print(render_health(
                health_report(
                    store,
                    discord_surface_max_age_seconds=args.discord_surface_max_age_seconds,
                )
            ))
    elif args.command == "maintenance":
        settings = Settings.from_env(environment)
        with SQLiteStore(database_path).open() as store:
            print(run_maintenance(
                store,
                receipt_retention_seconds=settings.receipt_retention_seconds,
                handle_retention_seconds=settings.handle_retention_seconds,
            ))
    elif args.command == "requeue-events":
        with SQLiteStore(database_path).open() as store:
            requeued = requeue_dead_letter_events(store, destination=args.destination_key)
            print(f"Requeued {requeued} dead-lettered event(s) for delivery.")
    elif args.command == "backup":
        # The directory and retention come from the same configuration the
        # scheduled backup uses, because they rotate one set of files and
        # health reports on whichever snapshot was written last.
        destination = args.destination or (
            _backup_directory(environment)
            / f"quartermaster-{datetime.now(UTC).strftime('%Y%m%d-%H%M%SZ')}.sqlite"
        )
        with SQLiteStore(database_path).open() as store:
            print(create_backup(
                store,
                destination,
                off_device_directory=args.off_device_directory,
                retention_count=args.retention_count,
            ))
    else:
        if args.source is None:
            parser.error("restore requires --source")
        if not args.source.expanduser().is_file():
            parser.error(f"no backup to restore at {args.source}")
        print(restore_backup(args.source, database_path, replace=args.replace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
