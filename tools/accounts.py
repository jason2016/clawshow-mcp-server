"""
Account (namespace) management endpoints.

Endpoints:
  GET  /accounts/me        → list all namespaces the session user belongs to
  POST /accounts           → create a new namespace (Pro trial) for session user
  GET  /accounts/:ns       → get namespace detail (must be member)
  POST /internal/invite-founding  → (Jason-only) link a real email to a founding namespace
"""
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse

from tools.auth import _get_db, _create_namespace_for_user, require_session

INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")


def _namespace_to_dict(row) -> dict:
    d = dict(row)
    # envelope_quota -1 means unlimited
    d["quota_unlimited"] = d.get("envelope_quota") == -1
    return d


async def accounts_me(request: Request) -> JSONResponse:
    user, err = require_session(request)
    if err:
        return err

    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT n.*, un.role
            FROM namespaces n
            JOIN user_namespaces un ON n.namespace = un.namespace
            WHERE un.user_id = ?
            ORDER BY n.created_at
            """,
            (user["id"],),
        )
        rows = cursor.fetchall()
        return JSONResponse({
            "user": {"id": user["id"], "email": user["email"]},
            "accounts": [_namespace_to_dict(r) for r in rows],
        })
    finally:
        conn.close()


async def accounts_create(request: Request) -> JSONResponse:
    user, err = require_session(request)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    display_name = (body.get("display_name") or "").strip()
    if not display_name:
        return JSONResponse({"error": "display_name required"}, status_code=400)

    conn = _get_db()
    try:
        cursor = conn.cursor()

        # Generate slug from display_name
        import re
        slug = re.sub(r"[^a-z0-9-]", "-", display_name.lower())[:30].strip("-")
        base_slug = slug
        counter = 1
        while True:
            cursor.execute("SELECT 1 FROM namespaces WHERE namespace = ?", (slug,))
            if not cursor.fetchone():
                break
            slug = f"{base_slug}-{counter}"
            counter += 1

        now = datetime.now(timezone.utc)
        trial_end = (now + timedelta(days=30)).isoformat()

        cursor.execute(
            """
            INSERT INTO namespaces (
                namespace, owner_name, owner_email, business_type,
                tier, status, envelope_quota, trial_ends_at,
                current_period_start, current_period_end
            ) VALUES (?, ?, ?, 'default', 'pro', 'trial', 150, ?,
                      CURRENT_TIMESTAMP, ?)
            """,
            (slug, display_name, user["email"], trial_end, trial_end),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO user_namespaces (user_id, namespace, role) VALUES (?, ?, 'admin')",
            (user["id"], slug),
        )
        conn.commit()

        cursor.execute("SELECT * FROM namespaces WHERE namespace = ?", (slug,))
        row = cursor.fetchone()
        return JSONResponse(_namespace_to_dict(row), status_code=201)
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


async def accounts_get(request: Request) -> JSONResponse:
    user, err = require_session(request)
    if err:
        return err

    ns = request.path_params.get("namespace", "")
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT n.*, un.role FROM namespaces n
            JOIN user_namespaces un ON n.namespace = un.namespace
            WHERE n.namespace = ? AND un.user_id = ?
            """,
            (ns, user["id"]),
        )
        row = cursor.fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return JSONResponse(_namespace_to_dict(row))
    finally:
        conn.close()


async def internal_invite_founding(request: Request) -> JSONResponse:
    secret = request.headers.get("X-Internal-Secret", "")
    if not INTERNAL_SECRET or secret != INTERNAL_SECRET:
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    namespace = (body.get("namespace") or "").strip()
    display_name = body.get("display_name")

    if not email or not namespace:
        return JSONResponse({"error": "email and namespace required"}, status_code=400)

    conn = _get_db()
    try:
        cursor = conn.cursor()

        cursor.execute("SELECT 1 FROM namespaces WHERE namespace = ?", (namespace,))
        if not cursor.fetchone():
            return JSONResponse({"error": f"Namespace '{namespace}' not found"}, status_code=404)

        cursor.execute(
            """
            UPDATE namespaces
            SET owner_email = ?,
                owner_name = COALESCE(?, owner_name),
                updated_at = CURRENT_TIMESTAMP
            WHERE namespace = ?
            """,
            (email, display_name, namespace),
        )
        conn.commit()

        # Generate and send founding welcome magic link
        from tools.auth import _create_login_token, _send_magic_link_email, _rate_limit_exceeded
        if _rate_limit_exceeded(cursor, email):
            return JSONResponse(
                {"error": "Rate limit hit for this email"},
                status_code=429,
            )
        token = _create_login_token(cursor, email)
        conn.commit()
        _send_magic_link_email(email, token, is_founding_welcome=True)

        return JSONResponse({"sent": True, "namespace": namespace, "email": email})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


accounts_routes_list = [
    ("GET",  "/accounts/me",                   accounts_me),
    ("POST", "/accounts",                      accounts_create),
    ("GET",  "/accounts/{namespace}",          accounts_get),
    ("POST", "/internal/invite-founding",      internal_invite_founding),
]
