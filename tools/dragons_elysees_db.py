"""
Dragons Elysées 龙城酒楼 — dedicated SQLite database layer.
DB path: <repo-root>/data/dragons-elysees.db

Tables:
    customers            — registered customers
    balance_transactions — cashback ledger (balance = SUM(amount))
    orders               — dine-in orders (DRG-XXX)
    otp_codes            — 6-digit email OTP codes (10 min expiry)
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

DE_DB_PATH = Path(__file__).parent.parent / "data" / "dragons-elysees.db"


@contextmanager
def get_conn():
    """SQLite connection with WAL mode and FK enforcement."""
    conn = sqlite3.connect(str(DE_DB_PATH), timeout=10)
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


def init_tables() -> None:
    """Create all tables and indexes (idempotent)."""
    DE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS customers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT UNIQUE NOT NULL,
                name       TEXT,
                phone      TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login DATETIME
            );

            CREATE TABLE IF NOT EXISTS balance_transactions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id      INTEGER NOT NULL,
                type             TEXT NOT NULL,
                amount           REAL NOT NULL,
                description      TEXT,
                related_order_id INTEGER,
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number   TEXT UNIQUE NOT NULL,
                customer_id    INTEGER,
                items          TEXT NOT NULL,
                subtotal       REAL NOT NULL,
                cashback_used  REAL DEFAULT 0,
                total_paid     REAL NOT NULL,
                cashback_earned REAL DEFAULT 0,
                payment_method TEXT,
                payment_id     TEXT,
                status         TEXT DEFAULT 'pending',
                table_number   TEXT,
                note           TEXT,
                created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );

            CREATE TABLE IF NOT EXISTS otp_codes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL,
                code       TEXT NOT NULL,
                expires_at DATETIME NOT NULL,
                used       INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_orders_status   ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_date     ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_balance_customer ON balance_transactions(customer_id);
            CREATE INDEX IF NOT EXISTS idx_otp_email       ON otp_codes(email, used);
        """)


# ── Customers ──────────────────────────────────────────────────────────────

def get_or_create_customer(email: str) -> dict:
    """Upsert customer by email, update last_login, return dict."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO customers (email, created_at) VALUES (?, ?)",
            (email, now),
        )
        conn.execute(
            "UPDATE customers SET last_login = ? WHERE email = ?",
            (now, email),
        )
        row = conn.execute(
            "SELECT id, email, name, phone, created_at FROM customers WHERE email = ?",
            (email,),
        ).fetchone()
    return dict(row) if row else {}


def get_customer_by_id(customer_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, phone, created_at FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


# ── OTP ────────────────────────────────────────────────────────────────────

def save_otp(email: str, code: str) -> None:
    """Store a new OTP with 10-minute expiry."""
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO otp_codes (email, code, expires_at) VALUES (?, ?, ?)",
            (email, code, expires_at),
        )


def verify_and_consume_otp(email: str, code: str) -> bool:
    """Return True and mark used if code is valid, unused, and unexpired."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id FROM otp_codes
               WHERE email = ? AND code = ? AND used = 0 AND expires_at > ?
               ORDER BY id DESC LIMIT 1""",
            (email, code, now),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE otp_codes SET used = 1 WHERE id = ?", (row["id"],))
    return True


# ── Balance ────────────────────────────────────────────────────────────────

def get_balance(customer_id: int) -> dict:
    """Return {balance, total_earned, total_used} for a customer."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                COALESCE(SUM(amount), 0)                                          AS balance,
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount  ELSE 0 END), 0)   AS total_earned,
                COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0)   AS total_used
               FROM balance_transactions WHERE customer_id = ?""",
            (customer_id,),
        ).fetchone()
    return {
        "balance":       round(row["balance"], 2),
        "total_earned":  round(row["total_earned"], 2),
        "total_used":    round(row["total_used"], 2),
    }


def add_balance_transaction(
    customer_id: int,
    tx_type: str,
    amount: float,
    description: str,
    related_order_id: int | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO balance_transactions
               (customer_id, type, amount, description, related_order_id)
               VALUES (?, ?, ?, ?, ?)""",
            (customer_id, tx_type, amount, description, related_order_id),
        )


def get_transactions(customer_id: int, limit: int = 20, offset: int = 0) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, type, amount, description, related_order_id, created_at
               FROM balance_transactions WHERE customer_id = ?
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (customer_id, limit, offset),
        ).fetchall()
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM balance_transactions WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
    return {
        "transactions": [dict(r) for r in rows],
        "total": total_row["cnt"],
    }


# ── Orders ──────────────────────────────────────────────────────────────────

def _next_order_number() -> str:
    """Generate next DRG-XXX number (global, never resets)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT order_number FROM orders ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return "DRG-001"
    last = row["order_number"]  # e.g. "DRG-042"
    try:
        n = int(last.split("-")[1]) + 1
    except (IndexError, ValueError):
        n = 1
    return f"DRG-{n:03d}"


def _row_to_order(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["items"] = json.loads(d["items"])
    except Exception:
        pass
    return d


def create_order(data: dict) -> dict | None:
    """Create order with optional cashback deduction. Returns full order dict."""
    items = data.get("items", [])
    subtotal = round(sum(float(i.get("qty", 1)) * float(i.get("price", 0)) for i in items), 2)
    customer_id = data.get("customer_id")
    cashback_use = float(data.get("cashback_use", 0) or 0)
    payment_method = data.get("payment_method", "stancer")
    table_number = str(data.get("table_number", "") or "")
    note = str(data.get("note", "") or "")

    cashback_used = 0.0
    if customer_id and cashback_use > 0:
        bal = get_balance(int(customer_id))
        cashback_used = round(min(cashback_use, bal["balance"]), 2)

    total_paid = round(subtotal - cashback_used, 2)
    order_number = _next_order_number()
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO orders
               (order_number, customer_id, items, subtotal, cashback_used,
                total_paid, payment_method, status, table_number, note,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (
                order_number,
                customer_id,
                json.dumps(items, ensure_ascii=False),
                subtotal,
                cashback_used,
                total_paid,
                payment_method,
                table_number,
                note,
                now,
                now,
            ),
        )
        order_id = cur.lastrowid

    if customer_id and cashback_used > 0:
        add_balance_transaction(
            int(customer_id),
            "payment",
            -cashback_used,
            f"Paiement par solde - Commande {order_number}",
            order_id,
        )

    return get_order_by_id(order_id)


def get_order_by_id(order_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _row_to_order(row) if row else None


def get_order_by_payment_id(payment_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE payment_id = ?", (payment_id,)
        ).fetchone()
    return _row_to_order(row) if row else None


def query_orders(
    status: str = "",
    date: str = "",
    customer_id: int | None = None,
) -> list[dict]:
    sql = "SELECT * FROM orders WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if date:
        sql += " AND DATE(created_at) = ?"
        params.append(date)
    if customer_id is not None:
        sql += " AND customer_id = ?"
        params.append(customer_id)
    sql += " ORDER BY id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_order(r) for r in rows]


def update_order_status(order_id: int, new_status: str) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, order_id),
        )
    return get_order_by_id(order_id)


def update_order_payment(order_id: int, payment_id: str, payment_method: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET payment_id = ?, payment_method = ?, updated_at = ? WHERE id = ?",
            (payment_id, payment_method, now, order_id),
        )


def apply_cashback(order_id: int) -> None:
    """Credit 10% cashback to customer if eligible (total_paid >= €15, not already applied)."""
    order = get_order_by_id(order_id)
    if not order:
        return
    if order.get("cashback_earned", 0) > 0:
        return  # already applied
    customer_id = order.get("customer_id")
    total_paid = float(order.get("total_paid", 0))
    if not customer_id or total_paid < 15.0:
        return
    cashback = round(total_paid * 0.10, 2)
    add_balance_transaction(
        int(customer_id),
        "cashback",
        cashback,
        f"10% cashback - Commande {order['order_number']}",
        order_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET cashback_earned = ?, updated_at = ? WHERE id = ?",
            (cashback, now, order_id),
        )
