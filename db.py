"""
ClawShow SQLite data layer.
Database file: data/clawshow.db (gitignored)
All times stored as UTC. Namespace isolation enforced on every query.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, date, timedelta
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
                booking_code    TEXT DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS dine_orders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace        TEXT NOT NULL,
                order_number     TEXT NOT NULL,
                order_type       TEXT DEFAULT 'dine_in',
                items            TEXT DEFAULT '[]',
                total_amount     REAL DEFAULT 0,
                status           TEXT DEFAULT 'pending',
                payment_status   TEXT DEFAULT 'unpaid',
                payment_method   TEXT DEFAULT '',
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (namespace) REFERENCES namespaces(namespace)
            );

            CREATE INDEX IF NOT EXISTS idx_bookings_ns_date ON bookings(namespace, booking_date);
            CREATE INDEX IF NOT EXISTS idx_orders_ns_status ON orders(namespace, status);
            CREATE INDEX IF NOT EXISTS idx_dine_orders_ns_status ON dine_orders(namespace, status);
            CREATE INDEX IF NOT EXISTS idx_dine_orders_ns_date ON dine_orders(namespace, created_at);

            CREATE TABLE IF NOT EXISTS esign_documents (
                id               TEXT PRIMARY KEY,
                namespace        TEXT NOT NULL,
                template         TEXT NOT NULL,
                reference_id     TEXT,
                signer_name      TEXT NOT NULL,
                signer_email     TEXT NOT NULL,
                fields           TEXT DEFAULT '{}',
                status           TEXT DEFAULT 'pending',
                signing_url      TEXT,
                original_pdf_path TEXT,
                rendered_html_path TEXT,
                signed_pdf_path  TEXT,
                callback_url     TEXT,
                signer_ip        TEXT,
                city             TEXT,
                lu_approuve      TEXT,
                signed_at        TEXT,
                language         TEXT DEFAULT 'fr',
                send_email       INTEGER DEFAULT 1,
                created_at       TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_esign_ns ON esign_documents(namespace);
            CREATE INDEX IF NOT EXISTS idx_esign_ref ON esign_documents(reference_id);
        """)
        # Migration: add esign columns if missing (for existing DBs)
        for col, typedef in [
            ("city", "TEXT"),
            ("lu_approuve", "TEXT"),
            ("rendered_html_path", "TEXT"),
        ]:
            try:
                conn.execute(f"SELECT {col} FROM esign_documents LIMIT 1")
            except Exception:
                conn.execute(f"ALTER TABLE esign_documents ADD COLUMN {col} {typedef}")
        # Migration: add booking_code column if missing (for existing DBs)
        try:
            conn.execute("SELECT booking_code FROM bookings LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE bookings ADD COLUMN booking_code TEXT DEFAULT ''")
        # Backfill empty booking_codes (ordered by created_at within each week)
        empty = conn.execute(
            "SELECT id, namespace, booking_date FROM bookings WHERE booking_code = '' OR booking_code IS NULL ORDER BY created_at"
        ).fetchall()
        for row in empty:
            code = _next_booking_code_for_backfill(conn, row["namespace"], row["booking_date"])
            conn.execute("UPDATE bookings SET booking_code = ? WHERE id = ?", (code, row["id"]))
        conn.commit()


def _next_booking_code_for_backfill(conn, namespace: str, booking_date: str) -> str:
    """Backfill helper — same weekly logic but only counts already-coded bookings."""
    monday = _week_start(booking_date)
    sunday = (date.fromisoformat(monday) + timedelta(days=6)).isoformat()
    row = conn.execute(
        "SELECT MAX(CAST(booking_code AS INTEGER)) as mx FROM bookings WHERE namespace = ? AND booking_date >= ? AND booking_date <= ? AND booking_code != '' AND booking_code IS NOT NULL",
        (namespace, monday, sunday),
    ).fetchone()
    nxt = ((row["mx"] or 0) + 1) % 1000
    if nxt == 0:
        nxt = 1
    return f"{nxt:03d}"


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

def _week_start(d: str) -> str:
    """Return Monday's date (ISO) for the week containing date string d."""
    dt = date.fromisoformat(d)
    monday = dt - timedelta(days=dt.weekday())
    return monday.isoformat()


def _next_booking_code(conn, namespace: str, booking_date: str) -> str:
    """Generate 3-digit code, resets weekly (Monday 001). Across all days in the same week."""
    monday = _week_start(booking_date)
    sunday = (date.fromisoformat(monday) + timedelta(days=6)).isoformat()
    row = conn.execute(
        "SELECT MAX(CAST(booking_code AS INTEGER)) as mx FROM bookings WHERE namespace = ? AND booking_date >= ? AND booking_date <= ? AND booking_code != ''",
        (namespace, monday, sunday),
    ).fetchone()
    nxt = ((row["mx"] or 0) + 1) % 1000
    if nxt == 0:
        nxt = 1
    return f"{nxt:03d}"


def create_booking(namespace: str, data: dict) -> dict:
    ensure_namespace(namespace, business_type="restaurant")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items_json = json.dumps(data.get("items", []), ensure_ascii=False)
    booking_date = data.get("booking_date", "")

    with get_conn() as conn:
        code = _next_booking_code(conn, namespace, booking_date)
        cur = conn.execute(
            """INSERT INTO bookings (namespace, customer_name, customer_phone, customer_email,
               booking_date, booking_time, booking_code, type, items, total, notes, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)""",
            (namespace, data.get("customer_name", ""), data.get("customer_phone", ""),
             data.get("customer_email", ""), booking_date,
             data.get("booking_time", ""), code, data.get("type", "emporter"),
             items_json, data.get("total", 0), data.get("notes", ""), now),
        )
        booking_id = cur.lastrowid

    return {"success": True, "booking_id": booking_id, "booking_code": code, "namespace": namespace}


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


def checkin_by_code(namespace: str, booking_code: str, booking_date: str = "") -> dict:
    """Check in a booking by its 3-digit code. If no date given, search all confirmed bookings."""
    with get_conn() as conn:
        if booking_date:
            row = conn.execute(
                "SELECT * FROM bookings WHERE namespace = ? AND booking_code = ? AND booking_date = ? AND status = 'confirmed'",
                (namespace, booking_code, booking_date),
            ).fetchone()
        else:
            # No date — find any confirmed booking with this code (most recent first)
            row = conn.execute(
                "SELECT * FROM bookings WHERE namespace = ? AND booking_code = ? AND status = 'confirmed' ORDER BY booking_date DESC LIMIT 1",
                (namespace, booking_code),
            ).fetchone()
        if not row:
            # Check if it exists but with a different status
            any_row = conn.execute(
                "SELECT status, customer_name FROM bookings WHERE namespace = ? AND booking_code = ? ORDER BY booking_date DESC LIMIT 1",
                (namespace, booking_code),
            ).fetchone()
            if any_row and any_row["status"] == "completed":
                return {"success": True, "already": True, "booking_code": booking_code, "customer_name": any_row["customer_name"], "status": "completed", "message": f"#{booking_code} ({any_row['customer_name']}) already checked in"}
            if any_row:
                return {"success": False, "error": f"#{booking_code} ({any_row['customer_name']}) has status '{any_row['status']}', cannot check in"}
            return {"success": False, "error": f"Booking code {booking_code} not found"}
        booking = _row_to_dict(row)
        conn.execute("UPDATE bookings SET status = 'completed' WHERE id = ?", (booking["id"],))
    return {
        "success": True,
        "booking_id": booking["id"],
        "booking_code": booking_code,
        "customer_name": booking["customer_name"],
        "items": booking.get("items", []),
        "total": booking.get("total", 0),
        "status": "completed",
    }


def cancel_by_code(namespace: str, booking_code: str, booking_date: str = "") -> dict:
    """Cancel a booking by its 3-digit code."""
    with get_conn() as conn:
        if booking_date:
            row = conn.execute(
                "SELECT * FROM bookings WHERE namespace = ? AND booking_code = ? AND booking_date = ? AND status = 'confirmed'",
                (namespace, booking_code, booking_date),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM bookings WHERE namespace = ? AND booking_code = ? AND status = 'confirmed' ORDER BY booking_date DESC LIMIT 1",
                (namespace, booking_code),
            ).fetchone()
        if not row:
            any_row = conn.execute(
                "SELECT status, customer_name FROM bookings WHERE namespace = ? AND booking_code = ? ORDER BY booking_date DESC LIMIT 1",
                (namespace, booking_code),
            ).fetchone()
            if any_row:
                return {"success": False, "error": f"#{booking_code} ({any_row['customer_name']}) has status '{any_row['status']}', cannot cancel"}
            return {"success": False, "error": f"Booking code {booking_code} not found"}
        booking = _row_to_dict(row)
        conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking["id"],))
    return {
        "success": True,
        "booking_id": booking["id"],
        "booking_code": booking_code,
        "customer_name": booking["customer_name"],
        "status": "cancelled",
    }


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


# ---------------------------------------------------------------------------
# Dine-in order CRUD
# ---------------------------------------------------------------------------

def _next_dine_order_number(conn, namespace: str) -> str:
    """Generate C001-style order number, resets daily."""
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM dine_orders WHERE namespace = ? AND created_at >= ?",
        (namespace, today),
    ).fetchone()
    nxt = (row["cnt"] or 0) + 1
    return f"C{nxt:03d}"


def create_dine_order(namespace: str, data: dict) -> dict:
    ensure_namespace(namespace, business_type="restaurant")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items_json = json.dumps(data.get("items", []), ensure_ascii=False)
    order_type = data.get("order_type", "dine_in")
    payment_method = data.get("payment_method", "online")
    # Determine initial payment_status based on payment method
    if payment_method == "card_counter":
        payment_status = "pending_counter"
    elif payment_method == "cash":
        payment_status = "pending_cash"
    else:
        payment_status = "unpaid"

    with get_conn() as conn:
        order_number = _next_dine_order_number(conn, namespace)
        cur = conn.execute(
            """INSERT INTO dine_orders (namespace, order_number, order_type, items,
               total_amount, status, payment_status, payment_method, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (namespace, order_number, order_type, items_json,
             data.get("total_amount", 0), payment_status, payment_method, now, now),
        )
        order_id = cur.lastrowid

    return {"success": True, "order_id": order_id, "order_number": order_number, "namespace": namespace}


def query_dine_orders(namespace: str, status: str = "", limit: int = 100) -> list[dict]:
    today = date.today().isoformat()
    sql = "SELECT * FROM dine_orders WHERE namespace = ? AND created_at >= ?"
    params: list = [namespace, today]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_dine_order_status(namespace: str, order_id: int, status: str) -> dict:
    valid = ("pending", "preparing", "ready", "picked")
    if status not in valid:
        return {"success": False, "error": f"Invalid status. Must be one of: {', '.join(valid)}"}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dine_orders SET status = ?, updated_at = ? WHERE id = ? AND namespace = ?",
            (status, now, order_id, namespace),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Order {order_id} not found"}
    return {"success": True, "order_id": order_id, "status": status}


# Auto-init on import
init_tables()


def update_dine_order_payment(namespace: str, order_id: int, payment_id: str, provider: str = "stancer") -> dict:
    """Record payment_id and provider on a dine order."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        # Migrate: add payment_id column if missing
        try:
            conn.execute("SELECT payment_id FROM dine_orders LIMIT 1")
        except Exception:
            conn.execute("ALTER TABLE dine_orders ADD COLUMN payment_id TEXT DEFAULT ''")
            conn.execute("ALTER TABLE dine_orders ADD COLUMN payment_provider TEXT DEFAULT 'stancer'")
        cur = conn.execute(
            "UPDATE dine_orders SET payment_id = ?, payment_provider = ?, updated_at = ? WHERE id = ? AND namespace = ?",
            (payment_id, provider, now, order_id, namespace),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Order {order_id} not found"}
    return {"success": True, "order_id": order_id, "payment_id": payment_id}


def update_dine_order_payment_status(namespace: str, order_id: int, payment_status: str) -> dict:
    """Mark a dine order as paid (or other payment status)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dine_orders SET payment_status = ?, updated_at = ? WHERE id = ? AND namespace = ?",
            (payment_status, now, order_id, namespace),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Order {order_id} not found"}
    return {"success": True, "order_id": order_id, "payment_status": payment_status}

def query_dine_orders_history(namespace: str, date: str = "", status: str = "", limit: int = 200) -> list[dict]:
    """Return dine orders for a specific date (all statuses, including picked)."""
    if not date:
        date = __import__("datetime").date.today().isoformat()
    # Match on the date part of created_at
    sql = "SELECT * FROM dine_orders WHERE namespace = ? AND created_at >= ? AND created_at < ?"
    params: list = [namespace, date, (
        __import__("datetime").date.fromisoformat(date) +
        __import__("datetime").timedelta(days=1)
    ).isoformat()]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]

# ---------------------------------------------------------------------------
# eSign CRUD
# ---------------------------------------------------------------------------

def create_esign_document(doc_id: str, namespace: str, template: str, signer_name: str,
                           signer_email: str, fields: dict, signing_url: str,
                           original_pdf_path: str, rendered_html_path: str = "",
                           reference_id: str = "", callback_url: str = "",
                           language: str = "fr", send_email: bool = True,
                           total_pages: int = 1, signature_positions=None,
                           initial_status: str = "pending") -> dict:
    ensure_namespace(namespace)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO esign_documents
               (id, namespace, template, reference_id, signer_name, signer_email,
                fields, status, signing_url, original_pdf_path, rendered_html_path,
                callback_url, language, send_email)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, namespace, template, reference_id, signer_name, signer_email,
             json.dumps(fields, ensure_ascii=False), initial_status, signing_url, original_pdf_path,
             rendered_html_path, callback_url, language, int(send_email)),
        )
        # Set total_pages and signature_positions via UPDATE (columns may be added by migration)
        try:
            sig_pos_json = json.dumps(signature_positions) if signature_positions else None
            conn.execute(
                "UPDATE esign_documents SET total_pages=?, signature_positions=? WHERE id=?",
                (total_pages, sig_pos_json, doc_id),
            )
        except Exception:
            pass  # columns may not exist on older schema
    return {"success": True, "document_id": doc_id}


def get_esign_document(doc_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM esign_documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("fields"), str):
        try:
            d["fields"] = json.loads(d["fields"])
        except Exception:
            pass
    return d


def update_esign_s3_url(doc_id: str, s3_url: str) -> None:
    """Persist S3 URL for the signed PDF."""
    with get_conn() as conn:
        conn.execute("UPDATE esign_documents SET s3_url=? WHERE id=?", (s3_url, doc_id))


def complete_esign_document(doc_id: str, signed_pdf_path: str, signer_ip: str,
                              city: str = "", lu_approuve: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE esign_documents
               SET status='completed', signed_pdf_path=?, signer_ip=?, signed_at=?,
                   city=?, lu_approuve=?
               WHERE id=?""",
            (signed_pdf_path, signer_ip, now, city, lu_approuve, doc_id),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Document {doc_id} not found"}
    return {"success": True, "document_id": doc_id, "signed_at": now}


def decline_esign_document(doc_id: str, signer_ip: str, reason: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE esign_documents SET status='declined', signer_ip=?, signed_at=?, lu_approuve=? WHERE id=?",
            (signer_ip, now, f"DECLINED: {reason}", doc_id),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Document {doc_id} not found"}
    return {"success": True, "document_id": doc_id, "status": "declined", "declined_at": now}


def confirm_dine_order_payment(namespace: str, order_id: int, payment_method: str, amount_received: float) -> dict:
    """Admin confirms counter payment (card or cash). Marks order as paid."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dine_orders SET payment_status = 'paid', payment_method = ?, updated_at = ? WHERE id = ? AND namespace = ?",
            (payment_method, now, order_id, namespace),
        )
        if cur.rowcount == 0:
            return {"success": False, "error": f"Order {order_id} not found"}
    return {"success": True, "order_id": order_id, "payment_status": "paid", "amount_received": amount_received}


# ---------------------------------------------------------------------------
# eSign V2 — multi-signer, audit, OTP

def _ensure_esign_v2_schema():
    """Add V2 columns and tables if they don't exist yet."""
    with get_conn() as conn:
        # Multi-signer table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS esign_signers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'student',
                signer_name TEXT,
                signer_email TEXT,
                signing_order INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                token TEXT UNIQUE,
                signature_png TEXT,
                paraphe_png TEXT,
                paraphes TEXT,
                city TEXT,
                lu_approuve_png TEXT,
                decline_reason TEXT,
                signer_ip TEXT,
                notified_at TEXT,
                viewed_at TEXT,
                signed_at TEXT,
                otp_verified_at TEXT,
                FOREIGN KEY (document_id) REFERENCES esign_documents(id)
            )
        """)
        # Audit log
        conn.execute("""
            CREATE TABLE IF NOT EXISTS esign_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                signer_id INTEGER,
                action TEXT NOT NULL,
                detail TEXT,
                ip_address TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (document_id) REFERENCES esign_documents(id)
            )
        """)
        # OTP table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS esign_otp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                signer_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                verified_at TEXT,
                attempts INTEGER DEFAULT 0,
                locked_until TEXT,
                FOREIGN KEY (document_id) REFERENCES esign_documents(id),
                FOREIGN KEY (signer_id) REFERENCES esign_signers(id)
            )
        """)
        # Add V2 columns to esign_documents if missing
        try:
            conn.execute("ALTER TABLE esign_documents ADD COLUMN total_pages INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE esign_documents ADD COLUMN s3_url TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE esign_documents ADD COLUMN expiration_days INTEGER DEFAULT 30")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE esign_documents ADD COLUMN reminder_frequency TEXT DEFAULT 'EVERY_THIRD_DAY'")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE esign_documents ADD COLUMN last_reminder_at TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE esign_documents ADD COLUMN reference_id TEXT")
        except Exception:
            pass
        # Add otp_verified_at to esign_signers if missing
        try:
            conn.execute("ALTER TABLE esign_signers ADD COLUMN otp_verified_at TEXT")
        except Exception:
            pass

_ensure_esign_v2_schema()


def create_esign_signer(document_id: str, role: str, signer_name: str, signer_email: str,
                         signing_order: int, token: str) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO esign_signers
               (document_id, role, signer_name, signer_email, signing_order, token, status)
               VALUES (?,?,?,?,?,?,'pending')""",
            (document_id, role, signer_name, signer_email, signing_order, token),
        )
    return {"id": cur.lastrowid, "token": token}


def get_signer_by_token(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM esign_signers WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def get_signers_by_document(doc_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM esign_signers WHERE document_id = ? ORDER BY signing_order",
            (doc_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_signer_viewed(signer_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE esign_signers SET status='signing', viewed_at=? WHERE id=? AND status='pending'",
            (now, signer_id),
        )


def update_signer_signed(signer_id: int, signer_ip: str, signature_png: str = "",
                          paraphe_png: str = "", paraphes: dict = None, city: str = "",
                          lu_approuve_png: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """UPDATE esign_signers
               SET status='signed', signer_ip=?, signed_at=?,
                   signature_png=?, paraphe_png=?, paraphes=?,
                   city=?, lu_approuve_png=?
               WHERE id=?""",
            (signer_ip, now, signature_png, paraphe_png,
             json.dumps(paraphes) if paraphes else None,
             city, lu_approuve_png, signer_id),
        )
    return {"success": True, "signer_id": signer_id, "signed_at": now}


def update_signer_status(signer_id: int, status: str, reason: str = "") -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE esign_signers SET status=?, decline_reason=?, signed_at=? WHERE id=? ",
            (status, reason, now if status == "declined" else None, signer_id),
        )
    return {"success": True, "signer_id": signer_id, "status": status}


def update_document_status(doc_id: str, status: str, completed_at: str = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if completed_at or status == "completed":
            conn.execute(
                "UPDATE esign_documents SET status=?, completed_at=? WHERE id=?",
                (status, completed_at or now, doc_id),
            )
        else:
            conn.execute("UPDATE esign_documents SET status=? WHERE id=?", (status, doc_id))
    return {"success": True, "doc_id": doc_id, "status": status}


def log_esign_audit(doc_id: str, action: str, detail: dict = None,
                     signer_id: int = None, ip_address: str = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO esign_audit_log (document_id, signer_id, action, detail, ip_address) VALUES (?,?,?,?,?)",
            (doc_id, signer_id, action, json.dumps(detail) if detail else None, ip_address),
        )


def get_pending_esign_documents() -> list:
    """Return all documents still waiting for signatures (including student_signed)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM esign_documents WHERE status IN "
            "('student_signing','school_signing','pending','student_signed')"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("fields"), str):
            try:
                d["fields"] = json.loads(d["fields"])
            except Exception:
                pass
        result.append(d)
    return result


def update_esign_last_reminder(doc_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE esign_documents SET last_reminder_at=? WHERE id=?", (now, doc_id))


# ---------------------------------------------------------------------------
# OTP functions

def create_otp(document_id: str, signer_id: int, code: str) -> dict:
    """Create a new OTP record, invalidating any prior pending ones."""
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=10)).isoformat()
    with get_conn() as conn:
        # Invalidate old codes
        conn.execute(
            "UPDATE esign_otp SET verified_at='invalidated' WHERE document_id=? AND signer_id=? AND verified_at IS NULL",
            (document_id, signer_id),
        )
        cur = conn.execute(
            "INSERT INTO esign_otp (document_id, signer_id, code, expires_at) VALUES (?,?,?,?)",
            (document_id, signer_id, code, expires_at),
        )
    return {"id": cur.lastrowid, "expires_at": expires_at}


def get_active_otp(document_id: str, signer_id: int) -> dict | None:
    """Get the most recent unverified, unexpired, unlocked OTP."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM esign_otp
               WHERE document_id=? AND signer_id=? AND verified_at IS NULL
               AND expires_at > ? AND (locked_until IS NULL OR locked_until < ?)
               ORDER BY id DESC LIMIT 1""",
            (document_id, signer_id, now, now),
        ).fetchone()
    return dict(row) if row else None


def verify_otp(otp_id: int, document_id: str, signer_id: int) -> None:
    """Mark OTP as verified and update signer otp_verified_at."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE esign_otp SET verified_at=? WHERE id=?", (now, otp_id))
        conn.execute("UPDATE esign_signers SET otp_verified_at=? WHERE id=?", (now, signer_id))


def increment_otp_attempts(otp_id: int, current_attempts: int) -> dict:
    """Increment attempt counter; lock if >= 5 attempts."""
    new_attempts = current_attempts + 1
    now = datetime.now(timezone.utc)
    locked_until = None
    if new_attempts >= 5:
        locked_until = (now + timedelta(minutes=30)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE esign_otp SET attempts=?, locked_until=? WHERE id=?",
            (new_attempts, locked_until, otp_id),
        )
    return {"attempts": new_attempts, "locked": locked_until is not None, "locked_until": locked_until}


def is_otp_verified(document_id: str, signer_id: int) -> bool:
    """Check if signer has a valid otp_verified_at on their record."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT otp_verified_at FROM esign_signers WHERE id=? AND document_id=?",
            (signer_id, document_id),
        ).fetchone()
    if row and row[0] and row[0] != "invalidated":
        return True
    return False


def is_otp_locked(document_id: str, signer_id: int) -> dict:
    """Check if signer is locked out due to too many failed OTP attempts."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT locked_until FROM esign_otp
               WHERE document_id=? AND signer_id=? AND verified_at IS NULL
               AND locked_until IS NOT NULL AND locked_until > ?
               ORDER BY id DESC LIMIT 1""",
            (document_id, signer_id, now),
        ).fetchone()
    if row:
        return {"locked": True, "locked_until": row[0]}
    return {"locked": False}
