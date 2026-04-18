"""
Namespace-level webhook configuration.

Endpoints:
  GET    /webhooks/config?namespace=x  — get current webhook URL
  PATCH  /webhooks/config              — set/clear webhook URL
"""
from starlette.requests import Request
from starlette.responses import JSONResponse

from tools.auth import require_session, _get_db


async def webhooks_config_get(request: Request) -> JSONResponse:
    """GET /webhooks/config?namespace=x — return current webhook config."""
    user, err = require_session(request)
    if err:
        return err

    namespace = request.query_params.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace required"}, status_code=400)

    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM user_namespaces WHERE user_id = ? AND namespace = ?",
            (user["id"], namespace),
        )
        if not cursor.fetchone():
            return JSONResponse({"error": "Not found"}, status_code=404)

        cursor.execute(
            "SELECT webhook_url FROM namespaces WHERE namespace = ?",
            (namespace,),
        )
        row = cursor.fetchone()
        webhook_url = row["webhook_url"] if row else None
        return JSONResponse({
            "namespace": namespace,
            "webhook_url": webhook_url or "",
        })
    finally:
        conn.close()


async def webhooks_config_patch(request: Request) -> JSONResponse:
    """PATCH /webhooks/config — set or clear webhook URL for a namespace."""
    user, err = require_session(request)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    namespace = (body.get("namespace") or "").strip()
    webhook_url = (body.get("webhook_url") or "").strip()

    if not namespace:
        return JSONResponse({"error": "namespace required"}, status_code=400)

    if webhook_url and not webhook_url.startswith(("https://", "http://")):
        return JSONResponse({"error": "webhook_url must start with http:// or https://"}, status_code=400)

    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM user_namespaces WHERE user_id = ? AND namespace = ?",
            (user["id"], namespace),
        )
        if not cursor.fetchone():
            return JSONResponse({"error": "Not found"}, status_code=404)

        cursor.execute(
            "UPDATE namespaces SET webhook_url = ? WHERE namespace = ?",
            (webhook_url or None, namespace),
        )
        conn.commit()
        return JSONResponse({
            "namespace": namespace,
            "webhook_url": webhook_url or "",
            "updated": True,
        })
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


def get_namespace_webhook_url(namespace: str) -> str | None:
    """Return the configured webhook_url for a namespace, or None."""
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT webhook_url FROM namespaces WHERE namespace = ?",
            (namespace,),
        )
        row = cursor.fetchone()
        return row["webhook_url"] if row and row["webhook_url"] else None
    except Exception:
        return None
    finally:
        conn.close()
