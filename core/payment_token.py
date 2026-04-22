"""
Payment token management — P0-1 (Week 1).

Each token maps to one (plan_id, installment_no) pair.
Token lifetime: 30 days (configurable via PAYMENT_TOKEN_EXPIRY_DAYS).
One-time use: marked used_at when /pay is called, paid_at when gateway confirms.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_EXPIRY_DAYS = int(os.environ.get("PAYMENT_TOKEN_EXPIRY_DAYS", "30"))

_SCHEMA_PATH = Path(__file__).parent.parent / "migrations" / "002_billing_payment_tokens.sql"


def _ensure_table() -> None:
    from storage.billing_db import get_conn
    sql = _SCHEMA_PATH.read_text()
    with get_conn() as conn:
        conn.executescript(sql)


def generate_payment_token() -> str:
    """Generate a URL-safe 32-character token string."""
    return secrets.token_urlsafe(32)


def create_token_record(
    plan_id: str,
    installment_no: int,
    namespace: str,
    amount: float,
    currency: str = "EUR",
    custom_expiry_days: Optional[int] = None,
) -> str:
    """
    Create a token record and return the token string.

    Args:
        plan_id: billing plan this token authorizes
        installment_no: which installment (1-based; 0 = subscription first auth)
        namespace: customer namespace
        amount: amount to charge in this installment
        currency: ISO currency code
        custom_expiry_days: override default 30-day expiry
    """
    _ensure_table()
    token = generate_payment_token()
    expiry_days = custom_expiry_days or _EXPIRY_DAYS
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()

    from storage.billing_db import get_conn
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO billing_payment_tokens
               (token, plan_id, installment_no, namespace, amount, currency, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (token, plan_id, installment_no, namespace, amount, currency, expires_at),
        )
    return token


def get_token_record(token: str) -> dict | None:
    """Return the raw token record, or None if not found."""
    _ensure_table()
    from storage.billing_db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM billing_payment_tokens WHERE token = ?", (token,)
        ).fetchone()
    return dict(row) if row else None


def validate_token(token: str) -> dict | None:
    """
    Validate a token. Returns the record dict if valid (exists + not expired).

    The returned dict includes a synthetic "token_status" key:
      "valid"   — valid and unused
      "used"    — payment was initiated (used_at set)
      "paid"    — fully paid (paid_at set)

    Returns None for invalid/expired tokens.

    Note: does NOT reject used tokens — caller decides whether to allow re-entry
    (e.g. GET payment page still works after use, but POST /pay is rejected).
    """
    record = get_token_record(token)
    if not record:
        return None
    expires = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        return None

    if record.get("paid_at"):
        record["token_status"] = "paid"
    elif record.get("used_at"):
        record["token_status"] = "used"
    else:
        record["token_status"] = "valid"

    # Track access
    from storage.billing_db import get_conn
    with get_conn() as conn:
        conn.execute(
            """UPDATE billing_payment_tokens
               SET last_accessed_at = CURRENT_TIMESTAMP,
                   access_count = access_count + 1
               WHERE token = ?""",
            (token,),
        )

    return record


def mark_token_used(token: str, gateway_payment_id: str) -> None:
    """Mark token as used (payment initiated). Prevents replay."""
    from storage.billing_db import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE billing_payment_tokens SET used_at=?, gateway_payment_id=? WHERE token=?",
            (now, gateway_payment_id, token),
        )


def mark_token_paid(token: str) -> None:
    """Mark token as paid (payment confirmed by gateway webhook)."""
    from storage.billing_db import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE billing_payment_tokens SET paid_at=? WHERE token=?",
            (now, token),
        )


def get_token_for_installment(plan_id: str, installment_no: int) -> str | None:
    """Return an existing valid token for this installment, or None."""
    _ensure_table()
    from storage.billing_db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            """SELECT token, expires_at FROM billing_payment_tokens
               WHERE plan_id=? AND installment_no=?
               ORDER BY created_at DESC LIMIT 1""",
            (plan_id, installment_no),
        ).fetchone()
    if not row:
        return None
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        return None
    return row["token"]
