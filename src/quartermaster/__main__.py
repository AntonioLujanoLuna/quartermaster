"""Small operational CLI for the current implementation slice."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import SQLiteStore
from .export import render_export
from .operations import create_backup, health_report, render_health, restore_backup, run_maintenance


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="quartermaster")
    parser.add_argument("--db", default="quartermaster.sqlite", type=Path)
    parser.add_argument("command", choices=["export", "run", "health", "maintenance", "backup", "restore"])
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--off-device-directory", type=Path)
    parser.add_argument("--retention-count", type=int, default=7)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--discord-surface-max-age-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.command == "run":
        from .discord_adapter import run_bot

        environment = dict(os.environ)
        environment.setdefault("QM_DATABASE_PATH", str(args.db))
        run_bot(Settings.from_env(environment))
    elif args.command == "export":
        with SQLiteStore(args.db).open() as store:
            print(render_export(store), end="")
    elif args.command == "health":
        with SQLiteStore(args.db).open() as store:
            print(render_health(
                health_report(
                    store,
                    discord_surface_max_age_seconds=args.discord_surface_max_age_seconds,
                )
            ))
    elif args.command == "maintenance":
        environment = dict(os.environ)
        settings = Settings.from_env(environment)
        with SQLiteStore(args.db).open() as store:
            print(run_maintenance(
                store,
                receipt_retention_seconds=settings.receipt_retention_seconds,
                handle_retention_seconds=settings.handle_retention_seconds,
            ))
    elif args.command == "backup":
        destination = args.destination or (
            Path("backups")
            / f"quartermaster-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%SZ')}.sqlite"
        )
        with SQLiteStore(args.db).open() as store:
            print(create_backup(
                store,
                destination,
                off_device_directory=args.off_device_directory,
                retention_count=args.retention_count,
            ))
    else:
        if args.source is None:
            parser.error("restore requires --source")
        print(restore_backup(args.source, args.db, replace=args.replace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
