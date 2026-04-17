"""
Magic link authentication + session management for ClawShow dashboard.

Flow:
  POST /auth/request-login  →  email → generate token → send Resend email
  GET  /auth/verify?token=  →  validate token → create/find user → set cookie
  POST /auth/logout         →  delete session cookie
"""
import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import resend
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://app.clawshow.ai")
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SESSION_DOMAIN = os.environ.get("SESSION_DOMAIN", ".clawshow.ai")
SESSION_TTL_DAYS = 30
TOKEN_TTL_MINUTES = 15
MAX_LOGIN_ATTEMPTS_PER_HOUR = 3

_DEFAULT_DB_PATH = "/opt/clawshow-mcp-server/data/clawshow.db"


def _get_db() -> sqlite3.Connection:
    path = os.environ.get("CLAWSHOW_DB_PATH", _DEFAULT_DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _rate_limit_exceeded(cursor: sqlite3.Cursor, email: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM login_tokens WHERE email = ? AND created_at > datetime('now', '-1 hour')",
        (email.lower(),),
    )
    return cursor.fetchone()[0] >= MAX_LOGIN_ATTEMPTS_PER_HOUR


def _create_login_token(cursor: sqlite3.Cursor, email: str) -> str:
    token = secrets.token_urlsafe(32)
    # SQLite-compatible UTC format: YYYY-MM-DD HH:MM:SS
    expires_at = (_now_utc() + timedelta(minutes=TOKEN_TTL_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO login_tokens (token, email, expires_at) VALUES (?, ?, ?)",
        (token, email.lower(), expires_at),
    )
    return token


def _send_magic_link_email(email: str, token: str, is_founding_welcome: bool = False) -> None:
    resend.api_key = RESEND_API_KEY
    link = f"{APP_BASE_URL}/auth/verify?token={token}"

    if is_founding_welcome:
        subject = "Welcome to ClawShow — Your Founding Customer Access"
        html = f"""
<p>Hi,</p>
<p>You're one of ClawShow's founding customers — thank you for your trust during our launch phase.</p>
<p><a href="{link}" style="background:#2563eb;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold;">Access your dashboard →</a></p>
<p style="color:#666;font-size:12px;">Link expires in 15 minutes.</p>
<p>As a Founding Customer, you have:<br>
✓ Pro plan locked at €29/month until April 2028<br>
✓ Your existing namespace pre-configured<br>
✓ Priority support — reply to this email any time<br>
✓ Early access to new features</p>
<p>— The ClawShow Team</p>
"""
    else:
        subject = "Your ClawShow login link"
        html = f"""
<p>Click the link below to sign in to your ClawShow dashboard.</p>
<p><a href="{link}" style="background:#2563eb;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold;">Sign in to ClawShow →</a></p>
<p style="color:#666;font-size:12px;">This link expires in 15 minutes. If you didn't request this, ignore this email.</p>
"""

    resend.Emails.send({
        "from": "ClawShow <noreply@clawshow.ai>",
        "to": email,
        "subject": subject,
        "html": html,
    })


def _find_or_create_user(cursor: sqlite3.Cursor, email: str) -> tuple[sqlite3.Row, bool]:
    """Returns (user_row, is_new_user)."""
    cursor.execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
    user = cursor.fetchone()
    if user:
        return user, False

    user_id = secrets.token_urlsafe(16)
    cursor.execute(
        "INSERT INTO users (id, email, email_verified, last_login_at) VALUES (?, ?, 1, CURRENT_TIMESTAMP)",
        (user_id, email.lower()),
    )
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return cursor.fetchone(), True


def _auto_link_namespaces(cursor: sqlite3.Cursor, user_id: str, email: str) -> int:
    """Link user to any namespaces where owner_email matches. Returns count linked."""
    cursor.execute(
        """
        INSERT OR IGNORE INTO user_namespaces (user_id, namespace, role)
        SELECT ?, namespace, 'admin'
        FROM namespaces
        WHERE LOWER(owner_email) = LOWER(?)
        """,
        (user_id, email),
    )
    return cursor.rowcount


def _create_namespace_for_user(cursor: sqlite3.Cursor, user_id: str, email: str) -> None:
    slug = email.split("@")[0].lower().replace("+", "").replace(".", "-")[:30]
    # ensure uniqueness
    base_slug = slug
    counter = 1
    while True:
        cursor.execute("SELECT 1 FROM namespaces WHERE namespace = ?", (slug,))
        if not cursor.fetchone():
            break
        slug = f"{base_slug}-{counter}"
        counter += 1

    from datetime import timedelta
    trial_end = (_now_utc() + timedelta(days=30)).isoformat()
    cursor.execute(
        """
        INSERT INTO namespaces (
            namespace, owner_name, owner_email, business_type,
            tier, status, envelope_quota, trial_ends_at,
            current_period_start, current_period_end
        ) VALUES (?, ?, ?, 'default', 'pro', 'trial', 150, ?,
                  CURRENT_TIMESTAMP, ?)
        """,
        (slug, email.split("@")[0], email.lower(), trial_end, trial_end),
    )
    cursor.execute(
        "INSERT OR IGNORE INTO user_namespaces (user_id, namespace, role) VALUES (?, ?, 'admin')",
        (user_id, slug),
    )


def _create_session(cursor: sqlite3.Cursor, user_id: str) -> str:
    session_id = secrets.token_urlsafe(32)
    expires_at = (_now_utc() + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        (session_id, user_id, expires_at),
    )
    return session_id


# ---------------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------------

async def auth_request_login(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "Valid email required"}, status_code=400)

    conn = _get_db()
    try:
        cursor = conn.cursor()
        if _rate_limit_exceeded(cursor, email):
            return JSONResponse(
                {"error": "Too many login attempts. Try again in an hour."},
                status_code=429,
            )
        token = _create_login_token(cursor, email)
        conn.commit()
        _send_magic_link_email(email, token)
        return JSONResponse({"sent": True, "email": email})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        conn.close()


async def auth_verify(request: Request) -> Response:
    token = request.query_params.get("token", "")
    if not token:
        return RedirectResponse(f"{APP_BASE_URL}/auth/error?reason=missing_token")

    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM login_tokens WHERE token = ? AND used_at IS NULL",
            (token,),
        )
        record = cursor.fetchone()
        if not record:
            return RedirectResponse(f"{APP_BASE_URL}/auth/error?reason=invalid_token")

        expires_at = datetime.strptime(record["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if _now_utc() > expires_at:
            return RedirectResponse(f"{APP_BASE_URL}/auth/error?reason=expired_token")

        # Mark token used
        cursor.execute(
            "UPDATE login_tokens SET used_at = CURRENT_TIMESTAMP WHERE token = ?",
            (token,),
        )

        email = record["email"]
        user, is_new = _find_or_create_user(cursor, email)
        user_id = user["id"]

        # Update last_login_at
        cursor.execute(
            "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user_id,),
        )

        # Auto-link existing namespaces by owner_email
        linked = _auto_link_namespaces(cursor, user_id, email)

        # If new user and no namespace linked, create Pro trial namespace
        if is_new:
            cursor.execute(
                "SELECT COUNT(*) FROM user_namespaces WHERE user_id = ?",
                (user_id,),
            )
            if cursor.fetchone()[0] == 0:
                _create_namespace_for_user(cursor, user_id, email)

        session_id = _create_session(cursor, user_id)
        conn.commit()

        response = RedirectResponse(f"{APP_BASE_URL}/dashboard", status_code=302)
        response.set_cookie(
            key="cs_session",
            value=session_id,
            httponly=True,
            secure=True,
            samesite="lax",
            domain=SESSION_DOMAIN,
            max_age=SESSION_TTL_DAYS * 86400,
            path="/",
        )
        return response
    except Exception as e:
        conn.rollback()
        return RedirectResponse(f"{APP_BASE_URL}/auth/error?reason=server_error")
    finally:
        conn.close()


async def auth_logout(request: Request) -> JSONResponse:
    session_id = request.cookies.get("cs_session", "")
    if session_id:
        conn = _get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()

    response = JSONResponse({"logged_out": True})
    response.delete_cookie(
        key="cs_session",
        domain=SESSION_DOMAIN,
        path="/",
    )
    return response


def get_session_user(request: Request) -> dict | None:
    """Resolve current user from session cookie. Returns user dict or None."""
    session_id = request.cookies.get("cs_session", "")
    if not session_id:
        return None

    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT u.id, u.email, u.email_verified, u.last_login_at
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.id = ? AND s.expires_at > CURRENT_TIMESTAMP
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def require_session(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Returns (user, None) if authenticated, or (None, error_response)."""
    user = get_session_user(request)
    if not user:
        return None, JSONResponse({"error": "Authentication required"}, status_code=401)
    return user, None


auth_routes_list = [
    ("POST", "/auth/request-login", auth_request_login),
    ("GET",  "/auth/verify",        auth_verify),
    ("POST", "/auth/logout",        auth_logout),
]
