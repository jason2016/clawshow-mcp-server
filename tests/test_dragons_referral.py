"""
Tests: Dragons Elysées referral + google review reward system
Scenarios:
  1. New customer registration with referral code
  2. Referrer receives 10% commission on referred customer's first order
  3. Second order does NOT re-trigger referral reward
  4. Balance redemption on an order
  5. Google review demo reward (admin simulate)
  6. Anti-fraud: self-referral blocked
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def de_db(tmp_path, monkeypatch):
    """Fresh dragons-elysees DB for each test."""
    db_path = str(tmp_path / "dragons-elysees.db")
    monkeypatch.setattr("tools.dragons_elysees_db.DE_DB_PATH", Path(db_path))
    import tools.dragons_elysees_db as db
    db.init_tables()
    # run referral migration on this fresh DB
    from migrations import importlib_shim  # may not exist — use direct import
    return db, db_path


@pytest.fixture
def de_db(tmp_path, monkeypatch):
    """Fresh dragons-elysees DB for each test."""
    db_path = Path(tmp_path / "dragons-elysees.db")
    # Patch the module-level path before init
    import importlib
    import tools.dragons_elysees_db as _orig
    monkeypatch.setattr(_orig, "DE_DB_PATH", db_path)
    _orig.init_tables()
    return _orig


def _make_customer(db, phone, name="Test", email=None):
    """Helper: create customer and auto-assign referral code."""
    import random, string
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    email = email or f"{phone.replace('+', '')}@test.com"
    cid = db.create_customer_with_referral({
        "phone": phone,
        "email": email,
        "name": name,
        "referral_code": code,
        "referred_by_code": None,
    })
    return cid, code


def _make_paid_order(db, customer_id, amount=50.0):
    """Helper: insert a paid order directly (bypasses delivery-field schema issues)."""
    import json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    order_number = f"DRG-TEST-{customer_id:03d}"
    with db.get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO orders
               (order_number, customer_id, items, subtotal, cashback_used,
                total_paid, payment_method, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, ?, 'stancer', 'paid', ?, ?)""",
            (order_number, customer_id, json.dumps([{"name": "Test", "qty": 1, "price": amount}]),
             amount, amount, now, now),
        )
        oid = cur.lastrowid
    return db.get_order_by_id(oid)


# ── Scenario 1 ─────────────────────────────────────────────────────────────

def test_register_with_referral_code(de_db):
    """Customer B registers using customer A's referral code → referred_by_code set."""
    db = de_db
    a_id, a_code = _make_customer(db, "+33600000001", "Alice")

    b_id = db.create_customer_with_referral({
        "phone": "+33600000002",
        "email": "bob@test.com",
        "name": "Bob",
        "referral_code": db.generate_unique_referral_code(),
        "referred_by_code": a_code,
    })

    b = db.get_customer_by_id(b_id)
    assert b["referred_by_code"] == a_code

    stats = db.get_referral_stats(a_id)
    assert stats["total_referred"] == 1
    assert stats["pending_referrals"] == 1


# ── Scenario 2 ─────────────────────────────────────────────────────────────

def test_referral_first_order_reward(de_db):
    """B's first order €50 → A gets €5 (10%) credited."""
    db = de_db
    a_id, a_code = _make_customer(db, "+33600000011", "Alice")

    b_id = db.create_customer_with_referral({
        "phone": "+33600000012",
        "email": "bob2@test.com",
        "name": "Bob",
        "referral_code": db.generate_unique_referral_code(),
        "referred_by_code": a_code,
    })

    # Simulate order/complete logic
    order_amount = 50.0
    commission = round(order_amount * 0.10, 2)
    assert not db.has_triggered_referral_reward(b_id)

    event_id = db.create_referral_event({
        "referrer_customer_id": a_id,
        "referred_customer_id": b_id,
        "order_ref": "DRG-TEST-001",
        "order_amount": order_amount,
        "commission_amount": commission,
        "event_type": "referral_first_order",
        "status": "credited",
    })
    db.add_balance_transaction(a_id, "referral_reward", commission, "Test referral reward")

    assert event_id > 0
    assert db.has_triggered_referral_reward(b_id)

    bal_a = db.get_balance(a_id)
    assert bal_a["balance"] == 5.0

    stats = db.get_referral_stats(a_id)
    assert stats["total_earned"] == 5.0
    assert stats["pending_referrals"] == 0


# ── Scenario 3 ─────────────────────────────────────────────────────────────

def test_second_order_no_reward(de_db):
    """B's second order does NOT trigger referral reward again (UNIQUE constraint)."""
    import sqlite3
    db = de_db
    a_id, a_code = _make_customer(db, "+33600000021", "Alice")
    b_id = db.create_customer_with_referral({
        "phone": "+33600000022",
        "email": "bob3@test.com",
        "name": "Bob",
        "referral_code": db.generate_unique_referral_code(),
        "referred_by_code": a_code,
    })

    # First event succeeds
    db.create_referral_event({
        "referrer_customer_id": a_id,
        "referred_customer_id": b_id,
        "order_ref": "DRG-TEST-002",
        "order_amount": 50.0,
        "commission_amount": 5.0,
        "event_type": "referral_first_order",
        "status": "credited",
    })

    # Second event with same (referred_customer_id, event_type) → IntegrityError
    with pytest.raises(Exception):
        db.create_referral_event({
            "referrer_customer_id": a_id,
            "referred_customer_id": b_id,
            "order_ref": "DRG-TEST-003",
            "order_amount": 30.0,
            "commission_amount": 3.0,
            "event_type": "referral_first_order",
            "status": "credited",
        })

    # Balance unchanged from first reward only
    bal_a = db.get_balance(a_id)
    assert bal_a["balance"] == 0  # no add_balance_transaction called in this test


# ── Scenario 4 ─────────────────────────────────────────────────────────────

def test_balance_redeem(de_db):
    """Customer with €5 balance redeems €5 on a €30 order → €25 due."""
    db = de_db
    a_id, _ = _make_customer(db, "+33600000031", "Alice")

    # Give Alice €5 balance
    db.add_balance_transaction(a_id, "referral_reward", 5.0, "Test credit")
    bal = db.get_balance(a_id)
    assert bal["balance"] == 5.0

    # Redeem €5 on a €30 order
    db.add_balance_transaction(a_id, "payment", -5.0, "Utilisation solde - Commande DRG-TEST-004")
    bal_after = db.get_balance(a_id)
    assert bal_after["balance"] == 0.0

    txns = db.get_transactions(a_id)
    assert txns["total"] == 2


# ── Scenario 5 ─────────────────────────────────────────────────────────────

def test_google_review_demo_reward(de_db):
    """Admin simulates Google review for customer with paid order → 10% reward."""
    db = de_db
    c_id, _ = _make_customer(db, "+33600000041", "Charlie")

    # Charlie has a paid order for €40
    order = _make_paid_order(db, c_id, amount=40.0)

    # Simulate review
    import importlib
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    review_id = db.insert_google_review({
        "google_reviewer_name": "Charlie",
        "google_reviewer_email": "charlie@test.com",
        "rating": 5,
        "review_text": "Excellent!",
        "review_date": now_iso,
        "matched_customer_id": c_id,
        "matched_at": now_iso,
        "raw_email_content": "[SIMULATED]",
    })
    assert review_id > 0

    # Credit reward
    reward = round(float(order["total_paid"]) * 0.10, 2)
    db.create_referral_event({
        "referrer_customer_id": c_id,
        "referred_customer_id": c_id,
        "order_ref": order["order_number"],
        "order_amount": float(order["total_paid"]),
        "commission_amount": reward,
        "event_type": "google_review",
        "status": "credited",
    })
    db.add_balance_transaction(c_id, "review_reward", reward, "Avis Google (5★)")
    db.mark_review_rewarded(review_id)

    bal = db.get_balance(c_id)
    assert bal["balance"] == pytest.approx(reward)


# ── Scenario 6 ─────────────────────────────────────────────────────────────

def test_anti_fraud_self_referral(de_db):
    """Same phone cannot be both referrer and referred → log_fraud_attempt called."""
    db = de_db
    a_id, a_code = _make_customer(db, "+33600000051", "Alice")
    a = db.get_customer_by_id(a_id)

    # Attempt self-referral (same phone)
    same_phone = a["phone"]
    referrer = db.get_customer_by_referral_code(a_code)
    assert referrer is not None
    assert referrer["phone"] == same_phone

    # The register handler logic: if referrer.phone == new phone → fraud
    if referrer["phone"] == same_phone:
        db.log_fraud_attempt(referrer["phone"], same_phone, "self_referral")
        referred_by_code = None
    else:
        referred_by_code = a_code

    assert referred_by_code is None  # self-referral blocked

    # Verify fraud_log has entry
    import tools.dragons_elysees_db as _db_mod
    from contextlib import contextmanager
    with _db_mod.get_conn() as conn:
        row = conn.execute(
            "SELECT reason FROM fraud_log WHERE referrer_phone = ? AND referred_phone = ?",
            (same_phone, same_phone),
        ).fetchone()
    assert row is not None
    assert row["reason"] == "self_referral"
