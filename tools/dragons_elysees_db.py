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
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                email            TEXT UNIQUE NOT NULL,
                name             TEXT,
                phone            TEXT,
                referral_code    VARCHAR(8),
                referred_by_code VARCHAR(8),
                created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_login       DATETIME
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

            CREATE TABLE IF NOT EXISTS order_status_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id   INTEGER NOT NULL,
                status     TEXT NOT NULL,
                changed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                changed_by TEXT,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            );

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
            );

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
            );

            CREATE TABLE IF NOT EXISTS fraud_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_phone  TEXT,
                referred_phone  TEXT,
                reason          TEXT,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_orders_date      ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_balance_customer ON balance_transactions(customer_id);
            CREATE INDEX IF NOT EXISTS idx_otp_email        ON otp_codes(email, used);
            CREATE INDEX IF NOT EXISTS idx_history_order    ON order_status_history(order_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_code ON customers(referral_code);
            CREATE INDEX IF NOT EXISTS idx_referred_by      ON customers(referred_by_code);
            CREATE INDEX IF NOT EXISTS idx_re_referrer      ON referral_events(referrer_customer_id);
            CREATE INDEX IF NOT EXISTS idx_re_referred      ON referral_events(referred_customer_id);
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
            "SELECT id, email, name, phone, referral_code, referred_by_code, created_at FROM customers WHERE id = ?",
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

    # Delivery fields
    order_type = data.get("order_type", "dine_in") or "dine_in"
    delivery_address = str(data.get("delivery_address", "") or "")
    delivery_phone = str(data.get("delivery_phone", "") or "")
    delivery_instructions = str(data.get("delivery_instructions", "") or "")
    delivery_fee = round(float(data.get("delivery_fee", 0) or 0), 2)

    # Guest fields (non-registered users)
    guest_name = str(data.get("guest_name", "") or "")
    guest_phone = str(data.get("guest_phone", "") or "")

    cashback_used = 0.0
    if customer_id and cashback_use > 0:
        bal = get_balance(int(customer_id))
        cashback_used = round(min(cashback_use, bal["balance"], subtotal + delivery_fee), 2)

    total_paid = round(subtotal + delivery_fee - cashback_used, 2)

    # Determine effective payment method and initial status
    if total_paid == 0:
        effective_payment_method = "balance"
        initial_status = "paid"
    elif cashback_used > 0:
        effective_payment_method = "mixed"
        initial_status = "pending"
    else:
        effective_payment_method = payment_method
        initial_status = "pending"

    order_number = _next_order_number()
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO orders
               (order_number, customer_id, items, subtotal, cashback_used,
                total_paid, payment_method, status, table_number, note,
                order_type, delivery_address, delivery_phone,
                delivery_instructions, delivery_fee,
                guest_name, guest_phone,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_number,
                customer_id,
                json.dumps(items, ensure_ascii=False),
                subtotal,
                cashback_used,
                total_paid,
                effective_payment_method,
                initial_status,
                table_number,
                note,
                order_type,
                delivery_address,
                delivery_phone,
                delivery_instructions,
                delivery_fee,
                guest_name,
                guest_phone,
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


def get_order_by_number(order_number: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_number = ?", (order_number,)
        ).fetchone()
    return _row_to_order(row) if row else None


def query_orders(
    status: str = "",
    date: str = "",
    customer_id: int | None = None,
    order_number: str = "",
    order_type: str = "",
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
    if order_number:
        sql += " AND order_number = ?"
        params.append(order_number)
    if order_type:
        sql += " AND order_type = ?"
        params.append(order_type)
    sql += " ORDER BY id DESC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_order(r) for r in rows]


def get_stats(date: str) -> dict:
    """Return daily stats for admin panel."""
    orders = query_orders(date=date)
    active = [o for o in orders if o.get("status") != "cancelled"]
    revenue = round(sum(float(o.get("total_paid", 0)) for o in active), 2)
    cashback_issued = round(sum(float(o.get("cashback_earned", 0)) for o in orders), 2)
    by_type = {
        "dine_in": sum(1 for o in active if o.get("order_type", "dine_in") == "dine_in"),
        "delivery": sum(1 for o in active if o.get("order_type") == "delivery"),
        "balance_only": sum(1 for o in active if o.get("payment_method") == "balance"),
    }
    status_counts: dict = {}
    for o in orders:
        s = o.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    return {
        "total_orders": len(orders),
        "revenue": revenue,
        "cashback_issued": cashback_issued,
        "by_type": by_type,
        "by_status": status_counts,
    }


def update_order_status(order_id: int, new_status: str, changed_by: str = "system") -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, order_id),
        )
        conn.execute(
            "INSERT INTO order_status_history (order_id, status, changed_at, changed_by) VALUES (?, ?, ?, ?)",
            (order_id, new_status, now, changed_by),
        )
    return get_order_by_id(order_id)


def get_order_tracking(order_number: str) -> dict | None:
    """Return order dict with status_history list for public tracking."""
    order = get_order_by_number(order_number)
    if not order:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, changed_at, changed_by FROM order_status_history WHERE order_id = ? ORDER BY id ASC",
            (order["id"],),
        ).fetchall()
    order["status_history"] = [dict(r) for r in rows]
    return order


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


# ── Referral & Google Review ────────────────────────────────────────────────

def generate_unique_referral_code() -> str:
    import random, string
    chars = string.ascii_uppercase + string.digits
    with get_conn() as conn:
        while True:
            code = "".join(random.choices(chars, k=8))
            if not conn.execute(
                "SELECT 1 FROM customers WHERE referral_code = ?", (code,)
            ).fetchone():
                return code


def get_customer_by_referral_code(code: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, phone, referral_code, referred_by_code FROM customers WHERE referral_code = ?",
            (code.upper(),),
        ).fetchone()
    return dict(row) if row else None


def get_customer_by_phone(phone: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, phone, referral_code, referred_by_code FROM customers WHERE phone = ?",
            (phone,),
        ).fetchone()
    return dict(row) if row else None


def create_customer_with_referral(data: dict) -> int:
    """Insert new customer with referral fields, return new id."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO customers (email, name, phone, referral_code, referred_by_code, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                data.get("email", ""),
                data.get("name", ""),
                data.get("phone", ""),
                data["referral_code"],
                data.get("referred_by_code"),
                now,
            ),
        )
        return cur.lastrowid


def has_triggered_referral_reward(referred_customer_id: int) -> bool:
    """Return True if this customer already triggered a referral_first_order event."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM referral_events WHERE referred_customer_id = ? AND event_type = 'referral_first_order'",
            (referred_customer_id,),
        ).fetchone()
    return row is not None


def get_customer_last_order(customer_id: int) -> dict | None:
    """Return most recent paid order for this customer."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, order_number, total_paid, status FROM orders WHERE customer_id = ? AND status = 'paid' ORDER BY id DESC LIMIT 1",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def create_referral_event(data: dict) -> int:
    """Insert referral event. Raises sqlite3.IntegrityError on UNIQUE duplicate."""
    import json as _json
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO referral_events
               (referrer_customer_id, referred_customer_id, order_ref, order_amount,
                commission_amount, event_type, status, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["referrer_customer_id"],
                data["referred_customer_id"],
                data.get("order_ref"),
                data.get("order_amount"),
                data["commission_amount"],
                data["event_type"],
                data.get("status", "credited"),
                _json.dumps(data["metadata"]) if data.get("metadata") else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def get_referral_stats(customer_id: int) -> dict:
    with get_conn() as conn:
        code_row = conn.execute(
            "SELECT referral_code FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if not code_row or not code_row["referral_code"]:
            return {"total_referred": 0, "total_earned": 0.0, "pending_referrals": 0}
        code = code_row["referral_code"]

        total_referred = conn.execute(
            "SELECT COUNT(*) AS cnt FROM customers WHERE referred_by_code = ?", (code,)
        ).fetchone()["cnt"]

        earned_row = conn.execute(
            """SELECT COALESCE(SUM(commission_amount), 0) AS s FROM referral_events
               WHERE referrer_customer_id = ? AND event_type = 'referral_first_order' AND status = 'credited'""",
            (customer_id,),
        ).fetchone()
        total_earned = float(earned_row["s"] or 0)

        pending = conn.execute(
            """SELECT COUNT(*) AS cnt FROM customers c
               WHERE c.referred_by_code = ?
               AND NOT EXISTS (
                   SELECT 1 FROM referral_events e
                   WHERE e.referred_customer_id = c.id AND e.event_type = 'referral_first_order'
               )""",
            (code,),
        ).fetchone()["cnt"]

    return {
        "total_referred": total_referred,
        "total_earned": round(total_earned, 2),
        "pending_referrals": pending,
    }


def count_referrals_by_referrer_recent(referrer_customer_id: int, hours: int = 24) -> int:
    """Count referral_first_order events created by this referrer in the last N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM referral_events
               WHERE referrer_customer_id = ? AND event_type = 'referral_first_order' AND created_at > ?""",
            (referrer_customer_id, cutoff),
        ).fetchone()
    return row["cnt"] if row else 0


def log_fraud_attempt(referrer_phone: str, referred_phone: str, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fraud_log (referrer_phone, referred_phone, reason, created_at) VALUES (?, ?, ?, ?)",
            (referrer_phone, referred_phone, reason, now),
        )


def insert_google_review(data: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO google_reviews
               (google_reviewer_name, google_reviewer_email, rating, review_text,
                review_date, matched_customer_id, matched_at, raw_email_content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("google_reviewer_name"),
                data.get("google_reviewer_email"),
                data.get("rating"),
                data.get("review_text"),
                data.get("review_date", now),
                data.get("matched_customer_id"),
                data.get("matched_at", now),
                data.get("raw_email_content"),
                now,
            ),
        )
        return cur.lastrowid


def mark_review_rewarded(review_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE google_reviews SET rewarded = 1 WHERE id = ?", (review_id,))


def get_customers_with_orders_list(limit: int = 50) -> list[dict]:
    """Return customers who have at least one order, with their last paid order amount."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.phone, c.email, c.referral_code,
                      (SELECT o.total_paid FROM orders o
                       WHERE o.customer_id = c.id AND o.status = 'paid'
                       ORDER BY o.id DESC LIMIT 1) AS last_order_amount
               FROM customers c
               WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)
               ORDER BY c.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
