"""
Tests for tools/api_keys.py
"""
import sqlite3

import pytest


def _cursor(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()


def _insert_namespace(conn, namespace):
    conn.execute(
        "INSERT OR IGNORE INTO namespaces (namespace, owner_name, owner_email) VALUES (?, ?, ?)",
        (namespace, namespace, f"{namespace}@test.com"),
    )
    conn.commit()


def test_generate_api_key_format(tmp_db, monkeypatch):
    db_path, _ = tmp_db
    from tools.api_keys import _generate_api_key
    full, prefix, key_hash = _generate_api_key()
    assert full.startswith("sk_live_")
    assert prefix == full[:12]
    assert len(key_hash) == 64  # sha256 hex


def test_resolve_valid_key(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    _insert_namespace(conn, "test-ns")
    from tools.api_keys import _generate_api_key, resolve_namespace_from_api_key
    import hashlib, secrets as _s

    full, prefix, key_hash = _generate_api_key()
    key_id = _s.token_urlsafe(8)
    conn.execute(
        "INSERT INTO api_keys (id, namespace, key_prefix, key_hash, name) VALUES (?, ?, ?, ?, ?)",
        (key_id, "test-ns", prefix, key_hash, "test"),
    )
    conn.commit()

    result = resolve_namespace_from_api_key(full)
    assert result == "test-ns"


def test_resolve_revoked_key_returns_none(tmp_db, monkeypatch):
    db_path, conn = tmp_db
    _insert_namespace(conn, "test-ns2")
    from tools.api_keys import _generate_api_key, resolve_namespace_from_api_key
    import secrets as _s

    full, prefix, key_hash = _generate_api_key()
    key_id = _s.token_urlsafe(8)
    conn.execute(
        """INSERT INTO api_keys (id, namespace, key_prefix, key_hash, name, revoked_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (key_id, "test-ns2", prefix, key_hash, "revoked"),
    )
    conn.commit()

    result = resolve_namespace_from_api_key(full)
    assert result is None


def test_resolve_unknown_key_returns_none(tmp_db, monkeypatch):
    from tools.api_keys import resolve_namespace_from_api_key
    result = resolve_namespace_from_api_key("sk_live_nonexistent_key_value")
    assert result is None


def test_resolve_non_sk_live_key_returns_none(tmp_db, monkeypatch):
    from tools.api_keys import resolve_namespace_from_api_key
    result = resolve_namespace_from_api_key("Bearer not-an-api-key")
    assert result is None
