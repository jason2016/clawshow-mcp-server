"""eSign User Auth — OTP Magic Link (Starlette native handlers).

Tables: esign_users, esign_login_otps, esign_user_sessions
Cookie: clawshow_esign_session (httpOnly, Secure, SameSite=Lax, 30 days)
"""
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Lock

import db
from adapters.esign.mailer import send_html
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

COOKIE_NAME = "clawshow_esign_session"

# ── Rate limiting (in-memory, per-process) ────────────────────────────────────
_otp_send_history: dict = defaultdict(list)   # email -> [timestamps]
_ip_signup_history: dict = defaultdict(list)  # ip    -> [timestamps]
_rate_limit_lock = Lock()


def _check_rate_limit(email: str, ip: str) -> tuple[bool, str]:
    """60s cooldown per email; max 10 OTP requests/hour per IP."""
    now = time.time()
    with _rate_limit_lock:
        _otp_send_history[email] = [t for t in _otp_send_history[email] if now - t < 3600]
        _ip_signup_history[ip]   = [t for t in _ip_signup_history[ip]   if now - t < 3600]
        if _otp_send_history[email] and now - _otp_send_history[email][-1] < 60:
            return False, "Veuillez patienter avant de demander un nouveau code."
        if len(_ip_signup_history[ip]) >= 10:
            return False, "Trop de demandes. Veuillez réessayer plus tard."
        _otp_send_history[email].append(now)
        _ip_signup_history[ip].append(now)
        return True, ""


OTP_TTL_MIN = 10
SESSION_TTL_DAYS = 30


# ── helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%S")


def _otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _token() -> str:
    return secrets.token_urlsafe(32)


def _get_user(email: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, account_type, status, "
            "free_quota_total, free_quota_used "
            "FROM esign_users WHERE email = ?",
            (email,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def _user_remaining(u: dict) -> int:
    return u["free_quota_total"] - u["free_quota_used"]


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=True,
        samesite="lax",
        domain=".clawshow.ai",
    )


def get_session_user(request: Request) -> dict | None:
    """Read session cookie -> return user dict or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.display_name, u.account_type,
                      u.status, u.free_quota_total, u.free_quota_used,
                      s.expires_at
               FROM esign_user_sessions s
               JOIN esign_users u ON u.id = s.user_id
               WHERE s.session_token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        try:
            exp = datetime.fromisoformat(row["expires_at"].replace("+00:00", ""))
            if exp < _now_utc().replace(tzinfo=None):
                return None
        except Exception:
            return None
        if row["status"] != "active":
            return None
        conn.execute(
            "UPDATE esign_user_sessions SET last_used_at = ? WHERE session_token = ?",
            (_now_iso(), token),
        )
    return row


# ── POST /esign/auth/signup ───────────────────────────────────────────────────

async def auth_signup(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "Valid email required"}, status_code=400)

    xff = request.headers.get("x-forwarded-for", "")
    client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "127.0.0.1")

    allowed, reason = _check_rate_limit(email, client_ip)
    if not allowed:
        logger.warning("[SIGNUP] rate_limit email=%s ip=%s", email[:4] + "***", client_ip)
        return JSONResponse({"error": reason}, status_code=429)

    display_name = (body.get("display_name") or email.split("@")[0]).strip()

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM esign_users WHERE email = ?", (email,)
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO esign_users (email, display_name, account_type, free_quota_total, free_quota_used) "
                "VALUES (?, ?, 'personal', 3, 0)",
                (email, display_name),
            )
            action = "created"
        else:
            action = "existing"

    # Auto-send OTP so user lands directly on verify page
    code = _otp()
    expires = (_now_utc() + timedelta(minutes=OTP_TTL_MIN)).strftime("%Y-%m-%dT%H:%M:%S")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE esign_login_otps SET verified_at = ? WHERE email = ? AND verified_at IS NULL",
            (_now_iso(), email),
        )
        conn.execute(
            "INSERT INTO esign_login_otps (email, otp_code, expires_at) VALUES (?, ?, ?)",
            (email, code, expires),
        )

    subject = "Votre code ClawShow eSign"
    html = (
        "<body style='font-family:Arial,sans-serif;max-width:560px;margin:0 auto;"
        "padding:24px;background:#f9f9fb'>"
        "<div style='background:#fff;border-radius:10px;padding:32px 40px;"
        "box-shadow:0 2px 8px rgba(0,0,0,.06)'>"
        "<div style='text-align:center;margin-bottom:24px'>"
        "<span style='font-size:28px;font-weight:700;color:#0F62FE'>X</span>"
        "<span style='font-size:18px;font-weight:600;color:#0f172a;margin-left:4px'>"
        "ClawShow eSign</span></div>"
        "<p style='color:#475569;margin:0 0 12px'>Votre code de connexion&nbsp;:</p>"
        "<div style='text-align:center;margin:0 0 28px'>"
        "<span style='display:inline-block;font-size:40px;font-weight:700;letter-spacing:10px;"
        f"color:#0F62FE;background:#EFF6FF;padding:16px 32px;border-radius:8px'>{code}</span>"
        "</div>"
        "<p style='color:#64748b;font-size:13px;text-align:center'>"
        "Ce code expire dans <strong>10 minutes</strong>.<br>"
        "Si vous n&rsquo;avez pas demand&eacute; cette connexion, ignorez cet e-mail.</p>"
        "<hr style='border:none;border-top:1px solid #e2e8f0;margin:24px 0'>"
        "<p style='color:#94a3b8;font-size:11px;text-align:center'>"
        "ClawShow SAS &mdash; 50 avenue des Champs-&Eacute;lys&eacute;es, 75008 Paris</p>"
        "</div></body>"
    )

    try:
        send_html(to=email, subject=subject, html=html)
    except Exception as exc:
        logger.error("Signup OTP email failed: %s", exc)
        return JSONResponse({"error": "Echec envoi email"}, status_code=500)

    logger.info("[SIGNUP] action=%s email=%s*** ip=%s", action, email[:4], client_ip)
    import urllib.parse as _up
    redirect_url = "/login/verify?email=" + _up.quote(email)
    return JSONResponse({"status": "ok", "email": email, "redirect_url": redirect_url})


# ── POST /esign/auth/login/otp/send ──────────────────────────────────────────

async def auth_login_otp_send(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"error": "Email required"}, status_code=400)

    user = _get_user(email)
    if not user:
        return JSONResponse({"error": "Compte introuvable. Veuillez vous inscrire d'abord."}, status_code=404)
    if user["status"] != "active":
        return JSONResponse({"error": "Compte suspendu."}, status_code=403)

    code = _otp()
    expires = (_now_utc() + timedelta(minutes=OTP_TTL_MIN)).strftime("%Y-%m-%dT%H:%M:%S")

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE esign_login_otps SET verified_at = ? WHERE email = ? AND verified_at IS NULL",
            (_now_iso(), email),
        )
        conn.execute(
            "INSERT INTO esign_login_otps (email, otp_code, expires_at) VALUES (?, ?, ?)",
            (email, code, expires),
        )

    name = user.get("display_name") or email.split("@")[0]
    html = (
        "<body style='font-family:Arial,sans-serif;max-width:560px;margin:0 auto;"
        "padding:24px;background:#f9f9fb'>"
        "<div style='background:#fff;border-radius:10px;padding:32px 40px;"
        "box-shadow:0 2px 8px rgba(0,0,0,.06)'>"
        "<div style='text-align:center;margin-bottom:24px'>"
        "<span style='font-size:28px;font-weight:700;color:#0F62FE'>X</span>"
        "<span style='font-size:18px;font-weight:600;color:#0f172a;margin-left:4px'>"
        "ClawShow eSign</span></div>"
        f"<h2 style='color:#0f172a;font-size:20px;margin:0 0 8px'>Bonjour {name}&nbsp;!</h2>"
        "<p style='color:#475569;margin:0 0 28px'>Votre code de connexion ClawShow eSign&nbsp;:</p>"
        "<div style='text-align:center;margin:0 0 28px'>"
        "<span style='display:inline-block;font-size:40px;font-weight:700;letter-spacing:10px;"
        f"color:#0F62FE;background:#EFF6FF;padding:16px 32px;border-radius:8px'>{code}</span>"
        "</div>"
        "<p style='color:#64748b;font-size:13px;text-align:center'>"
        "Ce code expire dans <strong>10 minutes</strong>.<br>"
        "Si vous n&rsquo;avez pas demand&eacute; cette connexion, ignorez cet e-mail.</p>"
        "<hr style='border:none;border-top:1px solid #e2e8f0;margin:24px 0'>"
        "<p style='color:#94a3b8;font-size:11px;text-align:center'>"
        "ClawShow SAS &mdash; 50 avenue des Champs-&Eacute;lys&eacute;es, 75008 Paris</p>"
        "</div></body>"
    )
    try:
        send_html(to=email, subject=f"Code de connexion ClawShow : {code}", html=html)
    except Exception as exc:
        logger.error("Login OTP email failed: %s", exc)
        return JSONResponse({"error": "Echec envoi email"}, status_code=500)

    logger.info("login OTP sent: %s***", email[:4])
    return JSONResponse({"status": "sent", "expires_in_minutes": OTP_TTL_MIN})


# ── POST /esign/auth/login/verify ─────────────────────────────────────────────

async def auth_login_verify(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    email = (body.get("email") or "").strip().lower()
    otp_input = str(body.get("otp") or "").strip()

    user = _get_user(email)
    if not user:
        return JSONResponse({"error": "Compte introuvable"}, status_code=404)

    session_token = None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, otp_code, expires_at, attempts FROM esign_login_otps "
            "WHERE email = ? AND verified_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (email,),
        ).fetchone()

        if not row:
            return JSONResponse({"error": "Aucun code actif. Demandez un nouveau code."}, status_code=400)

        otp_id = row["id"]
        otp_code = row["otp_code"]
        expires_at = row["expires_at"]
        attempts = row["attempts"]

        try:
            exp = datetime.fromisoformat(expires_at)
            if exp < _now_utc().replace(tzinfo=None):
                return JSONResponse({"error": "Code expiré. Demandez un nouveau code."}, status_code=400)
        except Exception:
            pass

        if attempts >= 5:
            return JSONResponse({"error": "Trop de tentatives. Demandez un nouveau code."}, status_code=429)

        if otp_input != otp_code:
            conn.execute(
                "UPDATE esign_login_otps SET attempts = attempts + 1 WHERE id = ?", (otp_id,)
            )
            remaining = max(0, 4 - attempts)
            return JSONResponse(
                {"error": f"Code incorrect. {remaining} tentative(s) restante(s)."},
                status_code=400,
            )

        conn.execute(
            "UPDATE esign_login_otps SET verified_at = ? WHERE id = ?", (_now_iso(), otp_id)
        )

        session_token = _token()
        session_expires = (_now_utc() + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")

        xff = request.headers.get("x-forwarded-for", "")
        ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")

        conn.execute(
            "INSERT INTO esign_user_sessions (user_id, session_token, expires_at, ip_address, user_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            (user["id"], session_token, session_expires, ip,
             request.headers.get("user-agent", "")[:500]),
        )
        conn.execute(
            "UPDATE esign_users SET last_login_at = ? WHERE id = ?", (_now_iso(), user["id"])
        )

    resp = JSONResponse({
        "status": "logged_in",
        "user": {
            "email": user["email"],
            "display_name": user["display_name"],
            "account_type": user["account_type"],
            "free_quota_total": user["free_quota_total"],
            "free_quota_used": user["free_quota_used"],
            "free_quota_remaining": _user_remaining(user),
        }
    })
    _set_session_cookie(resp, session_token)
    logger.info("login verified: %s***", email[:4])
    return resp


# ── POST /esign/auth/logout ───────────────────────────────────────────────────

async def auth_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM esign_user_sessions WHERE session_token = ?", (token,))
    resp = JSONResponse({"status": "logged_out"})
    resp.delete_cookie(COOKIE_NAME, domain=".clawshow.ai")
    return resp


# ── GET /esign/auth/me ────────────────────────────────────────────────────────

async def auth_me(request: Request) -> JSONResponse:
    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Non authentifié"}, status_code=401)
    return JSONResponse({
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "account_type": user["account_type"],
        "free_quota_total": user["free_quota_total"],
        "free_quota_used": user["free_quota_used"],
        "free_quota_remaining": _user_remaining(user),
    })


# ── GET /esign/user/documents ─────────────────────────────────────────────────

async def auth_user_documents(request: Request) -> JSONResponse:
    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "Non authentifié"}, status_code=401)

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, status, created_at, signer_name, signer_email "
            "FROM esign_documents WHERE creator_user_id = ? "
            "ORDER BY created_at DESC LIMIT 50",
            (user["id"],),
        ).fetchall()

    docs = [dict(r) for r in rows]
    return JSONResponse({"documents": docs, "total": len(docs)})
