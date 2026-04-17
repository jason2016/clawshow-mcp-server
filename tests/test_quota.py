"""
Tests for tools/subscriptions.py quota logic.
"""
import sqlite3

import pytest


def _insert_namespace(conn, namespace, tier, quota, used, status="active"):
    conn.execute(
        """INSERT OR REPLACE INTO namespaces
           (namespace, owner_name, owner_email, tier, status,
            envelope_quota, envelopes_used_this_period)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (namespace, namespace, f"{namespace}@test.com", tier, status, quota, used),
    )
    conn.commit()


def _cursor(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()


def test_free_tier_under_quota_allowed(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import check_quota
    _insert_namespace(conn, "ns1", "free", 5, 3)
    _, cur = _cursor(db_path)
    result = check_quota("ns1", cur)
    assert result.allowed
    assert not result.is_overage


def test_free_tier_at_quota_blocked(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import check_quota
    _insert_namespace(conn, "ns2", "free", 5, 5)
    _, cur = _cursor(db_path)
    result = check_quota("ns2", cur)
    assert not result.allowed
    assert "Free plan" in result.reason


def test_pro_tier_over_quota_allows_overage(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import check_quota
    _insert_namespace(conn, "ns3", "pro", 150, 155)
    _, cur = _cursor(db_path)
    result = check_quota("ns3", cur)
    assert result.allowed
    assert result.is_overage
    assert result.overage_rate_cents == 35


def test_starter_tier_overage(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import check_quota
    _insert_namespace(conn, "ns4", "starter", 30, 31)
    _, cur = _cursor(db_path)
    result = check_quota("ns4", cur)
    assert result.allowed
    assert result.is_overage
    assert result.overage_rate_cents == 50


def test_enterprise_unlimited(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import check_quota
    _insert_namespace(conn, "ns5", "enterprise", -1, 9999)
    _, cur = _cursor(db_path)
    result = check_quota("ns5", cur)
    assert result.allowed
    assert not result.is_overage
    assert result.envelope_quota == -1


def test_cancelled_account_blocked(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import check_quota
    _insert_namespace(conn, "ns6", "pro", 150, 0, status="cancelled")
    _, cur = _cursor(db_path)
    result = check_quota("ns6", cur)
    assert not result.allowed
    assert "cancelled" in result.reason.lower()


def test_unknown_namespace_blocked(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import check_quota
    _, cur = _cursor(db_path)
    result = check_quota("doesnt-exist", cur)
    assert not result.allowed


def test_increment_usage(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    from tools.subscriptions import increment_usage
    _insert_namespace(conn, "ns7", "pro", 150, 10)
    c2, cur = _cursor(db_path)
    increment_usage("ns7", "doc-001", cur, is_overage=False)
    c2.commit()

    cur.execute("SELECT envelopes_used_this_period FROM namespaces WHERE namespace='ns7'")
    assert cur.fetchone()[0] == 11

    cur.execute("SELECT COUNT(*) FROM usage_events WHERE namespace='ns7'")
    assert cur.fetchone()[0] == 1
