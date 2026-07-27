"""SQLite lifecycle command."""

import argparse
from pathlib import Path

from trade_agent.adapters.sqlite import SQLiteDatabase, backup_database, restore_database
from trade_agent.core.config import AppSettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trade-agent-db")
    parser.add_argument("--database", type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init")
    subcommands.add_parser("health")
    backup = subcommands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    restore = subcommands.add_parser("restore")
    restore.add_argument("source", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = AppSettings()
    database_path = args.database or settings.database.path
    database = SQLiteDatabase(
        database_path,
        busy_timeout_ms=settings.database.busy_timeout_ms,
    )
    if args.command == "init":
        database.initialize()
        print(database.health())
    elif args.command == "health":
        print(database.health())
    elif args.command == "backup":
        backup_database(database_path, args.destination)
    elif args.command == "restore":
        restore_database(args.source, database_path)


__all__ = ["main"]
