"""SQLite setup and connection helpers for PlainWatch."""

from __future__ import annotations

import os
import sqlite3
import argparse
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PROJECT_DIR / "schema.sql"

load_dotenv(PROJECT_DIR / ".env")

DB_PATH = Path(os.getenv("PLAINWATCH_DB_PATH", "plainwatch.db"))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_DIR / DB_PATH



def connect() -> sqlite3.Connection:
    """Open a configured SQLite connection for one short operation."""
    connection = sqlite3.connect(DB_PATH, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def execute_query(query: str, params=()):
    """Execute one query and return rows or the affected-row count.

    SELECT and RETURNING queries return a list of sqlite3.Row objects.
    INSERT, UPDATE, and DELETE queries return their affected-row count.
    """
    connection = connect()
    try:
        cursor = connection.execute(query, params)
        if cursor.description is not None:
            return cursor.fetchall()
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def shutdown_database() -> None:
    """Finish the WAL checkpoint and close the shutdown connection."""
    connection = connect()
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()



def initialize_database() -> None:
    """Create or update the local database using the checked-in schema."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    connection = connect()
    try:
        connection.executescript(schema)
    finally:
        connection.close()

    print(f"Database ready: {DB_PATH}")



def database_is_ready() -> bool:
    """Return whether the required PlainWatch tables are available."""
    required_tables = {
        "topics",
        "feed_items",
        "activity_notification_log",
    }
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name IN (?, ?, ?)",
            tuple(required_tables),
        )
        existing_tables = {row[0] for row in rows}
    finally:
        connection.close()
    return existing_tables == required_tables

def dev_mode():
    """Print the current database tables and their schema information."""
    tables = execute_query(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    )

    if not tables:
        print("No application tables found.")
        return

    for table in tables:
        print(f"\nTABLE: {table['name']}")
        print(table["sql"])
        print("COLUMNS:")

        columns = execute_query(
            f'PRAGMA table_info("{table["name"]}")'
        )
        for column in columns:
            print(
                f"  {column['name']} {column['type']}"
                f" {'NOT NULL' if column['notnull'] else 'NULL'}"
            )

def main():
    parser = argparse.ArgumentParser(description="PlainWatch database utility")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument(
        "--dev",
        action="store_true",
        help="Run development mode",
    )
    commands.add_argument(
        "--initialize",
        action="store_true",
        help="Initialize and verify the database",
    )
    args = parser.parse_args()

    if args.dev:
        dev_mode()
        return

    initialize_database()
    if not database_is_ready():
        raise RuntimeError("Database initialization verification failed.")
    print("Database verification passed.")


if __name__ == "__main__":
    main()
