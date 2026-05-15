"""Create a consistent SQLite backup for the dashboard database."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.config import get_database_path, load_env, project_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up the dashboard SQLite database.")
    parser.add_argument("--db-path", default=None, help="Source database path. Defaults to DATABASE_PATH.")
    parser.add_argument("--output-dir", default="backups", help="Backup output directory.")
    return parser


def main() -> int:
    load_env()
    args = build_parser().parse_args()
    source = project_path(args.db_path) if args.db_path else get_database_path()
    if not source.is_file():
        raise SystemExit(f"Database not found: {source}")

    output_dir = project_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = output_dir / f"{source.stem}-{stamp}.db"

    source_connection = sqlite3.connect(source)
    try:
        target_connection = sqlite3.connect(target)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()

    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
