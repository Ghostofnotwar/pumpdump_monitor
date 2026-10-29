"""Consistent live SQLite backup using the SQLite backup API (never copy a live WAL DB alone)."""

import argparse
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from config import Config, database_path


def backup_database(source: Path, destination: Path) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"Database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError("Backup integrity check failed")
    destination.chmod(0o600)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="SQLite file; defaults to DATABASE_URL")
    parser.add_argument("--output", type=Path, help="Backup file; defaults to DB directory/backups/bot-UTC.db")
    args = parser.parse_args()
    source = database_path(args.database) if args.database else Config.from_env().db_path
    output = args.output or source.parent / "backups" / f"bot-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db"
    try:
        print(backup_database(source, output))
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Backup failed: {exc}") from None


if __name__ == "__main__":
    main()
