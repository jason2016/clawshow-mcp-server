"""
Tests for tools/auth.py

Covers:
- Token creation and expiry
- Rate limiting (3/hour)
- find_or_create_user: new vs returning
- auto-link namespaces by owner_email
- new user with no namespace → default Pro trial namespace created
- returning user → no duplicate namespace links
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


def _insert_namespace(conn, namespace, owner_email, tier="free", quota=5):
    conn.execute(
        """INSERT INTO namespaces
           (namespace, owner_name, owner_email, tier, status, envelope_quota)
           VALUES (?, ?, ?, ?, 'active', ?)""",
        (namespace, namespace, owner_email, tier, quota),
    )
    conn.commit()


def test_create_login_token(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    cur = conn.cursor()
    token = auth._create_login_token(cur, "user@example.com")
    conn.commit()
    assert token and len(token) > 20

    cur.execute("SELECT email, used_at FROM login_tokens WHERE token=?", (token,))
    row = cur.fetchone()
    assert row["email"] == "user@example.com"
    assert row["used_at"] is None


def test_rate_limit_not_exceeded_initially(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    cur = conn.cursor()
    assert not auth._rate_limit_exceeded(cur, "test@example.com")


def test_rate_limit_exceeded_after_3(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    cur = conn.cursor()
    for _ in range(3):
        auth._create_login_token(cur, "flood@example.com")
    conn.commit()
    assert auth._rate_limit_exceeded(cur, "flood@example.com")


def test_find_or_create_user_new(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    cur = conn.cursor()
    user, is_new = auth._find_or_create_user(cur, "new@example.com")
    conn.commit()
    assert is_new is True
    assert user["email"] == "new@example.com"


def test_find_or_create_user_returning(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    cur = conn.cursor()
    user1, _ = auth._find_or_create_user(cur, "ret@example.com")
    conn.commit()
    user2, is_new = auth._find_or_create_user(cur, "ret@example.com")
    assert is_new is False
    assert user1["id"] == user2["id"]


def test_auto_link_namespace_by_owner_email(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    _insert_namespace(conn, "dragons-elysees", "dragons@owner.com")

    cur = conn.cursor()
    user, _ = auth._find_or_create_user(cur, "dragons@owner.com")
    conn.commit()
    linked = auth._auto_link_namespaces(cur, user["id"], "dragons@owner.com")
    conn.commit()
    assert linked == 1

    cur.execute(
        "SELECT namespace FROM user_namespaces WHERE user_id=?", (user["id"],)
    )
    assert cur.fetchone()["namespace"] == "dragons-elysees"


def test_auto_link_no_match(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    cur = conn.cursor()
    user, _ = auth._find_or_create_user(cur, "nobody@example.com")
    conn.commit()
    linked = auth._auto_link_namespaces(cur, user["id"], "nobody@example.com")
    conn.commit()
    assert linked == 0


def test_auto_link_idempotent(tmp_db, monkeypatch):
    """Returning user login should not create duplicate links."""
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    _insert_namespace(conn, "my-biz", "owner@biz.com")

    cur = conn.cursor()
    user, _ = auth._find_or_create_user(cur, "owner@biz.com")
    conn.commit()
    auth._auto_link_namespaces(cur, user["id"], "owner@biz.com")
    conn.commit()
    # Second login
    auth._auto_link_namespaces(cur, user["id"], "owner@biz.com")
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM user_namespaces WHERE user_id=?", (user["id"],))
    assert cur.fetchone()[0] == 1


def test_new_user_no_match_gets_default_namespace(tmp_db, monkeypatch):
    """New user with no matching namespace → auto-created Pro trial namespace."""
    db_path, conn = tmp_db
    import importlib, tools.auth as auth
    importlib.reload(auth)

    cur = conn.cursor()
    user, is_new = auth._find_or_create_user(cur, "brand-new@startup.io")
    conn.commit()
    assert is_new

    linked = auth._auto_link_namespaces(cur, user["id"], "brand-new@startup.io")
    conn.commit()
    assert linked == 0

    # Simulate the verify_magic_link logic: if new + no link → create namespace
    cur.execute("SELECT COUNT(*) FROM user_namespaces WHERE user_id=?", (user["id"],))
    if cur.fetchone()[0] == 0:
        auth._create_namespace_for_user(cur, user["id"], "brand-new@startup.io")
        conn.commit()

    cur.execute("SELECT namespace, tier FROM user_namespaces un JOIN namespaces n USING(namespace) WHERE un.user_id=?", (user["id"],))
    row = cur.fetchone()
    assert row is not None
    assert row["tier"] == "pro"
