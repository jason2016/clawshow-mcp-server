"""
Public Billing API — P0-1 (2026-04-22)

Endpoints (no auth, token-gated):
  GET  /api/billing/public/{token}       — load payment page data
  POST /api/billing/public/{token}/pay   — create Mollie checkout, return URL
  GET  /api/billing/public/{token}/status — poll payment status

Security:
  - Token validated on every request
  - Rate limiting: max 30 requests/minute per IP per token
  - Invalid/expired token → 200 with status="expired" (no 404, prevents scanning)
  - No PII in logs (customer name/email masked)
  - CORS: clawshow.ai + localhost:5173 only
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from core.payment_token import validate_token, mark_token_used, mark_token_paid, get_token_record
from core.brand_config import get_brand
from storage.billing_db import BillingDB

logger = logging.getLogger(__name__)

# ---- rate limiting (in-memory, single-instance) ----------------------------
# Structure: {(ip, token): [timestamp, ...]}
_RATE_STORE: dict[tuple, list[float]] = defaultdict(list)
_RATE_WINDOW = 60   # seconds
_RATE_LIMIT = 30    # max requests per window


def _check_rate_limit(ip: str, token: str) -> bool:
    """Returns True if allowed, False if rate-limited."""
    key = (ip, token[:16])  # use token prefix as key (not full token in memory)
    now = time.monotonic()
    timestamps = _RATE_STORE[key]
    # evict old entries
    _RATE_STORE[key] = [t for t in timestamps if now - t < _RATE_WINDOW]
    if len(_RATE_STORE[key]) >= _RATE_LIMIT:
        return False
    _RATE_STORE[key].append(now)
    return True


def _cors_headers(request: Request) -> dict:
    origin = request.headers.get("origin", "")
    allowed = {"https://clawshow.ai", "http://localhost:5173", "http://localhost:4173"}
    if origin in allowed:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }
    return {}


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")


def _mask(s: str) -> str:
    """Mask PII for logging: 'Jean Dupont' → 'Je***nt'"""
    if not s or len(s) < 4:
        return "***"
    return s[:2] + "***" + s[-2:]


def _format_date_fr(date_str: str) -> str:
    """'2026-09-01' → '1 septembre 2026'"""
    MONTHS_FR = {
        1: "janvier", 2: "février", 3: "mars", 4: "avril",
        5: "mai", 6: "juin", 7: "juillet", 8: "août",
        9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
    }
    try:
        d = datetime.fromisoformat(date_str[:10])
        return f"{d.day} {MONTHS_FR[d.month]} {d.year}"
    except Exception:
        return date_str


# ---- GET /api/billing/public/{token} ----------------------------------------

async def billing_public_get(request: Request) -> JSONResponse:
    """Return payment page data for a token."""
    token = request.path_params["token"]
    ip = _client_ip(request)
    cors = _cors_headers(request)

    if not _check_rate_limit(ip, token):
        return JSONResponse({"error": "rate_limited"}, status_code=429, headers=cors)

    record = validate_token(token)
    if not record:
        # Return expired shape rather than 404 (prevents token scanning)
        return JSONResponse({"status": "expired", "brand": get_brand("clawshow")}, headers=cors)

    db = BillingDB()
    db.init_tables()

    plan = db.get_plan(record["plan_id"], record["namespace"])
    if not plan:
        return JSONResponse({"status": "expired", "brand": get_brand("clawshow")}, headers=cors)

    installments = db.get_installments(record["plan_id"])
    installment_no = record["installment_no"]

    # Amount/currency come from token record (stored at creation time)
    token_amount = record.get("amount") or 0.0
    token_currency = record.get("currency") or plan.get("currency", "EUR")

    # Find matching installment (installment_no=0 means subscription first charge)
    target = next((i for i in installments if i["installment_number"] == installment_no), None)
    # For subscription (installment_no=0), fall back to first scheduled installment for due_date
    if target is None and installment_no == 0 and installments:
        target = installments[0]

    brand = get_brand(record["namespace"])

    # Determine payment status
    token_status = record.get("token_status", "valid")
    if token_status == "paid" or record.get("paid_at"):
        pay_status = "paid"
    elif target and target["status"] == "charged":
        pay_status = "paid"
        mark_token_paid(token)
    elif target and target["status"] == "failed":
        pay_status = "failed"
    else:
        pay_status = "pending"

    total = plan["installments"]
    total_label = "∞" if total == -1 else str(total)

    # Build history (max 20 entries for UX)
    history = []
    for inst in installments[:20]:
        if inst["status"] == "charged":
            st = "paid"
        elif inst["status"] == "cancelled":
            st = "cancelled"
        elif inst["status"] == "failed":
            st = "failed"
        elif inst["installment_number"] == installment_no:
            st = pay_status
        else:
            st = "upcoming"
        history.append({
            "installment_no": inst["installment_number"],
            "amount": inst["amount"],
            "due_date": inst["scheduled_date"],
            "due_date_fr": _format_date_fr(inst["scheduled_date"]),
            "status": st,
        })

    logger.info("billing_public_get: token=**** plan=%s inst=%d status=%s ip=%s",
                record["plan_id"], installment_no, pay_status, ip)

    # Top-level status so frontend can dispatch on it
    # "paid" / "pending" / "failed" map to frontend states; "ready" = pending (unpaid)
    top_status = "paid" if pay_status == "paid" else "ready"

    return JSONResponse({
        "status": top_status,
        "namespace": record["namespace"],
        "brand": brand,
        "payment": {
            "customer_name": plan.get("customer_name", ""),
            "amount": token_amount,
            "currency": token_currency,
            "description": plan.get("description", ""),
            "installment_no": installment_no,
            "total_installments": total,
            "total_installments_label": total_label,
            "due_date": target["scheduled_date"] if target else None,
            "due_date_fr": _format_date_fr(target["scheduled_date"]) if target else "",
            "status": pay_status,
        },
        "history": history,
        "gateway": plan.get("gateway", "mollie"),
        "expires_at": record["expires_at"],
    }, headers=cors)


# ---- POST /api/billing/public/{token}/pay -----------------------------------

async def billing_public_pay(request: Request) -> JSONResponse:
    """
    Create a Mollie checkout payment for this installment.
    Returns {checkout_url, payment_id}.
    """
    token = request.path_params["token"]
    ip = _client_ip(request)
    cors = _cors_headers(request)

    if not _check_rate_limit(ip, token):
        return JSONResponse({"error": "rate_limited"}, status_code=429, headers=cors)

    record = validate_token(token)
    if not record:
        return JSONResponse({"error": "token_expired"}, status_code=410, headers=cors)

    # Prevent double-payment: if already used AND paid, reject
    if record.get("paid_at"):
        return JSONResponse({"error": "already_paid"}, status_code=409, headers=cors)

    db = BillingDB()
    plan = db.get_plan(record["plan_id"], record["namespace"])
    if not plan:
        return JSONResponse({"error": "plan_not_found"}, status_code=404, headers=cors)

    installment_no = record["installment_no"]

    # Check if installment already charged (for fixed-plan tokens, installment_no >= 1)
    if installment_no >= 1:
        installments = db.get_installments(record["plan_id"])
        target = next((i for i in installments if i["installment_number"] == installment_no), None)
        if target and target["status"] == "charged":
            mark_token_paid(token)
            return JSONResponse({"error": "already_paid"}, status_code=409, headers=cors)

    # Use amount/currency from token record (authoritative)
    token_amount = record.get("amount") or 0.0
    token_currency = record.get("currency") or plan.get("currency", "EUR")

    gateway = plan.get("gateway", "mollie")
    mode = plan.get("gateway_mode", "test")

    base_url = os.environ.get("PAYMENT_PAGE_BASE_URL", "https://clawshow.ai/pay/")
    redirect_url = f"{base_url}{token}?result=success"
    webhook_url = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai") + "/webhooks/mollie"

    if gateway == "mollie":
        try:
            result = _create_mollie_payment(
                plan=plan,
                amount=token_amount,
                currency=token_currency,
                installment_no=installment_no,
                token=token,
                mode=mode,
                redirect_url=redirect_url,
                webhook_url=webhook_url,
                namespace=record["namespace"],
            )
        except Exception as exc:
            logger.error("Mollie payment creation failed for plan=%s: %s", plan["plan_id"], exc)
            return JSONResponse({"error": f"gateway_error: {exc}"}, status_code=502, headers=cors)
    else:
        return JSONResponse({"error": f"gateway {gateway} not yet supported on payment page"}, status_code=501, headers=cors)

    # Mark token as used (prevents replay, not yet paid)
    mark_token_used(token, result["payment_id"])

    logger.info("billing_public_pay: plan=%s inst=%d payment=%s ip=%s",
                plan["plan_id"], installment_no, result["payment_id"], ip)

    return JSONResponse({
        "checkout_url": result["checkout_url"],
        "payment_id": result["payment_id"],
    }, headers=cors)


def _create_mollie_payment(
    plan: dict,
    amount: float,
    currency: str,
    installment_no: int,
    token: str,
    mode: str,
    redirect_url: str,
    webhook_url: str,
    namespace: str,
) -> Dict:
    """Create a Mollie one-off checkout payment for a specific installment."""
    from adapters.mollie.customer import _get_client

    mollie = _get_client(mode)
    description = plan.get("description") or f"Paiement {namespace}"

    payment = mollie.payments.create({
        "amount": {"currency": currency.upper(), "value": f"{amount:.2f}"},
        "description": description,
        "redirectUrl": redirect_url,
        "webhookUrl": webhook_url,
        "metadata": {
            "token": token,
            "plan_id": plan["plan_id"],
            "installment_no": installment_no,
            "namespace": namespace,
        },
    })

    return {
        "payment_id": payment.id,
        "checkout_url": payment.checkout_url,
    }


# ---- GET /api/billing/public/{token}/status ---------------------------------

async def billing_public_status(request: Request) -> JSONResponse:
    """
    Poll payment status for this token.
    Used by frontend after Mollie redirect.
    """
    token = request.path_params["token"]
    ip = _client_ip(request)
    cors = _cors_headers(request)

    if not _check_rate_limit(ip, token):
        return JSONResponse({"error": "rate_limited"}, status_code=429, headers=cors)

    record = get_token_record(token)
    if not record:
        return JSONResponse({"status": "expired"}, headers=cors)

    # If already marked paid
    if record.get("paid_at"):
        return JSONResponse({"status": "paid", "paid_at": record["paid_at"]}, headers=cors)

    db = BillingDB()
    installments = db.get_installments(record["plan_id"])
    target = next(
        (i for i in installments if i["installment_number"] == record["installment_no"]),
        None,
    )
    if not target:
        return JSONResponse({"status": "pending"}, headers=cors)

    if target["status"] == "charged":
        mark_token_paid(token)
        return JSONResponse({"status": "paid", "paid_at": target.get("charged_at", "")}, headers=cors)
    elif target["status"] == "failed":
        return JSONResponse({"status": "failed"}, headers=cors)
    else:
        return JSONResponse({"status": "pending"}, headers=cors)


# ---- OPTIONS preflight -------------------------------------------------------

async def billing_public_options(request: Request) -> JSONResponse:
    cors = _cors_headers(request)
    return JSONResponse({}, headers=cors)
