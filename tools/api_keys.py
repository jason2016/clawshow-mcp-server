"""
API key management.

Keys are in the format: sk_live_<32-char-random>
Only the first 12 chars (sk_live_xxxx) are stored in plain text (key_prefix).
The full key is hashed with SHA-256 before storage.
The raw key is returned ONCE on creation and never again.

Endpoints:
  POST   /api-keys         → create key for namespace
  GET    /api-keys         → list keys for namespace (no raw values)
  DELETE /api-keys/:id     → revoke key
"""
import hashlib
import os
import secrets
import sqlite3

from starlette.requests import Request
from starlette.responses import JSONResponse

from tools.auth import _get_db, require_session


def _generate_api_key() -> tuple[str, str, str]:
    """Returns (full_key, key_prefix, key_hash)."""
    raw = "sk_live_" + secrets.token_urlsafe(32)
    prefix = raw[:12]
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, prefix, key_hash


def resolve_namespace_from_api_key(bearer_token: str) -> str | None:
    """Resolve namespace from Bearer token. Returns namespace string or None."""
    if not bearer_token or not bearer_token.startswith("sk_live_"):
        return None

    key_hash = hashlib.sha256(bearer_token.encode()).hexdigest()
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT namespace FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        )
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE key_hash = ?",
                (key_hash,),
            )
            conn.commit()
            return row["namespace"]
        return None
    finally:
        conn.close()


def require_api_key(request: Request) -> tuple[str | None, JSONResponse | None]:
    """Returns (namespace, None) if valid API key, or (None, error_response)."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, JSONResponse(
            {"error": "Bearer API key required"},
            status_code=401,
        )
    token = auth_header[7:]
    namespace = resolve_namespace_from_api_key(token)
    if not namespace:
        return None, JSONResponse(
            {"error": "Invalid or revoked API key"},
            status_code=401,
        )
    return namespace, None


# ---------------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------------

async def api_keys_create(request: Request) -> JSONResponse:
    user, err = require_session(request)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    namespace = (body.get("namespace") or "").strip()
    name = (body.get("name") or "Default").strip()

    if not namespace:
        return JSONResponse({"error": "namespace required"}, status_code=400)

    conn = _get_db()
    try:
        cursor = conn.cursor()
        # Verify user is member of this namespace
        cursor.execute(
            "SELECT 1 FROM user_namespaces WHERE user_id = ? AND namespace = ?",
            (user["id"], namespace),
        )
        if not cursor.fetchone():
            return JSONResponse({"error": "Not found"}, status_code=404)

        full_key, prefix, key_hash = _generate_api_key()
        key_id = secrets.token_urlsafe(12)

        cursor.execute(
            """
            INSERT INTO api_keys (id, namespace, key_prefix, key_hash, name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key_id, namespace, prefix, key_hash, name),
        )
        conn.commit()

        return JSONResponse({
            "id": key_id,
            "key": full_key,  # only time raw key is returned
            "key_prefix": prefix,
            "name": name,
            "namespace": namespace,
            "warning": "Save this key — it will not be shown again.",
        }, status_code=201)
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


async def api_keys_list(request: Request) -> JSONResponse:
    user, err = require_session(request)
    if err:
        return err

    namespace = request.query_params.get("namespace", "")
    conn = _get_db()
    try:
        cursor = conn.cursor()
        if namespace:
            cursor.execute(
                "SELECT 1 FROM user_namespaces WHERE user_id = ? AND namespace = ?",
                (user["id"], namespace),
            )
            if not cursor.fetchone():
                return JSONResponse({"error": "Not found"}, status_code=404)
            cursor.execute(
                """
                SELECT id, namespace, key_prefix, name, last_used_at, created_at, revoked_at
                FROM api_keys WHERE namespace = ? AND revoked_at IS NULL
                ORDER BY created_at DESC
                """,
                (namespace,),
            )
        else:
            cursor.execute(
                """
                SELECT ak.id, ak.namespace, ak.key_prefix, ak.name,
                       ak.last_used_at, ak.created_at, ak.revoked_at
                FROM api_keys ak
                JOIN user_namespaces un ON ak.namespace = un.namespace
                WHERE un.user_id = ? AND ak.revoked_at IS NULL
                ORDER BY ak.created_at DESC
                """,
                (user["id"],),
            )
        rows = cursor.fetchall()
        return JSONResponse({"keys": [dict(r) for r in rows]})
    finally:
        conn.close()


async def api_keys_revoke(request: Request) -> JSONResponse:
    user, err = require_session(request)
    if err:
        return err

    key_id = request.path_params.get("id", "")
    conn = _get_db()
    try:
        cursor = conn.cursor()
        # Verify ownership
        cursor.execute(
            """
            SELECT ak.id FROM api_keys ak
            JOIN user_namespaces un ON ak.namespace = un.namespace
            WHERE ak.id = ? AND un.user_id = ? AND ak.revoked_at IS NULL
            """,
            (key_id, user["id"]),
        )
        if not cursor.fetchone():
            return JSONResponse({"error": "Not found"}, status_code=404)

        cursor.execute(
            "UPDATE api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE id = ?",
            (key_id,),
        )
        conn.commit()
        return JSONResponse({"revoked": True, "id": key_id})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


api_keys_routes_list = [
    ("POST",   "/api-keys",       api_keys_create),
    ("GET",    "/api-keys",       api_keys_list),
    ("DELETE", "/api-keys/{id}",  api_keys_revoke),
]
