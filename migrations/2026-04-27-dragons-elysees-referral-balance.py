"""
Migration: 龙城餐厅推荐 + Google评价奖励系统
Date: 2026-04-27
Scope: dragons-elysees namespace only

Adds:
  - customers.referral_code (VARCHAR 8, unique)
  - customers.referred_by_code (VARCHAR 8)
  - referral_events table
  - google_reviews table
  - fraud_log table
  - generates referral codes for all existing customers
"""
import sqlite3
import random
import string
from pathlib import Path

DE_DB_PATH = Path(__file__).parent.parent / "data" / "dragons-elysees.db"


def upgrade(db_path: str = None):
    path = db_path or str(DE_DB_PATH)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()

    # Check if customers table exists (skip ALTER on fresh installs — init_tables handles it)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customers'")
    if not cursor.fetchone():
        print("Fresh install detected — init_tables() will create schema with new columns. Skipping migration.")
        conn.close()
        return

    # 1. Add new columns to customers (ALTER TABLE is idempotent via try/except)
    for col_def in [
        "ALTER TABLE customers ADD COLUMN referral_code VARCHAR(8)",
        "ALTER TABLE customers ADD COLUMN referred_by_code VARCHAR(8)",
    ]:
        try:
            cursor.execute(col_def)
        except sqlite3.OperationalError as e:
            print(f"  skip (already exists): {e}")

    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_code ON customers(referral_code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_referred_by ON customers(referred_by_code)")

    # 2. referral_events table
    # UNIQUE(referred_customer_id, event_type) prevents duplicate triggers per customer per type
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS referral_events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_customer_id  INTEGER NOT NULL,
            referred_customer_id  INTEGER NOT NULL,
            order_ref             TEXT,
            order_amount          REAL,
            commission_amount     REAL NOT NULL,
            event_type            TEXT NOT NULL,
            status                TEXT DEFAULT 'credited',
            metadata              TEXT,
            created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (referrer_customer_id) REFERENCES customers(id),
            FOREIGN KEY (referred_customer_id) REFERENCES customers(id),
            UNIQUE(referred_customer_id, event_type)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_re_referrer ON referral_events(referrer_customer_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_re_referred ON referral_events(referred_customer_id)")

    # 3. google_reviews table (demo mode: manually inserted; production: IMAP listener after 5/19)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS google_reviews (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            google_reviewer_name  TEXT,
            google_reviewer_email TEXT,
            rating                INTEGER,
            review_text           TEXT,
            review_date           DATETIME,
            matched_customer_id   INTEGER,
            matched_at            DATETIME,
            rewarded              INTEGER DEFAULT 0,
            raw_email_content     TEXT,
            created_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (matched_customer_id) REFERENCES customers(id)
        )
    """)

    # 4. fraud_log table (lightweight logging, no FK needed)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fraud_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_phone  TEXT,
            referred_phone  TEXT,
            reason          TEXT,
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 5. Generate referral codes for all existing customers that don't have one
    cursor.execute("SELECT id FROM customers WHERE referral_code IS NULL")
    existing = cursor.fetchall()
    count = 0
    for (cid,) in existing:
        code = _gen_unique_code(cursor)
        cursor.execute("UPDATE customers SET referral_code = ? WHERE id = ?", (code, cid))
        count += 1

    conn.commit()
    conn.close()
    print(f"Migration complete. Generated {count} referral codes for existing customers.")


def _gen_unique_code(cursor) -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(chars, k=8))
        cursor.execute("SELECT 1 FROM customers WHERE referral_code = ?", (code,))
        if not cursor.fetchone():
            return code


if __name__ == "__main__":
    upgrade()
