"""
Shared pytest fixtures: in-memory SQLite DB with full SaaS schema applied.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Point modules at the temp DB before importing anything
@pytest.fixture(scope="function")
def tmp_db(monkeypatch, tmp_path):
    """Create a fresh in-memory DB with schema applied. Returns (path, conn)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("CLAWSHOW_DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Apply full schema
    schema_dir = Path(__file__).parent.parent / "migrations"
    sql = (schema_dir / "2026-04-17-saas-transformation.sql").read_text()

    # Bootstrap namespaces table (exists on prod, needs creating for tests)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS namespaces (
            namespace TEXT PRIMARY KEY,
            owner_name TEXT,
            owner_email TEXT,
            business_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.executescript(sql)

    # Add SaaS columns (migration script handles idempotence on prod;
    # for tests we just apply them directly)
    new_cols = [
        ("tier", "TEXT DEFAULT 'free'"),
        ("status", "TEXT DEFAULT 'trial'"),
        ("billing_period", "TEXT"),
        ("current_period_start", "TIMESTAMP"),
        ("current_period_end", "TIMESTAMP"),
        ("trial_ends_at", "TIMESTAMP"),
        ("stancer_subscription_id", "TEXT"),
        ("envelope_quota", "INTEGER DEFAULT 5"),
        ("envelopes_used_this_period", "INTEGER DEFAULT 0"),
        ("overage_rate_cents", "INTEGER DEFAULT 0"),
        ("is_founding_customer", "BOOLEAN DEFAULT FALSE"),
        ("price_lock_until", "TIMESTAMP"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(namespaces)")}
    for col, defn in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE namespaces ADD COLUMN {col} {defn}")
    conn.commit()
    yield db_path, conn
    conn.close()
