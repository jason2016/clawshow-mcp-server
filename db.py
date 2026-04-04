"""
ClawShow SQLite data layer.
Database file: data/clawshow.db (gitignored)
All times stored as UTC. Namespace isolation enforced on every query.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "data" / "clawshow.db"


def _ensure_dir():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    """Context manager for a SQLite connection with WAL mode and foreign keys."""
    _ensure_dir()
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_tables():
    """Create all tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS namespaces (
                namespace    TEXT PRIMARY KEY,
                owner_name   TEXT,
                owner_email  TEXT,
                business_type TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace       TEXT NOT NULL,
                customer_name   TEXT NOT NULL,
                customer_phone  TEXT,
                customer_email  TEXT,
                booking_date    TEXT NOT NULL,
                booking_time    TEXT NOT NULL,
                type            TEXT DEFAULT 'emporter',
                items           TEXT DEFAULT '[]',
                total           REAL DEFAULT 0,
                notes           TEXT DEFAULT '',
                status          TEXT DEFAULT 'confirmed',
                created_at      TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (namespace) REFERENCES namespaces(namespace)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace        TEXT NOT NULL,
                customer_name    TEXT,
                customer_email   TEXT,
                customer_phone   TEXT,
                type             TEXT DEFAULT 'invoice',
                items            TEXT DEFAULT '[]',
                total            REAL DEFAULT 0,
                currency         TEXT DEFAULT 'EUR',
                status           TEXT DEFAULT 'pending',
                payment_method   TEXT DEFAULT 'none',
                stripe_payment_id TEXT,
                notes            TEXT DEFAULT '',
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (namespace) REFERENCES namespaces(namespace)
            );

            CREATE INDEX IF NOT EXISTS idx_bookings_ns_date ON bookings(namespace, booking_date);
            CREATE INDEX IF NOT EXISTS idx_orders_ns_status ON orders(namespace, status);
        """)


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------

def ensure_namespace(namespace: str, owner_name: str = "", owner_email: str = "", business_type: str = ""):
    """Create namespace if it doesn't exist (zero-registration)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO namespaces (namespace, owner_name, owner_email, business_type) VALUES (?, ?, ?, ?)",
            (namespace, owner_name, owner_email, business_type),
        )


# ---------------------------------------------------------------------------
# Booking CRUD
# ---------------------------------------------------------------------------

def create_booking(namespace: str, data: dict) -> dict:
    ensure_namespace(namespace, business_type="restaurant")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items_json = json.dumps(data.get("items", []), ensure_ascii=False)

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO bookings (namespace, customer_name, customer_phone, customer_email,
               booking_date, booking_time, type, items, total, notes, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)""",
            (namespace, data.get("customer_name", ""), data.get("customer_phone", ""),
             data.get("customer_email", ""), data.get("booking_date", ""),
             data.get("booking_time", ""), data.get("type", "emporter"),
             items_json, data.get("total", 0), data.get("notes", ""), now),
        )
        booking_id = cur.lastrowid

    return {"success": True, "booking_id": booking_id, "namespace": namespace}


def query_bookings(namespace: str, date: str = "", status: str = "", limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM bookings WHERE namespace = ?"
    params: list = [namespace]
    if date:
        sql += " AND booking_date = ?"
        params.append(date)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY booking_date DESC, booking_time DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def booking_summary(namespace: str, date: str) -> dict:
    bookings = query_bookings(namespace, date=date, status="confirmed")
    total_bookings = len(bookings)

    items_count: dict[str, int] = {}
    for b in bookings:
        for item in b.get("items", []):
            name = item.get("name", "unknown")
            qty = item.get("qty", 1)
            items_count[name] = items_count.get(name, 0) + qty

    total_amount = sum(b.get("total", 0) for b in bookings)

    return {
        "namespace": namespace,
        "date": date,
        "total_bookings": total_bookings,
        "total_amount": total_amount,
        "items_summary": [{"name": k, "qty": v} for k, v in sorted(items_count.items(), key=lambda x: -x[1])],
    }


def update_booking_status(namespace: str, booking_id: int, status: str) -> dict:
    valid = ("confirmed", "completed", "cancelled", "no_show")
    if status not in valid:
        return {"success": False, "error": f"Invalid status. Must be one of: {', '.join(valid)}"}
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET status = ? WHERE id = ? AND namespace = ?",
            (status, booking_id, namespace),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Booking {booking_id} not found in namespace '{namespace}'"}
    return {"success": True, "booking_id": booking_id, "status": status}


def cancel_booking(namespace: str, booking_id: int) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND namespace = ? AND status = 'confirmed'",
            (booking_id, namespace),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Booking {booking_id} not found or already cancelled"}
    return {"success": True, "booking_id": booking_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if "items" in d and isinstance(d["items"], str):
        try:
            d["items"] = json.loads(d["items"])
        except Exception:
            pass
    return d


# Auto-init on import
init_tables()
