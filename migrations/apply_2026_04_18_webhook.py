"""
Idempotent migration: 2026-04-18 namespace webhook config.

Adds webhook_url column to namespaces table.
Safe to re-run.
"""
import os
import sqlite3
import sys

DB_PATH = os.environ.get(
    "CLAWSHOW_DB_PATH",
    "/opt/clawshow-mcp-server/data/clawshow.db"
)


def column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def main():
    print(f"Database: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if not column_exists(cursor, "namespaces", "webhook_url"):
        cursor.execute("ALTER TABLE namespaces ADD COLUMN webhook_url TEXT")
        print("  + namespaces.webhook_url added")
    else:
        print("  ~ namespaces.webhook_url already exists, skipped")

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    main()
