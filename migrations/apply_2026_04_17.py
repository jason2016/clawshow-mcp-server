"""
Idempotent migration: 2026-04-17 SaaS transformation.

- ALTERs namespaces table (skips existing columns)
- Creates new tables via the .sql file
- Marks existing namespaces as enterprise/founding
- Safe to re-run: CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE
"""
import os
import sqlite3
import sys

DB_PATH = os.environ.get(
    "CLAWSHOW_DB_PATH",
    "/opt/clawshow-mcp-server/data/clawshow.db"
)
SQL_PATH = os.path.join(os.path.dirname(__file__), "2026-04-17-saas-transformation.sql")

NEW_NAMESPACE_COLUMNS = [
    ("tier",                        "TEXT DEFAULT 'free'"),
    ("status",                      "TEXT DEFAULT 'trial'"),
    ("billing_period",              "TEXT"),
    ("current_period_start",        "TIMESTAMP"),
    ("current_period_end",          "TIMESTAMP"),
    ("trial_ends_at",               "TIMESTAMP"),
    ("stancer_subscription_id",     "TEXT"),
    ("envelope_quota",              "INTEGER DEFAULT 5"),
    ("envelopes_used_this_period",  "INTEGER DEFAULT 0"),
    ("overage_rate_cents",          "INTEGER DEFAULT 0"),
    ("is_founding_customer",        "BOOLEAN DEFAULT FALSE"),
    ("price_lock_until",            "TIMESTAMP"),
    ("updated_at",                  "TIMESTAMP"),
]


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

    # 1. ALTER namespaces (idempotent)
    print("\n-- Step 1: Extending namespaces table --")
    for col_name, col_def in NEW_NAMESPACE_COLUMNS:
        if not column_exists(cursor, "namespaces", col_name):
            print(f"  ADD COLUMN namespaces.{col_name}")
            cursor.execute(f"ALTER TABLE namespaces ADD COLUMN {col_name} {col_def}")
        else:
            print(f"  SKIP      namespaces.{col_name} (already exists)")
    conn.commit()

    # 2. Run SQL file for new tables
    print("\n-- Step 2: Creating new tables --")
    with open(SQL_PATH) as f:
        sql = f.read()
    cursor.executescript(sql)
    conn.commit()
    print("  SQL file applied.")

    # 3. Mark all existing namespaces as enterprise/founding with 2-year price lock
    print("\n-- Step 3: Marking existing namespaces as founding --")
    cursor.execute("""
        UPDATE namespaces
        SET tier = 'enterprise',
            status = 'active',
            envelope_quota = -1,
            is_founding_customer = 1,
            price_lock_until = '2028-04-17',
            updated_at = CURRENT_TIMESTAMP
        WHERE tier IS NULL OR tier = 'free'
    """)
    updated = cursor.rowcount
    conn.commit()
    print(f"  Updated {updated} namespace(s) to enterprise/founding.")

    # 4. Verify
    print("\n-- Step 4: Verification --")
    cursor.execute(".tables" if False else "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"  Tables: {', '.join(tables)}")

    expected_new = {"users", "user_namespaces", "usage_events", "api_keys", "login_tokens", "sessions"}
    missing = expected_new - set(tables)
    if missing:
        print(f"  ERROR: Missing tables: {missing}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    cursor.execute("SELECT COUNT(*) FROM esign_documents")
    doc_count = cursor.fetchone()[0]
    print(f"  esign_documents count: {doc_count} (expected 13)")

    cursor.execute("SELECT namespace, tier, status, is_founding_customer FROM namespaces")
    print("  Namespaces:")
    for row in cursor.fetchall():
        print(f"    {row[0]}: tier={row[1]}, status={row[2]}, founding={row[3]}")

    conn.close()
    print("\nMigration applied successfully.")


if __name__ == "__main__":
    main()
