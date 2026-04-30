"""
ClawShow MCP Server
-------------------
Hosts: mcp.clawshow.ai/sse

Transport:
  - SSE (production on Railway, also local via http)
  - stdio (local Claude Desktop testing)

Endpoints (SSE mode):
  GET /sse              — MCP SSE stream
  GET /stats            — Tool call counts (JSON, CORS-enabled)
  POST /webhook/stripe  — Stripe payment webhook
  POST /api/booking     — Create restaurant booking
  GET /api/bookings     — Query bookings
  GET /api/bookings/summary — Daily booking summary

Usage:
  Local SSE:   python server.py
  Local stdio: python server.py --stdio
"""

import os
import json
import argparse
import smtplib
import ssl
import logging
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP

# SaaS auth / accounts / subscriptions / api-keys
from tools.auth import auth_request_login, auth_verify, auth_logout
from tools.accounts import accounts_me, accounts_create, accounts_get, internal_invite_founding
from tools.subscriptions import subscriptions_current, subscriptions_upgrade_intent
from tools.api_keys import api_keys_create, api_keys_list, api_keys_revoke
from tools.webhooks import webhooks_config_get, webhooks_config_patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_real_ip(request: Request) -> str:
    """Extract real client IP — prefers X-Forwarded-For over X-Real-IP over client.host."""
    xff = request.headers.get('x-forwarded-for', '')
    if xff:
        first = xff.split(',')[0].strip()
        if first and first != '127.0.0.1':
            return first
    real = request.headers.get('x-real-ip', '')
    if real and real != '127.0.0.1':
        return real
    return request.client.host if request.client else 'unknown'


# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------

_port = int(os.environ.get("PORT", 8000))
_host = os.environ.get("HOST", "0.0.0.0")

mcp = FastMCP(
    name="ClawShow",
    instructions=(
        "ClawShow is a discovery and invocation layer for AI-ready skills. "
        "Use the available tools to generate rental websites, extract finance fields, "
        "and more. Each tool is narrow, well-defined, and returns structured output."
    ),
    host=_host,
    port=_port,
)

# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

USAGE_LOG_PATH = Path(__file__).parent / "data" / "usage_log.json"


def _record_call(tool_name: str, meta: dict | None = None) -> None:
    """Append one record to usage_log.json every time a tool is called."""
    USAGE_LOG_PATH.parent.mkdir(exist_ok=True)
    records: list = []
    if USAGE_LOG_PATH.exists():
        try:
            records = json.loads(USAGE_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            records = []
    records.append(
        {
            "tool": tool_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(meta or {}),
        }
    )
    USAGE_LOG_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

from tools.rental_website import register as _register_rental_website
from tools.finance_extract import register as _register_finance_extract
from tools.notification import register as _register_notification
from tools.orders import register as _register_orders
from tools.business_page import register as _register_business_page
from tools.inventory import register as _register_inventory
from tools.report import register as _register_report
from tools.bookings import register as _register_bookings
from tools.payment import register as _register_payment
from tools.esign import register as _register_esign
from tools.billing import register as _register_billing

_register_rental_website(mcp, _record_call)
_register_finance_extract(mcp, _record_call)
_register_notification(mcp, _record_call)
_register_orders(mcp, _record_call)
_register_business_page(mcp, _record_call)
_register_inventory(mcp, _record_call)
_register_report(mcp, _record_call)
_register_bookings(mcp, _record_call)
_register_payment(mcp, _record_call)
_register_esign(mcp, _record_call)
_register_billing(mcp, _record_call)

# ---------------------------------------------------------------------------
# /stats endpoint
# ---------------------------------------------------------------------------

async def stats(request: Request) -> JSONResponse:
    """
    GET /stats
    Returns total call count and per-tool breakdown.

    Response:
        {
          "total": 5,
          "tools": {
            "generate_rental_website": 5
          }
        }
    """
    records: list = []
    if USAGE_LOG_PATH.exists():
        try:
            records = json.loads(USAGE_LOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    counts: dict[str, int] = {}
    for r in records:
        tool = r.get("tool", "unknown")
        counts[tool] = counts.get(tool, 0) + 1

    return JSONResponse({"total": len(records), "tools": counts})


# ---------------------------------------------------------------------------
# Stripe webhook endpoint
# ---------------------------------------------------------------------------

PAYMENTS_DIR = Path(__file__).parent / "data" / "payments"




async def sumup_webhook_handler(request: Request) -> JSONResponse:
    """POST /webhook/{namespace}/sumup — inbound SumUp payment webhook."""
    namespace = request.path_params.get("namespace", "")
    try:
        raw_body = await request.body()
        parsed = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)

    signature = request.headers.get("x-sumup-signature", "")
    webhook_secret = _os.environ.get("SUMUP_WEBHOOK_SECRET", "mock_secret_for_demo")

    from adapters.sumup.webhook import handle_sumup_webhook
    result = handle_sumup_webhook(
        namespace=namespace,
        raw_payload=raw_body,
        parsed_body=parsed,
        signature=signature or None,
        webhook_secret=webhook_secret,
    )

    status_code = result.pop("_status", 200)
    return JSONResponse(result, status_code=status_code)




async def api_order_status(request: Request) -> JSONResponse:
    """GET /api/order/{id}/status?namespace=x — get payment status of a dine order."""
    order_id_str = request.path_params.get("id", "")
    namespace = request.query_params.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace required"}, status_code=400)
    try:
        order_id = int(order_id_str)
    except ValueError:
        return JSONResponse({"error": "order_id must be integer"}, status_code=400)

    with db.get_conn() as _conn:
        row = _conn.execute(
            """SELECT id, order_number, payment_status, payment_mode, payment_id,
                      payment_provider, sumup_checkout_id, sumup_external_id, updated_at
               FROM dine_orders WHERE id=? AND namespace=?""",
            (order_id, namespace),
        ).fetchone()
    if not row:
        return JSONResponse({"error": "Order not found"}, status_code=404)

    return JSONResponse({
        "id": row["id"],
        "order_number": row["order_number"],
        "payment_status": row["payment_status"],
        "payment_mode": row["payment_mode"],
        "payment_provider": row["payment_provider"] or "stancer",
        "payment_id": row["payment_id"],
        "sumup_checkout_id": row["sumup_checkout_id"],
        "updated_at": row["updated_at"],
    })



async def mock_checkout_page(request: Request) -> HTMLResponse:
    """GET /mock-checkout/{checkout_id}?return_url=...&namespace=...&order_id=...
    Demo checkout page for SumUp mock mode. Shows amount + yellow [模拟支付成功] button.
    Only served when SUMUP_MODE=mock.
    """
    if _os.environ.get("SUMUP_MODE", "mock") != "mock":
        return JSONResponse({"error": "Not available in live mode"}, status_code=403)

    checkout_id = request.path_params.get("checkout_id", "")
    return_url = request.query_params.get("return_url", "")
    namespace = request.query_params.get("namespace", "")
    order_id = request.query_params.get("order_id", "")
    amount_str = request.query_params.get("amount", "")
    currency = request.query_params.get("currency", "EUR")

    trigger_url = f"/api/dev/mock-payment-success/{namespace}/{order_id}" if namespace and order_id else ""

    _merchant = " ".join(w.capitalize() for w in namespace.replace("-", " ").replace("_", " ").split()) if namespace else "Boutique"
    _amount_display = f"{float(amount_str):.2f} {currency}" if amount_str else f"--- {currency}"
    _trigger_js = f"const _r = await fetch('/api/dev/mock-payment-success/{namespace}/{order_id}', {{method: 'POST'}}); if (!_r.ok) throw new Error('trigger ' + _r.status);" if namespace and order_id else ""
    _redirect_js = f"window.location.href = {repr(return_url)};" if return_url else "window.location.reload();"

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Paiement &#8212; {_merchant}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f7f8fa;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;padding:16px}}
    .header{{display:flex;align-items:center;gap:10px;margin-bottom:24px}}
    .sumup-logo{{background:#1b3b6f;color:#fff;font-weight:800;font-size:18px;padding:6px 14px;border-radius:8px;letter-spacing:1px}}
    .secure{{font-size:13px;color:#666}}
    .card{{background:#fff;border-radius:16px;padding:28px 24px;max-width:400px;width:100%;box-shadow:0 2px 16px rgba(0,0,0,.08)}}
    .merchant{{font-size:14px;color:#888;text-align:center;margin-bottom:4px}}
    .amount{{font-size:42px;font-weight:700;color:#111;text-align:center;margin-bottom:18px}}
    .demo-banner{{background:#fff3cd;border:1px solid #ffc107;border-radius:8px;font-size:12px;color:#856404;padding:6px 12px;text-align:center;margin-bottom:18px}}
    .alt-pay{{display:flex;gap:10px;margin-bottom:14px}}
    .alt-btn{{flex:1;height:44px;border-radius:8px;border:1.5px solid #ddd;background:#f0f0f0;color:#bbb;font-size:14px;font-weight:600;display:flex;align-items:center;justify-content:center;gap:6px;opacity:.5;cursor:not-allowed;user-select:none}}
    .divider{{display:flex;align-items:center;gap:10px;margin:12px 0;color:#bbb;font-size:13px}}
    .divider::before,.divider::after{{content:'';flex:1;height:1px;background:#e8e8e8}}
    .field-group{{margin-bottom:12px}}
    label{{font-size:12px;color:#666;display:block;margin-bottom:4px;font-weight:500}}
    .field{{width:100%;padding:12px 14px;border:1.5px solid #ddd;border-radius:8px;font-size:15px;color:#333;background:#fafafa;outline:none}}
    .field-row{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
    .btn{{width:100%;padding:15px;background:#f5a623;color:#fff;border:none;border-radius:10px;font-size:17px;font-weight:700;cursor:pointer;margin-top:6px;transition:background .15s}}
    .btn:hover:not(:disabled){{background:#e09000}}
    .btn:disabled{{opacity:.7;cursor:not-allowed}}
    .footer{{font-size:11px;color:#bbb;text-align:center;margin-top:14px}}
  </style>
</head>
<body>
  <div class="header">
    <span class="sumup-logo">SumUp</span>
    <span class="secure">&#128274; Paiement s&#233;curis&#233;</span>
  </div>
  <div class="card">
    <div class="merchant">{_merchant}</div>
    <div class="amount">{_amount_display}</div>
    <div class="demo-banner">&#9888; Mode d&#233;mo &#8212; aucun paiement r&#233;el</div>
    <div class="alt-pay">
      <div class="alt-btn">&#63743; Apple Pay</div>
      <div class="alt-btn">G&amp;nbsp;Google Pay</div>
    </div>
    <div class="divider">ou</div>
    <div class="field-group">
      <label>Num&#233;ro de carte</label>
      <input class="field" type="text" value="4242 4242 4242 4242" readonly>
    </div>
    <div class="field-row">
      <div class="field-group">
        <label>Date d&#39;expiration</label>
        <input class="field" type="text" value="12/30" readonly>
      </div>
      <div class="field-group">
        <label>CVV</label>
        <input class="field" type="text" value="123" readonly>
      </div>
    </div>
    <button class="btn" id="simBtn" onclick="simulatePay()">&#128179; Simuler paiement accept&#233;</button>
  </div>
  <div class="footer">Mode d&#233;mo &middot; Aucun paiement r&#233;el &middot; R&#233;f: {checkout_id[:16]}</div>
  <script>
    async function simulatePay() {{
      const btn = document.getElementById('simBtn');
      btn.disabled = true;
      btn.textContent = 'Traitement en cours...';
      try {{
        const params = new URLSearchParams(window.location.search);
        const ns  = params.get('namespace');
        const oid = params.get('order_id');
        const ret = params.get('return_url');
        if (ns && oid) {{
          const r = await fetch('/api/dev/mock-payment-success/' + ns + '/' + oid, {{method: 'POST'}});
          if (!r.ok) throw new Error('mock-trigger HTTP ' + r.status);
        }}
        btn.textContent = 'Paiement accepté !';
        btn.style.background = '#28a745';
        setTimeout(() => {{
          if (ret) {{
            window.location.href = ret;
          }} else {{
            btn.textContent = 'Paiement accepté — return_url manquant';
          }}
        }}, 1200);
      }} catch(e) {{
        btn.disabled = false;
        btn.textContent = 'Erreur : ' + e.message;
        btn.style.background = '#dc3545';
      }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(html)

async def mock_payment_success(request: Request) -> JSONResponse:
    """POST /api/dev/mock-payment-success/{namespace}/{order_id}

    Demo trigger: manually mark an order as paid (mock mode only).
    Used for recording demo videos — simulates a customer tapping the terminal.
    """
    if _os.environ.get("SUMUP_MODE", "mock") != "mock":
        return JSONResponse({"error": "Only available in mock mode"}, status_code=403)

    namespace = request.path_params.get("namespace", "")
    order_id_str = request.path_params.get("order_id", "")
    try:
        order_id = int(order_id_str)
    except ValueError:
        return JSONResponse({"error": "order_id must be an integer"}, status_code=400)

    import logging as _log_mod
    _mock_log = _log_mod.getLogger("mock_payment_success")
    _caller_ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for") or "unknown"

    # dragons-elysees uses its own DB (orders table), not the generic dine_orders table
    if namespace == "dragons-elysees":
        if _de_db is None:
            return JSONResponse({"error": "Dragons DB unavailable"}, status_code=503)
        order = _de_db.get_order_by_id(order_id)
        if not order:
            return JSONResponse({"error": "Order not found"}, status_code=404)
        _de_db.update_order_status(order_id, "paid")
        _de_db.apply_cashback(order_id)
        _mock_log.info(f"[MOCK] Payment success triggered: {namespace}/order {order_id} caller_ip={_caller_ip}")
        return JSONResponse({
            "success": True,
            "order_id": order_id,
            "namespace": namespace,
            "payment_status": "paid",
            "mock": True,
        })

    # all other namespaces use the generic dine_orders table
    result = db.update_dine_order_payment_status(namespace, order_id, "paid")
    if not result.get("success"):
        return JSONResponse({"error": result.get("error", "Order not found")}, status_code=404)

    _mock_log.info(f"[MOCK] Payment success triggered: {namespace}/order {order_id} caller_ip={_caller_ip}")
    return JSONResponse({
        "success": True,
        "order_id": order_id,
        "namespace": namespace,
        "payment_status": "paid",
        "mock": True,
    })


async def mollie_webhook(request: Request) -> JSONResponse:
    """
    POST /webhooks/mollie
    Mollie POSTs {"id": "tr_xxx"} (form or JSON) when a payment status changes.
    """
    payment_id = ""
    try:
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            data = await request.json()
            payment_id = data.get("id", "")
        else:
            body = await request.form()
            payment_id = body.get("id", "")
    except Exception:
        pass

    if not payment_id:
        return JSONResponse({"error": "missing payment id"}, status_code=400)

    from adapters.mollie.webhook_handler import handle_mollie_webhook
    result = await handle_mollie_webhook(payment_id)
    # Mollie expects 2xx; always return 200 to avoid Mollie retrying
    return JSONResponse({"ok": True, "result": result})


async def stripe_webhook(request: Request) -> JSONResponse:
    """
    POST /webhook/stripe
    Handles Stripe checkout.session.completed events.
    Writes payment record to data/payments/{session_id}.json.
    """
    import stripe

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not webhook_secret:
        return JSONResponse({"error": "STRIPE_WEBHOOK_SECRET not configured"}, status_code=500)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.SignatureVerificationError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if event["type"] == "checkout.session.completed":
        # StripeObject doesn't support dict() — use to_dict_recursive()
        raw = event["data"]["object"]
        session = raw.to_dict_recursive() if hasattr(raw, "to_dict_recursive") else raw.to_dict() if hasattr(raw, "to_dict") else dict(vars(raw).get("_data", {}))
        PAYMENTS_DIR.mkdir(parents=True, exist_ok=True)
        customer_details = session.get("customer_details") or {}
        metadata = session.get("metadata") or {}
        record = {
            "session_id":     session.get("id"),
            "amount":         session.get("amount_total"),
            "currency":       session.get("currency"),
            "customer_email": customer_details.get("email"),
            "metadata":       metadata,
            "completed_at":   datetime.now(timezone.utc).isoformat(),
        }
        out = PAYMENTS_DIR / f"{session.get('id', 'unknown')}.json"
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        # Auto-mark linked order as paid
        order_id = metadata.get("order_id")
        if order_id:
            from tools.orders import webhook_mark_paid
            webhook_mark_paid(order_id)

    return JSONResponse({"received": True})


async def stripe_billing_webhook(request: Request) -> JSONResponse:
    """
    POST /webhooks/stripe
    Stripe billing subscription events (IESIG sandbox, 18 events).
    Signature verified with STRIPE_WEBHOOK_SECRET_IESIG_TEST.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    from adapters.stripe.webhook_handler import handle_stripe_billing_webhook
    result = await handle_stripe_billing_webhook(payload, sig_header, mode="test")
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error", "unknown")}, status_code=400)
    return JSONResponse({"ok": True, "result": result})


async def billing_esign_webhook(request: Request) -> JSONResponse:
    """
    POST /webhooks/esign
    Called by ClawShow eSign engine when a contract is signed/declined/expired.
    Expected payload: {plan_id, namespace, status, document_id, ...}
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    from engines.billing_engine.esign_integration import handle_esign_callback
    result = await handle_esign_callback(payload)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# PDF report serving
# ---------------------------------------------------------------------------

REPORTS_DIR = Path(__file__).parent / "data" / "reports"


async def serve_report(request: Request):
    """GET /reports/{namespace}/{filename} — serve generated PDF files."""
    from starlette.responses import FileResponse
    namespace = request.path_params["namespace"]
    filename = request.path_params["filename"]
    filepath = REPORTS_DIR / namespace / filename
    if not filepath.exists() or ".." in filename:
        return JSONResponse({"error": "Report not found"}, status_code=404)
    return FileResponse(str(filepath), media_type="application/pdf")


# ---------------------------------------------------------------------------
# Booking API endpoints (REST, for frontend forms)
# ---------------------------------------------------------------------------

import db

logger = logging.getLogger("clawshow")


def _send_booking_email(data: dict, booking_code: str, deposit_amount: float = 0) -> None:
    """Send booking confirmation email via SMTP (runs in background thread)."""
    try:
        email_to = data.get("customer_email", "")
        if not email_to:
            return

        host = os.getenv("SMTP_HOST", "")
        port = int(os.getenv("SMTP_PORT", "465"))
        user = os.getenv("SMTP_USER", "")
        pwd = os.getenv("SMTP_PASS", "")
        from_name = os.getenv("SMTP_FROM_NAME", "Neige Rouge")

        if not host or not user:
            logger.warning("SMTP not configured, skipping booking email")
            return

        customer_name = data.get("customer_name", "")
        booking_date = data.get("booking_date", "")
        booking_time = data.get("booking_time", "")
        items = data.get("items", [])
        total = data.get("total", 0)
        order_type = "Sur place / 堂食" if data.get("type") == "surPlace" else "À emporter / 外带"
        notes = data.get("notes", "")

        items_html = ""
        for item in items:
            opts = item.get("options") or {}
            opts_str = ", ".join(v for v in opts.values() if v) if opts else ""
            name_cell = item.get("name", "")
            if opts_str:
                name_cell += f'<br><span style="font-size:12px;color:#888">{opts_str}</span>'
            items_html += f'<tr><td style="padding:6px 12px;border-bottom:1px solid #eee">{name_cell}</td><td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center">×{item.get("qty",1)}</td><td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right">{item.get("price",0):.2f}€</td></tr>'

        html = f"""
        <div style="max-width:520px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
          <div style="background:#8B0000;padding:24px;text-align:center;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:22px">Neige Rouge 红雪餐厅</h1>
            <p style="color:rgba(255,255,255,0.8);margin:8px 0 0;font-size:13px">Réservation confirmée · 预订已确认</p>
          </div>
          <div style="background:white;padding:28px;border:1px solid #eee;border-top:none">
            <div style="text-align:center;margin-bottom:24px">
              <div style="font-size:13px;color:#999;margin-bottom:4px">N° de réservation · 预订号</div>
              <div style="font-size:42px;font-weight:900;color:#8B0000;letter-spacing:6px">#{booking_code}</div>
            </div>
            <table style="width:100%;border-collapse:collapse;margin-bottom:20px">
              <tr><td style="padding:8px 0;color:#999;width:40%">Date · 日期</td><td style="padding:8px 0;font-weight:600">{booking_date}</td></tr>
              <tr><td style="padding:8px 0;color:#999">Heure · 时间</td><td style="padding:8px 0;font-weight:600">{booking_time}</td></tr>
              <tr><td style="padding:8px 0;color:#999">Type · 类型</td><td style="padding:8px 0;font-weight:600">{order_type}</td></tr>
              <tr><td style="padding:8px 0;color:#999">Nom · 姓名</td><td style="padding:8px 0;font-weight:600">{customer_name}</td></tr>
              {"<tr><td style='padding:8px 0;color:#999'>Remarques · 备注</td><td style='padding:8px 0'>" + notes + "</td></tr>" if notes else ""}
            </table>
            <h3 style="font-size:14px;color:#999;text-transform:uppercase;letter-spacing:1px;margin:20px 0 8px">Commande · 订单明细</h3>
            <table style="width:100%;border-collapse:collapse">
              {items_html}
              <tr style="font-weight:700;font-size:16px"><td style="padding:12px 12px 6px" colspan="2">Total · 合计</td><td style="padding:12px 12px 6px;text-align:right;color:#8B0000">{total:.2f}€</td></tr>
            </table>
            {f"""
            <div style="margin-top:20px;padding:16px;background:#fef9c3;border-radius:10px;border:1px solid #fde68a">
              <p style="margin:0 0 8px;font-weight:700;color:#854d0e">💳 Acompte de garantie: {deposit_amount:.2f} €</p>
              <p style="margin:0 0 4px;font-size:13px;color:#78350f">→ Ce montant sera déduit de votre addition lors du repas.</p>
              <p style="margin:0;font-size:13px;color:#78350f">✓ Annulation gratuite jusqu'à 24h avant la réservation.</p>
            </div>
            """ if deposit_amount > 0 else ""}
          </div>
          <div style="background:#faf8f5;padding:20px;text-align:center;border:1px solid #eee;border-top:none;border-radius:0 0 12px 12px">
            <p style="margin:0 0 8px;font-size:14px"><strong>75 Rue Buffon, 75005 Paris</strong></p>
            <p style="margin:0 0 12px;font-size:14px">📞 01 72 60 48 89</p>
            <p style="margin:0;color:#8B0000;font-weight:600;font-size:15px">Merci pour votre commande ! 感谢您的订单！</p>
          </div>
        </div>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Réservation confirmée #{booking_code} | Neige Rouge 红雪餐厅"
        msg["From"] = f"{from_name} <{user}>"
        msg["To"] = email_to
        msg.attach(MIMEText(html, "html", "utf-8"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
        logger.info(f"Booking confirmation email sent to {email_to} (#{booking_code})")
    except Exception:
        logger.exception("Failed to send booking confirmation email")


async def api_create_booking(request: Request) -> JSONResponse:
    """POST /api/booking — create a restaurant booking from frontend form."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    result = db.create_booking(namespace, data)
    if not result.get("success"):
        return JSONResponse(result, status_code=400)

    booking_id = result["booking_id"]
    booking_code = result["booking_code"]
    payment_type = result.get("payment_type", "deposit")  # 'full' | 'deposit'
    deposit_amount = result.get("deposit_amount", 0)

    # Both payment types always require Stancer payment — mandatory, hard-fail if unavailable
    payment_url = None
    if deposit_amount > 0:
        stancer_key = _os.environ.get("STANCER_SECRET_KEY", "")
        if not stancer_key or stancer_key.startswith("stest_your"):
            return JSONResponse({"error": "payment_unavailable", "detail": "Stancer not configured"}, status_code=503)
        auth = base64.b64encode(f"{stancer_key}:".encode()).decode()
        return_url = f"https://jason2016.github.io/neige-rouge/#booking-success?booking_id={booking_id}&booking_code={booking_code}"
        if payment_type == "full":
            description = f"Commande complète reservation #{booking_code} - Neige Rouge"
        else:
            description = f"Acompte reservation #{booking_code} - Neige Rouge"
        try:
            resp = _req_lib.post(
                "https://api.stancer.com/v2/payment_intents/",
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
                json={
                    "amount": int(round(deposit_amount * 100)),
                    "currency": "eur",
                    "description": description,
                    "return_url": return_url,
                },
                timeout=15,
            )
            pi = resp.json()
        except Exception as exc:
            logger.error(f"Stancer call failed for booking {booking_id}: {exc}")
            return JSONResponse({"error": "payment_unavailable", "detail": str(exc)}, status_code=502)
        if resp.status_code not in (200, 201) or not pi.get("url"):
            err = pi.get("error", f"Stancer error {resp.status_code}")
            logger.error(f"Stancer rejected booking {booking_id}: {err} | response: {pi}")
            return JSONResponse({"error": "payment_unavailable", "detail": err}, status_code=502)
        payment_url = pi["url"]
        db.update_booking_deposit_payment(namespace, booking_id, pi.get("id", ""), "unpaid")

    # Send confirmation email (always, even if payment pending)
    threading.Thread(
        target=_send_booking_email,
        args=(data, booking_code, deposit_amount),
        daemon=True,
    ).start()

    response = {**result}
    if payment_url:
        response["payment_url"] = payment_url
    return JSONResponse(response, status_code=201)


async def api_update_booking(request: Request) -> JSONResponse:
    """PATCH /api/booking/{id} — update booking status."""
    booking_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    status = data.get("status", "")
    if not namespace or not status:
        return JSONResponse({"error": "namespace and status are required"}, status_code=400)
    result = db.update_booking_status(namespace, booking_id, status)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


async def api_checkin_booking(request: Request) -> JSONResponse:
    """PATCH /api/booking/checkin — check in by 3-digit booking code."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    code = data.get("booking_code", "")
    booking_date = data.get("booking_date", "")
    if not namespace or not code:
        return JSONResponse({"error": "namespace and booking_code are required"}, status_code=400)
    result = db.checkin_by_code(namespace, code, booking_date)
    return JSONResponse(result, status_code=200 if result.get("success") else 404)


async def api_query_bookings(request: Request) -> JSONResponse:
    """GET /api/bookings?namespace=x&date=2026-04-04&status=confirmed"""
    namespace = request.query_params.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    date = request.query_params.get("date", "")
    status = request.query_params.get("status", "")
    limit = int(request.query_params.get("limit", "50"))
    bookings = db.query_bookings(namespace, date=date, status=status, limit=limit)
    return JSONResponse({"bookings": bookings, "total": len(bookings)})


async def api_booking_summary(request: Request) -> JSONResponse:
    """GET /api/bookings/summary?namespace=x&date=2026-04-04"""
    namespace = request.query_params.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    date = request.query_params.get("date", "")
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = db.booking_summary(namespace, date)
    return JSONResponse(summary)


async def api_nr_booking_verify(request: Request) -> JSONResponse:
    """GET /api/neige-rouge/bookings/verify?booking_id=x — verify Stancer deposit payment."""
    booking_id_str = request.query_params.get("booking_id", "")
    namespace = request.query_params.get("namespace", "neige-rouge")
    if not booking_id_str:
        return JSONResponse({"error": "booking_id required"}, status_code=400)
    booking = db.get_booking_by_id(namespace, int(booking_id_str))
    if not booking:
        return JSONResponse({"error": "Booking not found"}, status_code=404)
    if booking.get("deposit_payment_status") == "paid":
        return JSONResponse({"paid": True, "deposit_amount": booking.get("deposit_amount", 0)})
    payment_id = booking.get("deposit_payment_id", "")
    if not payment_id:
        return JSONResponse({"paid": False, "status": "no_payment_id"})
    stancer_key = _os.environ.get("STANCER_SECRET_KEY", "")
    if not stancer_key or stancer_key.startswith("stest_your"):
        return JSONResponse({"error": "Stancer not configured"}, status_code=500)
    auth = base64.b64encode(f"{stancer_key}:".encode()).decode()
    import time as _time_mod
    for attempt in range(3):
        try:
            resp = _req_lib.get(
                f"https://api.stancer.com/v2/payment_intents/{payment_id}",
                headers={"Authorization": f"Basic {auth}"},
                timeout=15,
            )
            result = resp.json()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        status = result.get("status", "")
        paid = status in ("succeeded", "requires_capture", "paid", "captured", "to_capture", "authorized")
        if paid:
            db.update_booking_deposit_payment(namespace, int(booking_id_str), payment_id, "paid")
            return JSONResponse({
                "paid": True, "status": status,
                "deposit_amount": booking.get("deposit_amount", 0),
                "booking_code": booking.get("booking_code", ""),
            })
        if attempt < 2:
            _time_mod.sleep(2)
    return JSONResponse({"paid": False, "status": status})


async def api_nr_booking_use_deposit(request: Request) -> JSONResponse:
    """POST /api/neige-rouge/bookings/:id/use-deposit — deduct deposit from order total."""
    booking_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "neige-rouge")
    order_id = data.get("order_id")
    if not order_id:
        return JSONResponse({"error": "order_id required"}, status_code=400)
    result = db.use_booking_deposit(namespace, booking_id, int(order_id))
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


async def api_nr_booking_refund(request: Request) -> JSONResponse:
    """POST /api/neige-rouge/bookings/:id/refund — refund deposit via Stancer."""
    booking_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "neige-rouge")
    booking = db.get_booking_by_id(namespace, booking_id)
    if not booking:
        return JSONResponse({"error": "Booking not found"}, status_code=404)
    if booking.get("deposit_payment_status") != "paid":
        return JSONResponse({"error": "Deposit not in paid status"}, status_code=400)
    payment_id = booking.get("deposit_payment_id", "")
    if not payment_id:
        return JSONResponse({"error": "No payment_id on file"}, status_code=400)
    stancer_key = _os.environ.get("STANCER_SECRET_KEY", "")
    if not stancer_key or stancer_key.startswith("stest_your"):
        return JSONResponse({"error": "Stancer not configured"}, status_code=500)
    auth = base64.b64encode(f"{stancer_key}:".encode()).decode()
    try:
        resp = _req_lib.post(
            f"https://api.stancer.com/v2/payment_intents/{payment_id}/refund",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            json={},
            timeout=15,
        )
        if resp.status_code not in (200, 201, 204):
            err = resp.json() if resp.content else {}
            return JSONResponse({"error": err.get("error", f"Stancer refund failed {resp.status_code}")}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    result = db.mark_booking_deposit_refunded(namespace, booking_id)
    return JSONResponse({**result, "refund_amount": booking.get("deposit_amount", 0)})


async def api_nr_booking_arrive(request: Request) -> JSONResponse:
    """POST /api/neige-rouge/bookings/{id}/arrive — mark as arrived, create dine_order."""
    booking_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        data = {}
    namespace = data.get("namespace", "neige-rouge")
    result = db.arrive_booking(namespace, booking_id)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


async def api_nr_checkout(request: Request) -> JSONResponse:
    """POST /api/neige-rouge/orders/{id}/checkout — compute amount due, create Stancer payment if needed."""
    order_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "neige-rouge")

    order = db._get_dine_order(namespace, order_id)
    if not order:
        return JSONResponse({"error": "Order not found"}, status_code=404)

    total = float(order.get("total_amount") or 0)
    deposit = float(order.get("deposit_applied") or 0)
    amount_due = max(0.0, round(total - deposit, 2))

    if amount_due <= 0:
        db.update_dine_order_payment_status(namespace, order_id, "paid")
        return JSONResponse({
            "success": True,
            "order_total": total,
            "deposit_applied": deposit,
            "amount_due": 0.0,
            "payment_url": None,
        })

    stancer_key = _os.environ.get("STANCER_SECRET_KEY", "")
    if not stancer_key or stancer_key.startswith("stest_your"):
        return JSONResponse({"error": "Stancer not configured"}, status_code=503)

    auth = base64.b64encode(f"{stancer_key}:".encode()).decode()
    order_number = order.get("order_number", str(order_id))
    return_url = f"https://jason2016.github.io/neige-rouge/#payment-success?order_id={order_id}"
    try:
        resp = _req_lib.post(
            "https://api.stancer.com/v2/payment_intents/",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            json={
                "amount": int(round(amount_due * 100)),
                "currency": "eur",
                "description": f"Neige Rouge #{order_number} (acompte {deposit:.2f}EUR deduit)",
                "return_url": return_url,
            },
            timeout=15,
        )
        result = resp.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    if resp.status_code not in (200, 201):
        return JSONResponse({"error": result.get("error", f"Stancer error {resp.status_code}")}, status_code=502)

    payment_url = result.get("url", "")
    if not payment_url:
        return JSONResponse({"error": "No payment URL from Stancer"}, status_code=502)

    db.update_dine_order_payment(namespace, order_id, result.get("id", ""), "stancer")
    return JSONResponse({
        "success": True,
        "order_total": total,
        "deposit_applied": deposit,
        "amount_due": amount_due,
        "payment_url": payment_url,
    })


# ---------------------------------------------------------------------------
# Dine-in order API endpoints
# ---------------------------------------------------------------------------

async def api_create_dine_order(request: Request) -> JSONResponse:
    """POST /api/order/create — create a dine-in order."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    result = db.create_dine_order(namespace, data)
    return JSONResponse(result, status_code=201 if result.get("success") else 400)


async def api_order_queue(request: Request) -> JSONResponse:
    """GET /api/order/queue?namespace=x — get today's dine-in orders for kitchen."""
    namespace = request.query_params.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    status = request.query_params.get("status", "")
    orders = db.query_dine_orders(namespace, status=status)
    return JSONResponse({"orders": orders, "total": len(orders)})


async def api_order_complete(request: Request) -> JSONResponse:
    """PATCH /api/order/{id}/complete — mark order as ready for pickup."""
    order_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    result = db.update_dine_order_status(namespace, order_id, "ready")
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


async def api_order_picked(request: Request) -> JSONResponse:
    """PATCH /api/order/{id}/picked — mark order as picked up."""
    order_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    result = db.update_dine_order_status(namespace, order_id, "picked")
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


async def api_order_mark_printed(request: Request) -> JSONResponse:
    """PATCH /api/order/{id}/mark-printed — record that kitchen ticket was printed."""
    order_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    result = db.mark_dine_order_printed(namespace, order_id)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


# ---------------------------------------------------------------------------
# Neige Rouge — Receipt & Invoice PDF endpoints
# ---------------------------------------------------------------------------

from tools.neige_rouge_receipt import generate_receipt as _nr_generate_receipt
from tools.neige_rouge_receipt import generate_invoice as _nr_generate_invoice
from starlette.responses import Response as StarletteResponse


async def api_nr_booking_receipt(request: Request) -> StarletteResponse:
    """GET /api/neige-rouge/bookings/{id}/receipt — receipt PDF directly from booking (pre-arrival)."""
    booking_id = int(request.path_params["id"])
    namespace = request.query_params.get("namespace", "neige-rouge")
    try:
        booking = db.get_booking_by_id(namespace, booking_id)
        if not booking:
            return JSONResponse({"error": "Booking not found"}, status_code=404)
        # Build an order-like dict for the receipt generator
        items = booking.get("items") or []
        if isinstance(items, str):
            import json as _json
            items = _json.loads(items)
        order_dict = {
            "items": items,
            "total_amount": float(booking.get("total") or 0),
            "order_number": f"R-{booking.get('booking_code', '???')}",
            "receipt_number": f"R-{booking.get('booking_code', '???')}",
            "order_type": "dine_in",
            "payment_method": "stancer",
            "payment_id": booking.get("deposit_payment_id", ""),
            "created_at": booking.get("created_at", ""),
        }
        pdf_bytes = _nr_generate_receipt(order_dict)
        filename = f"recu-R-{booking.get('booking_code', booking_id)}.pdf"
        return StarletteResponse(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_nr_receipt(request: Request) -> StarletteResponse:
    """GET /api/neige-rouge/orders/{id}/receipt — generate receipt PDF (idempotent)."""
    order_id = int(request.path_params["id"])
    namespace = request.query_params.get("namespace", "neige-rouge")
    try:
        receipt_number = db.get_or_assign_nr_receipt_number(namespace, order_id)
        order = db._get_dine_order(namespace, order_id)
        if not order:
            return JSONResponse({"error": "Order not found"}, status_code=404)
        order["receipt_number"] = receipt_number
        pdf_bytes = _nr_generate_receipt(order)
        filename = f"recu-{receipt_number}.pdf"
        return StarletteResponse(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_nr_invoice_get(request: Request) -> JSONResponse:
    """GET /api/neige-rouge/orders/{id}/invoice — check if invoice exists."""
    order_id = int(request.path_params["id"])
    namespace = request.query_params.get("namespace", "neige-rouge")
    record = db.get_nr_invoice_record(namespace, order_id)
    if not record:
        return JSONResponse({"exists": False}, status_code=200)
    return JSONResponse({"exists": True, "invoice_number": record["invoice_number"], "client_company": record["client_company"]})


async def api_nr_invoice_post(request: Request) -> StarletteResponse:
    """POST /api/neige-rouge/orders/{id}/invoice — create (or re-download) invoice PDF."""
    order_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "neige-rouge")
    client_company = data.get("client_company", "").strip()
    client_address = data.get("client_address", "").strip()
    client_vat_number = data.get("client_vat_number", "").strip()
    if not client_company or not client_address:
        return JSONResponse({"error": "client_company and client_address are required"}, status_code=400)
    try:
        record = db.create_nr_invoice_record(namespace, order_id, client_company, client_address, client_vat_number)
        order = db._get_dine_order(namespace, order_id)
        if not order:
            return JSONResponse({"error": "Order not found"}, status_code=404)
        pdf_bytes = _nr_generate_invoice(
            order,
            client_company=record["client_company"],
            client_address=record["client_address"],
            client_vat_number=record.get("client_vat_number", ""),
            invoice_number=record["invoice_number"],
        )
        filename = f"facture-{record['invoice_number']}.pdf"
        return StarletteResponse(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


import base64
import requests as _req_lib
import os as _os

async def api_payment_create(request: Request) -> JSONResponse:
    """POST /api/payment/create — create a payment for a dine order (Stancer or SumUp)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    namespace = data.get("namespace", "")
    order_id = data.get("order_id")
    payment_mode = (data.get("payment_mode") or "").strip()
    amount_raw = data.get("amount", 0)
    description = data.get("description", "Commande Neige Rouge")

    if not namespace or not order_id:
        return JSONResponse({"error": "namespace, order_id required"}, status_code=400)
    if not amount_raw and payment_mode != "cash":
        return JSONResponse({"error": "amount required"}, status_code=400)

    # SumUp path — triggered by explicit payment_mode
    if payment_mode:
        import logging as _logging
        _log = _logging.getLogger("api_payment_create")
        try:
            from tools.payment import _create_sumup_by_mode
            # amount: PWA can pass euros (float) or cents (int) — normalize to cents
            if isinstance(amount_raw, float) or (isinstance(amount_raw, (int, float)) and amount_raw < 500):
                amount_cents = int(round(float(amount_raw) * 100))
            else:
                amount_cents = int(amount_raw)
            ext_ref = str(order_id)
            result = _create_sumup_by_mode(
                namespace=namespace,
                amount=amount_cents,
                currency=data.get("currency", "EUR"),
                description=description,
                payment_mode=payment_mode,
                device_id=data.get("device_id") or None,
                return_url=data.get("return_url") or None,
                external_reference=ext_ref,
                items=data.get("items"),
            )
            if result.get("success"):
                # Record on order
                db._ensure_sumup_schema()
                db.update_dine_order_sumup(
                    namespace,
                    int(order_id),
                    payment_mode,
                    result.get("payment_id", ""),
                    ext_ref,
                )
                # For online mock: append query params so /mock-checkout page can show amount + trigger success
                if result.get("is_mock") and payment_mode == "online" and result.get("payment_url"):
                    from urllib.parse import urlencode as _urlencode
                    _qp = {"namespace": namespace, "order_id": str(order_id), "amount": f"{amount_cents / 100:.2f}", "currency": data.get("currency", "EUR")}
                    _ret = data.get("return_url")
                    if _ret:
                        _qp["return_url"] = _ret
                    result["payment_url"] = result["payment_url"] + "?" + _urlencode(_qp)
            return JSONResponse(result)
        except Exception as e:
            _log.error(f"api_payment_create SumUp error: {e}", exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=502)

    # Stancer path (existing behavior, preserved)
    amount = amount_raw
    stancer_key = _os.environ.get("STANCER_SECRET_KEY", "")
    stancer_pub = _os.environ.get("STANCER_PUBLIC_KEY", "")
    if not stancer_key or stancer_key.startswith("stest_your"):
        return JSONResponse({"error": "Stancer not configured"}, status_code=500)
    auth = base64.b64encode(f"{stancer_key}:".encode()).decode()
    return_url = f"https://jason2016.github.io/neige-rouge/#payment-success?order_id={order_id}"
    try:
        resp = _req_lib.post(
            "https://api.stancer.com/v2/payment_intents/",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            json={"amount": int(amount), "currency": "eur", "description": description, "return_url": return_url},
            timeout=15,
        )
        result = resp.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    if resp.status_code not in (200, 201):
        return JSONResponse({"error": result.get("error", f"Stancer error {resp.status_code}")}, status_code=502)
    payment_id = result.get("id", "")
    payment_url = result.get("url", "")
    if not payment_url:
        return JSONResponse({"error": "No payment URL returned by Stancer"}, status_code=502)
    db.update_dine_order_payment(namespace, int(order_id), payment_id, "stancer")
    return JSONResponse({"success": True, "payment_id": payment_id, "payment_url": payment_url})


async def api_payment_verify(request: Request) -> JSONResponse:
    """GET /api/payment/verify?namespace=x&order_id=y — verify Stancer payment for an order."""
    namespace = request.query_params.get("namespace", "")
    order_id_str = request.query_params.get("order_id", "")
    if not namespace or not order_id_str:
        return JSONResponse({"error": "namespace and order_id required"}, status_code=400)

    # Look up payment_id from DB
    import sqlite3 as _sqlite3
    with db.get_conn() as _conn:
        row = _conn.execute(
            "SELECT payment_id, payment_status FROM dine_orders WHERE id = ? AND namespace = ?",
            (int(order_id_str), namespace)
        ).fetchone()
    if not row:
        return JSONResponse({"error": "Order not found"}, status_code=404)

    # Already marked paid — return immediately
    if row["payment_status"] == "paid":
        return JSONResponse({"success": True, "paid": True, "status": "captured"})

    payment_id = row["payment_id"]
    if not payment_id:
        return JSONResponse({"success": True, "paid": False, "status": "no_payment_id"})

    stancer_key = _os.environ.get("STANCER_SECRET_KEY", "")
    if not stancer_key or stancer_key.startswith("stest_your"):
        return JSONResponse({"error": "Stancer not configured"}, status_code=500)
    auth = base64.b64encode(f"{stancer_key}:".encode()).decode()

    import time as _time
    for attempt in range(3):
        try:
            resp = _req_lib.get(
                f"https://api.stancer.com/v2/payment_intents/{payment_id}",
                headers={"Authorization": f"Basic {auth}"},
                timeout=15,
            )
            result = resp.json()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        status = result.get("status", "")
        paid = status in ("succeeded", "requires_capture", "paid", "captured", "to_capture", "authorized")
        if paid:
            try:
                db.update_dine_order_payment_status(namespace, int(order_id_str), "paid")
            except Exception:
                pass
            return JSONResponse({"success": True, "paid": True, "status": status, "payment_id": payment_id})
        # Non-final status — wait and retry
        if attempt < 2:
            _time.sleep(2)

    return JSONResponse({"success": True, "paid": False, "status": status, "payment_id": payment_id})

async def api_order_history(request: Request) -> JSONResponse:
    """GET /api/order/history?namespace=x&date=YYYY-MM-DD&status= — all dine orders for a date."""
    namespace = request.query_params.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    date = request.query_params.get("date", "")
    status = request.query_params.get("status", "")
    orders = db.query_dine_orders_history(namespace, date=date, status=status)
    return JSONResponse({"orders": orders, "total": len(orders)})


async def api_order_confirm_payment(request: Request) -> JSONResponse:
    """POST /api/order/{id}/confirm-payment — admin confirms counter payment."""
    order_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    payment_method = data.get("payment_method", "card_counter")
    amount_received = float(data.get("amount_received", 0))
    result = db.confirm_dine_order_payment(namespace, order_id, payment_method, amount_received)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


# ---------------------------------------------------------------------------
# Daily Stock Management API
# ---------------------------------------------------------------------------

async def api_inventory_get(request: Request) -> JSONResponse:
    """GET /api/inventory?namespace=&date=YYYY-MM-DD"""
    from datetime import datetime, timezone
    namespace = request.query_params.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    date_str = request.query_params.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    stock = db.get_daily_stock(namespace, date_str)
    return JSONResponse({"stock": stock, "date": date_str})


async def api_inventory_set(request: Request) -> JSONResponse:
    """POST /api/inventory/set — batch upsert daily stock limits."""
    from datetime import datetime, timezone
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    date_str = data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    items = data.get("items", [])
    if not items:
        return JSONResponse({"error": "items required"}, status_code=400)
    result = db.set_daily_stock(namespace, date_str, items)
    return JSONResponse(result)


async def api_inventory_restore_yesterday(request: Request) -> JSONResponse:
    """POST /api/inventory/restore-yesterday — copy yesterday limits to today."""
    from datetime import datetime, timezone
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    if not namespace:
        return JSONResponse({"error": "namespace is required"}, status_code=400)
    date_str = data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    result = db.restore_yesterday_stock(namespace, date_str)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# eSign V2 endpoints
# ---------------------------------------------------------------------------

ESIGN_DATA_DIR = Path("/opt/clawshow-data/esign")
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai")



# ---------------------------------------------------------------------------
# Signing page V3 — Foxit-match UX
# All labels injected via JSON config; only __CONFIG_JSON__ and __LANG__ replaced
# ---------------------------------------------------------------------------

_SIGNING_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="__LANG__">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>ClawShow eSign</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Dancing+Script:wght@700&family=Great+Vibes&family=Caveat:wght@700&family=Pacifico&display=swap"/>
<script id="cfg" type="application/json">__CONFIG_JSON__</script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333;min-height:100vh}
button{cursor:pointer}
/* TOP BAR */
#topBar{position:fixed;top:0;left:0;right:0;z-index:100;background:#fff;border-bottom:1px solid #e0e0e0;padding:8px 16px;display:flex;align-items:center;gap:10px;box-shadow:0 2px 6px rgba(0,0,0,.08)}
#docBrand{font-size:13px;font-weight:700;color:#1976d2;flex-shrink:0}
#progWrap{flex:1;display:flex;align-items:center;gap:8px;min-width:0}
#progBar{flex:1;height:7px;background:#e8e8e8;border-radius:4px;overflow:hidden;max-width:220px}
#progFill{height:100%;width:0%;background:#28a745;border-radius:4px;transition:width .35s ease}
#reqLeftLabel{font-size:12px;color:#555;white-space:nowrap;flex-shrink:0}
#topActions{display:flex;gap:8px;position:relative;flex-shrink:0}
#btnNextField{padding:6px 12px;background:#28a745;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;white-space:nowrap}
#btnNextField.is-finish{background:#1976d2}
@keyframes finish-pulse{0%,100%{box-shadow:0 0 0 0 rgba(25,118,210,.7)}60%{box-shadow:0 0 0 10px rgba(25,118,210,0)}}
#btnNextField.is-finish{animation:finish-pulse 1.2s ease infinite}
#btnMore{padding:6px 10px;background:#fff;border:1px solid #ccc;border-radius:6px;font-size:13px;color:#444}
#moreMenu{position:absolute;right:0;top:calc(100% + 4px);background:#fff;border:1px solid #ddd;border-radius:8px;min-width:192px;box-shadow:0 4px 16px rgba(0,0,0,.14);z-index:200;overflow:hidden;display:none}
#moreMenu a{display:block;padding:10px 16px;font-size:14px;color:#333;cursor:pointer;border-bottom:1px solid #f5f5f5}
#moreMenu a:last-child{border:none}
#moreMenu a:hover{background:#f5f5f5}
#moreMenu a.danger{color:#c62828}
/* MAIN */
#appWrap{padding-top:58px;padding-bottom:60px}
.pw{max-width:840px;margin:0 auto;padding:12px}
/* PAGE VIEWER */
#pageImgBox{position:relative;display:block;width:100%}
#pageImg{width:100%;display:block;border:1px solid #ccc;box-shadow:0 2px 8px rgba(0,0,0,.1)}
#sigZones{position:absolute;inset:0;pointer-events:none}
/* SIGNATURE ZONES */
.sz{position:absolute;pointer-events:auto;transition:background .2s}
.sz.pend{border:2px dashed #E6A817;background:rgba(230,168,23,.13);border-radius:4px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:2px}
.sz.pend:hover{background:rgba(230,168,23,.23)}
@keyframes zone-pulse{0%,100%{box-shadow:0 0 0 0 rgba(230,168,23,.8)}50%{box-shadow:0 0 0 12px rgba(230,168,23,0)}}
.sz.pend.pulse{animation:zone-pulse .7s ease 3}
@keyframes btn-flash{0%,100%{background:#1976d2}50%{background:#E6A817;transform:scale(1.05)}}
.btn-pri.flash{animation:btn-flash .35s ease 3}
@keyframes field-flash{0%,100%{box-shadow:none;border-color:transparent}25%,75%{box-shadow:0 0 0 3px rgba(230,60,60,.7);border-color:#e53935;border-radius:6px}}
.field-flash{animation:field-flash .5s ease 3}
.sz.pend .zh{font-size:10px;color:#c68000;font-weight:700;text-align:center;pointer-events:none;line-height:1.2}
.sz.pend .zi{font-size:16px;pointer-events:none}
.sz.done{border:none;background:transparent}
.sz.done img{max-width:100%;max-height:100%;object-fit:contain}
/* PAGE NAV */
#pageNav{display:flex;align-items:center;justify-content:center;gap:12px;padding:10px;margin-top:6px}
.nav-btn{padding:7px 14px;border:1px solid #ccc;background:#fff;border-radius:6px;font-size:14px}
.nav-btn:disabled{opacity:.4;cursor:default}
#pageInd{font-size:14px;color:#555;min-width:80px;text-align:center}
/* FINAL SECTION */
#finalSec{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:20px;margin-top:14px}
#finalSec h3{font-size:16px;margin-bottom:14px;border-bottom:1px solid #f0f0f0;padding-bottom:10px}
.ff{margin-bottom:14px}
.ff>label{display:block;font-size:14px;font-weight:500;margin-bottom:6px;color:#444}
.ff input[type=text]{width:100%;padding:8px 12px;border:1px solid #ccc;border-radius:6px;font-size:14px}
.cbrow{display:flex;gap:8px;align-items:flex-start;padding:6px 0}
.cbrow input[type=checkbox]{width:16px;height:16px;margin-top:2px;flex-shrink:0;cursor:pointer}
.cbrow label{font-size:14px;color:#333;line-height:1.45;cursor:pointer}
.cvwrap{border:1px solid #ddd;border-radius:6px;background:#fafafa;position:relative}
.cvwrap canvas{display:block;width:100%;cursor:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'><path d='M3 21l5-2L20 7a2 2 0 00-3-3L5 16z' fill='%23333' stroke='%23fff' stroke-width='0.5'/><path d='M3 21l2-1-1-1z' fill='%23555'/></svg>") 3 21,crosshair}
.cv-clr{position:absolute;top:5px;right:8px;border:none;background:transparent;font-size:12px;color:#999;padding:2px 6px}
.use-saved-btn{margin-top:6px;padding:5px 10px;border:1px solid #1976d2;background:#e3f2fd;color:#1976d2;border-radius:5px;font-size:12px}
/* MODALS */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:300;display:flex;align-items:center;justify-content:center;padding:16px}
.modal-box{background:#fff;border-radius:12px;max-width:480px;width:100%;padding:24px;box-shadow:0 8px 32px rgba(0,0,0,.18);max-height:92vh;overflow-y:auto}
.modal-box h3{font-size:17px;margin-bottom:14px;color:#222}
/* SIG SETUP PANEL — overrides modal-bg to be a non-blocking top panel */
#sigModal{top:48px;bottom:auto;background:#fff;border-bottom:2px solid #e0e0e0;box-shadow:0 6px 20px rgba(0,0,0,.12);padding:0;align-items:flex-start}
#sigModal .modal-box{max-width:900px;border-radius:0;box-shadow:none;padding:12px 24px 16px;max-height:none;width:100%}
#sigInstr{font-size:13px;color:#555;background:#f0f4ff;border-radius:6px;padding:8px 12px;margin-bottom:10px;border-left:3px solid #1a1a2e}
/* TABS */
.mtabs{display:flex;border-bottom:2px solid #ebebeb;margin-bottom:14px}
.mtab{padding:8px 14px;background:none;border:none;font-size:14px;color:#777;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px}
.mtab.on{color:#1976d2;border-bottom-color:#1976d2;font-weight:600}
.tp{display:none}
.tp.on{display:block}
/* SIG SETUP */
.name-inp{width:100%;padding:8px 12px;border:1px solid #ccc;border-radius:6px;font-size:14px;margin-bottom:10px}
.font-row,.color-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.font-btn{padding:5px 10px;border:1px solid #ccc;border-radius:5px;background:#fff;font-size:13px}
.font-btn.on{border-color:#1976d2;background:#e3f2fd}
.col-btn{width:26px;height:26px;border-radius:50%;border:2px solid transparent}
.col-btn.on{box-shadow:0 0 0 2px #555}
#typePrev{border:1px solid #e0e0e0;border-radius:6px;height:168px;background:#fafafa;overflow:hidden;margin-bottom:10px;display:flex;align-items:center;justify-content:center}
#typePrevCv{max-width:100%}
.draw-wrap{border:1px solid #ddd;border-radius:6px;background:#fafafa;margin-bottom:6px}
#drawCv{width:100%;display:block;height:120px;cursor:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'><path d='M3 21l5-2L20 7a2 2 0 00-3-3L5 16z' fill='%23333' stroke='%23fff' stroke-width='0.5'/><path d='M3 21l2-1-1-1z' fill='%23555'/></svg>") 3 21,crosshair}
.cv-hint{font-size:12px;color:#aaa;text-align:center;margin-bottom:10px}
.upload-zone{border:2px dashed #ccc;border-radius:8px;padding:24px;text-align:center;background:#fafafa;cursor:pointer;margin-bottom:10px}
.upload-zone:hover{border-color:#1976d2;background:#f0f7ff}
#imgPrev{max-width:100%;max-height:80px;object-fit:contain;margin:6px auto 0;display:none;border-radius:4px}
/* LEGAL */
.legal-row{display:flex;gap:8px;align-items:flex-start;background:#f5f5f5;border-radius:6px;padding:10px;margin-bottom:14px}
.legal-row input{margin-top:2px;flex-shrink:0;cursor:pointer}
.legal-row label{font-size:12px;color:#555;line-height:1.4;cursor:pointer}
/* BUTTONS */
.btn-row{display:flex;justify-content:flex-end;gap:8px;margin-top:2px}
.btn-sec{padding:8px 16px;border:1px solid #ccc;background:#fff;border-radius:8px;font-size:14px;color:#333}
.btn-pri{padding:8px 16px;background:#1976d2;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600}
.btn-pri:disabled{opacity:.45;cursor:default}
.btn-danger{padding:8px 16px;background:#c62828;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600}
/* RESULT */
#resultArea{padding:60px 16px;text-align:center;max-width:500px;margin:0 auto}
.res-ok{background:#fff;border:1px solid #ddd;border-radius:12px;padding:36px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
.res-ok h2{color:#28a745;font-size:22px;margin-bottom:12px}
.res-ok p{color:#555;line-height:1.65;margin-bottom:6px}
.res-dec{background:#fff3f3;border:1px solid #fcc;border-radius:12px;padding:36px}
.res-dec h2{color:#c62828;font-size:20px;margin-bottom:10px}
@media(max-width:600px){#topBar{flex-wrap:wrap;gap:6px}#progWrap{order:3;width:100%}#reqLeftLabel{display:none}.modal-box{padding:16px}}
</style>
</head>
<body>

<!-- TOP BAR -->
<div id="topBar">
  <div id="docBrand">ClawShow eSign</div>
  <div id="progWrap">
    <div id="progBar"><div id="progFill"></div></div>
    <span id="reqLeftLabel"></span>
  </div>
  <div id="topActions">
    <button id="btnNextField" onclick="goNext()"></button>
    <button id="btnMore" onclick="toggleMore(event)"></button>
    <div id="moreMenu">
      <a id="maCN"></a>
      <a id="maCS"></a>
      <a id="maDecline" class="danger"></a>
      <a id="maDL" target="_blank"></a>
      <a id="maPrint" onclick="window.print()"></a>
    </div>
  </div>
</div>

<!-- APP AREA -->
<div id="appWrap">
  <div class="pw">
    <div id="pageImgBox">
      <img id="pageImg" alt=""/>
      <div id="sigZones"></div>
    </div>
    <div id="pageNav">
      <button class="nav-btn" id="btnPrev" onclick="gotoPage(S.cur-1)"></button>
      <span id="pageInd"></span>
      <button class="nav-btn" id="btnNext2" onclick="gotoPage(S.cur+1)"></button>
    </div>
    <!-- FINAL FIELDS (last page) -->
    <div id="finalSec" style="display:none">
      <h3 id="finalH3"></h3>
      <div class="cbrow ff"><input type="checkbox" id="cb1" onchange="onFF()"/><label for="cb1" id="lb1"></label></div>
      <div class="cbrow ff"><input type="checkbox" id="cb2" onchange="onFF()"/><label for="cb2" id="lb2"></label></div>
      <div class="ff"><label id="cityLbl"></label><input type="text" id="cityInp" oninput="onFF()"/></div>
      <div class="ff">
        <label id="luLbl"></label>
        <div class="cvwrap"><canvas id="luCv" height="110"></canvas><button class="cv-clr" id="luClrBtn" onclick="clrLu()"></button></div>
      </div>
      <div class="ff">
        <label id="sigLbl"></label>
        <div class="cvwrap"><canvas id="fsCv" height="130"></canvas><button class="cv-clr" id="fsClrBtn" onclick="clrFs()"></button></div>
        <button class="use-saved-btn" id="useSavedBtn" onclick="useSavedSig()"></button>
      </div>
    </div>
  </div>
</div>

<!-- RESULT -->
<div id="resultArea" style="display:none"></div>

<!-- MODAL: SIGNATURE SETUP -->
<div id="sigModal" class="modal-bg" style="display:flex">
  <div class="modal-box">
    <div id="sigInstr">&#9997;&#65039; <strong>&#201;tape 1</strong> — Configurez votre signature ci-dessous, puis cliquez <strong>Signer</strong> pour passer aux zones &#224; signer dans le document.</div>
    <h3 id="mSetupH3" style="margin-bottom:8px"></h3>
    <div class="mtabs">
      <button class="mtab on" id="tabType" onclick="switchTab('type')"></button>
      <button class="mtab" id="tabDraw" onclick="switchTab('draw')"></button>
      <button class="mtab" id="tabImg" onclick="switchTab('img')"></button>
    </div>
    <div class="tp on" id="tpType">
      <input class="name-inp" id="typeName" oninput="drawTyped()"/>
      <div class="font-row" id="fontRow"></div>
      <div class="color-row" id="tColRow"></div>
      <div id="typePrev"><canvas id="typePrevCv" height="160"></canvas></div>
    </div>
    <div class="tp" id="tpDraw">
      <div class="color-row" id="dColRow"></div>
      <div class="draw-wrap"><canvas id="drawCv" height="180"></canvas></div>
      <p class="cv-hint" id="cvHint"></p>
      <div style="display:flex;justify-content:flex-end;gap:6px;margin-bottom:4px">
        <button class="btn-sec" id="undoBtn" onclick="undoDraw()" style="font-size:12px;padding:4px 10px">↩ Undo</button>
      </div>
    </div>
    <div class="tp" id="tpImg">
      <div class="upload-zone" onclick="document.getElementById('imgFile').click()">
        <p id="uploadTxt" style="font-size:14px;color:#666"></p>
        <p style="font-size:11px;color:#aaa;margin-top:4px">PNG, JPG, JPEG — max 5 MB</p>
      </div>
      <input type="file" id="imgFile" accept=".png,.jpg,.jpeg" style="display:none" onchange="onImgFile()"/>
      <img id="imgPrev" alt=""/>
    </div>
    <div style="margin:10px 0 4px 0;font-size:13px;color:#444">
      J'ai lu et j'accepte les <a href="https://clawshow.ai/cgu" target="_blank" style="color:#1976d2;text-decoration:underline">CGU</a> et la <a href="https://clawshow.ai/confidentialite" target="_blank" style="color:#1976d2;text-decoration:underline">Politique de confidentialité</a>
    </div>
    <div class="btn-row">
      <button class="btn-sec" id="clrBtn" onclick="clrSetup()"></button>
      <button class="btn-pri" id="btnConfirm" onclick="confirmSetup()" disabled></button>
    </div>
  </div>
</div>

<!-- MODAL: OTP VERIFICATION -->
<div id="otpModal" class="modal-bg" style="display:none" onclick="bgClick(event,'otpModal')">
  <div class="modal-box" onclick="event.stopPropagation()">
    <h3 style="margin:0 0 12px">&#x1F512; V\u00e9rification d&apos;identit\u00e9</h3>
    <p style="font-size:14px;color:#555;margin:0 0 16px">Un code \u00e0 6 chiffres a \u00e9t\u00e9 envoy\u00e9 \u00e0 votre adresse email.</p>
    <input type="text" id="otpInput" maxlength="6" placeholder="000000"
      style="width:100%;box-sizing:border-box;font-size:28px;text-align:center;letter-spacing:10px;
             padding:12px;border:2px solid #ccc;border-radius:8px;font-family:monospace;margin-bottom:12px"
      oninput="this.value=this.value.replace(/\D/g,'')"/>
    <p id="otpError" style="color:#c00;font-size:13px;min-height:18px;margin:0 0 12px"></p>
    <div class="btn-row">
      <button class="btn-sec" onclick="closeModal('otpModal')" id="otpCancel">Annuler</button>
      <button class="btn-sec" onclick="resendOTP()" id="otpResend" style="color:#2a5298">Renvoyer</button>
      <button class="btn-pri" onclick="verifyOTP()" id="otpVerifyBtn">V\u00e9rifier</button>
    </div>
  </div>
</div>

<!-- MODAL: FINISH CONFIRM -->
<div id="finishModal" class="modal-bg" style="display:none" onclick="bgClick(event,'finishModal')">
  <div class="modal-box" onclick="event.stopPropagation()">
    <h3 id="finH3"></h3>
    <div class="cbrow" style="margin-bottom:20px">
      <input type="checkbox" id="finCb" onchange="document.getElementById('btnDoFin').disabled=!this.checked"/>
      <label for="finCb" id="finLbl" style="font-size:14px"></label>
    </div>
    <div class="btn-row">
      <button class="btn-sec" id="finCancel" onclick="closeModal('finishModal')"></button>
      <button class="btn-pri" id="btnDoFin" onclick="doFinish()" disabled></button>
    </div>
  </div>
</div>

<!-- MODAL: DECLINE -->
<div id="declineModal" class="modal-bg" style="display:none" onclick="bgClick(event,'declineModal')">
  <div class="modal-box" onclick="event.stopPropagation()">
    <h3 id="decH3"></h3>
    <textarea id="decReason" rows="4" style="width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;font-size:14px;resize:vertical;margin:10px 0"></textarea>
    <div class="btn-row">
      <button class="btn-sec" id="decCancel" onclick="closeModal('declineModal')"></button>
      <button class="btn-danger" id="decBtn" onclick="doDecline()"></button>
    </div>
  </div>
</div>

<!-- MODAL: CHANGE NAME -->
<div id="nameModal" class="modal-bg" style="display:none" onclick="bgClick(event,'nameModal')">
  <div class="modal-box" onclick="event.stopPropagation()">
    <h3 id="nameH3"></h3>
    <input class="name-inp" id="nameInp" style="margin-bottom:14px"/>
    <div class="btn-row">
      <button class="btn-sec" id="nameCancel" onclick="closeModal('nameModal')"></button>
      <button class="btn-pri" onclick="applyName()">OK</button>
    </div>
  </div>
</div>

<script>
const C = JSON.parse(document.getElementById('cfg').textContent);
const L = C.labels;
const FONTS = [
  {id:'dancing',label:'Dancing Script',css:"'Dancing Script', cursive"},
  {id:'vibes',  label:'Great Vibes',   css:"'Great Vibes', cursive"},
  {id:'caveat', label:'Caveat',        css:"'Caveat', cursive"},
  {id:'pacifico',label:'Pacifico',     css:"'Pacifico', cursive"}
];
const COLORS = ['#000000','#003366','#0066CC','#006633'];
const S = {
  cur:1, total:C.total_pages,
  paraphes:{},
  ff:{cb1:false,cb2:false,city:false,lu:false,fs:false},
  savedSig:null,
  adjustedParaphePos:null,
  sigTarget:null, sigPage:null,
  font:FONTS[0].css, color:'#000000',
  drawHas:false, imgData:null,
  luHas:false, fsHas:false,
  dCtx:null, luCtx:null, fsCtx:null
};

/* ---- COORD CONVERSION + DRAG ---- */
const PDF_W=595,PDF_H=842,PDF_SC=2.0;
function _pdfToImg(px,py,pw,ph,imgEl){
  const s=imgEl.offsetWidth/(PDF_W*PDF_SC);
  return{l:px*PDF_SC*s,t:(PDF_H-py-ph)*PDF_SC*s,w:pw*PDF_SC*s,h:ph*PDF_SC*s};}
function _imgToPdf(il,it,iw,ih,imgEl){
  const s=imgEl.offsetWidth/(PDF_W*PDF_SC);
  return{x:Math.round(il/(PDF_SC*s)),y:Math.round(PDF_H-it/(PDF_SC*s)-ih/(PDF_SC*s)),
         w:Math.round(iw/(PDF_SC*s)),h:Math.round(ih/(PDF_SC*s))};}
let _zDrag=null;
document.addEventListener('mousemove',e=>{
  if(!_zDrag)return;
  const r=_zDrag.el.parentElement.getBoundingClientRect();
  _zDrag.el.style.left=Math.max(0,Math.min(e.clientX-r.left-_zDrag.ox,r.width-_zDrag.el.offsetWidth))+'px';
  _zDrag.el.style.top=Math.max(0,Math.min(e.clientY-r.top-_zDrag.oy,r.height-_zDrag.el.offsetHeight))+'px';});
document.addEventListener('touchmove',e=>{
  if(!_zDrag)return;e.preventDefault();
  const t=e.touches[0],r=_zDrag.el.parentElement.getBoundingClientRect();
  _zDrag.el.style.left=Math.max(0,Math.min(t.clientX-r.left-_zDrag.ox,r.width-_zDrag.el.offsetWidth))+'px';
  _zDrag.el.style.top=Math.max(0,Math.min(t.clientY-r.top-_zDrag.oy,r.height-_zDrag.el.offsetHeight))+'px';},{passive:false});
document.addEventListener('mouseup',()=>{
  if(!_zDrag)return;
  const z=_zDrag.el,img=document.getElementById('pageImg');
  S.adjustedParaphePos=_imgToPdf(parseFloat(z.style.left)||0,parseFloat(z.style.top)||0,z.offsetWidth,z.offsetHeight,img);
  _zDrag=null;});
document.addEventListener('touchend',()=>{
  if(!_zDrag)return;
  const z=_zDrag.el,img=document.getElementById('pageImg');
  S.adjustedParaphePos=_imgToPdf(parseFloat(z.style.left)||0,parseFloat(z.style.top)||0,z.offsetWidth,z.offsetHeight,img);
  _zDrag=null;});
/* ---- PROGRESS ---- */
function reqLeft(){
  let d=Object.keys(S.paraphes).length;
  if(S.ff.cb2)d++;if(S.ff.city)d++;if(S.ff.lu)d++;if(S.ff.fs)d++;
  return (S.total+4)-d;
}
function updateBar(){
  const total=S.total+4,done=total-reqLeft();
  document.getElementById('progFill').style.width=Math.round(done/total*100)+'%';
  document.getElementById('reqLeftLabel').textContent=(L.req_left||'Required Fields Left')+': '+reqLeft();
  const btn=document.getElementById('btnNextField');
  if(reqLeft()===0){
    btn.textContent='\\u2705 '+(L.finish_btn||'Finish');
    btn.classList.add('is-finish');
    btn.onclick=openFinish;
  } else {
    btn.textContent=(L.next_field||'Next Required Field')+' \\u2192';
    btn.classList.remove('is-finish');
    btn.onclick=goNext;
  }
}
function flashField(el){
  if(!el)return;
  el.scrollIntoView({behavior:'smooth',block:'center'});
  el.classList.remove('field-flash');
  void el.offsetWidth;
  el.classList.add('field-flash');
  setTimeout(()=>el.classList.remove('field-flash'),1600);
}
function flashZone(){
  requestAnimationFrame(()=>{
    const z=document.querySelector('.sz.pend');
    if(!z)return;
    z.scrollIntoView({behavior:'smooth',block:'center'});
    z.classList.remove('pulse');
    void z.offsetWidth;
    z.classList.add('pulse');
    setTimeout(()=>z.classList.remove('pulse'),2200);
  });
}
function goNext(){
  if(!S.savedSig){
    // No signature configured yet — flash the confirm button in the panel
    const btn=document.getElementById('btnConfirm');
    btn.classList.remove('flash');void btn.offsetWidth;btn.classList.add('flash');
    setTimeout(()=>btn.classList.remove('flash'),1100);
    document.getElementById('sigModal').scrollIntoView&&
      document.getElementById('sigModal').scrollIntoView({behavior:'smooth',block:'start'});
    return;
  }
  for(let p=1;p<=S.total;p++){if(!S.paraphes[p]){gotoPage(p);flashZone();return;}}
  if(S.cur!==S.total){gotoPage(S.total);return;}
  const fs=document.getElementById('finalSec');
  if(!S.ff.cb2){const el=document.getElementById('cb2');flashField(el.closest('.cbrow')||el);el.focus();return;}
  if(!S.ff.city){const el=document.getElementById('cityInp');flashField(el.closest('.ff')||el);el.focus();return;}
  if(!S.ff.lu){flashField(document.getElementById('luCv').closest('.ff')||document.getElementById('luCv'));return;}
  if(!S.ff.fs){flashField(document.getElementById('fsCv').closest('.ff')||document.getElementById('fsCv'));}
}

/* ---- PAGE RENDERING ---- */
function gotoPage(n){n=Math.max(1,Math.min(n,S.total));S.cur=n;renderView();}
function renderView(){
  const n=S.cur;
  document.getElementById('pageImg').src='/esign/'+C.doc_id+'/page/'+n+'.png'+(C.token?'?token='+C.token:'');
  document.getElementById('pageInd').textContent='Page '+n+' / '+S.total;
  document.getElementById('btnPrev').disabled=n<=1;
  document.getElementById('btnNext2').disabled=n>=S.total;
  renderZones();
  const onLast=n===S.total;
  document.getElementById('finalSec').style.display=onLast?'block':'none';
  if(onLast)setTimeout(()=>{szCv(document.getElementById('luCv'));szCv(document.getElementById('fsCv'));},60);
}
function renderZones(){
  const c=document.getElementById('sigZones');c.innerHTML='';
  const n=S.cur,done=!!S.paraphes[n];
  const z=document.createElement('div');
  z.className='sz '+(done?'done':'pend');
  z.style.position='absolute';
  const img=document.getElementById('pageImg');
  const basePos=(C.zone_positions&&C.zone_positions.paraphe)||{x:397,y:40,w:130,h:45};
  function applyPos(){
    const p=S.adjustedParaphePos||basePos;
    const d=_pdfToImg(p.x,p.y,p.w,p.h,img);
    z.style.left=d.l+'px';z.style.top=d.t+'px';z.style.width=d.w+'px';z.style.height=d.h+'px';}
  if(done){
    const si=document.createElement('img');si.src=S.paraphes[n];z.appendChild(si);
  } else {
    z.innerHTML='<span class="zi">\\u270d</span><span class="zh">'+(L.zone_sign||'Signer ici')+'</span>';
    z.style.cursor='move';
    let _ds=null;
    z.addEventListener('mousedown',e=>{
      e.preventDefault();const r=z.getBoundingClientRect();
      _ds={x:e.clientX,y:e.clientY};
      _zDrag={el:z,ox:e.clientX-r.left,oy:e.clientY-r.top};});
    z.addEventListener('touchstart',e=>{
      const t=e.touches[0],r=z.getBoundingClientRect();
      _ds={x:t.clientX,y:t.clientY};
      _zDrag={el:z,ox:t.clientX-r.left,oy:t.clientY-r.top};},{passive:false});
    z.addEventListener('click',e=>{
      if(_ds&&Math.abs(e.clientX-_ds.x)<5&&Math.abs(e.clientY-_ds.y)<5)zoneClick(n);});
  }
  c.appendChild(z);
  if(img.complete&&img.naturalWidth>0)applyPos();
  else img.addEventListener('load',applyPos,{once:true});
}

/* ---- ZONE CLICK ---- */
function zoneClick(page){
  if(S.savedSig){
    S.paraphes[page]=S.savedSig;
    updateBar();renderZones();
    setTimeout(()=>{if(page<S.total)gotoPage(page+1);},300);
  } else {
    S.sigTarget='paraphe';S.sigPage=page;openSetup();
  }
}

/* ---- SETUP PANEL ---- */
function openSetup(){
  if(C.signer_name&&!document.getElementById('typeName').value)
    document.getElementById('typeName').value=C.signer_name;
  drawTyped();
  document.getElementById('sigModal').style.display='flex';
  requestAnimationFrame(()=>{
    const cv=document.getElementById('drawCv');
    const oldStrokes=cv._strokes||[];
    szCv(cv);S.dCtx=cv.getContext('2d');cv._strokes=oldStrokes;
    // Push appWrap down so document is visible below the panel
    const h=document.getElementById('sigModal').offsetHeight;
    document.getElementById('appWrap').style.paddingTop=(h+4)+'px';
  });
}
function closeSetup(){
  document.getElementById('sigModal').style.display='none';
  document.getElementById('appWrap').style.paddingTop='58px';
}
function switchTab(t){
  ['type','draw','img'].forEach(x=>{
    const T='tab'+x[0].toUpperCase()+x.slice(1);
    const P='tp'+x[0].toUpperCase()+x.slice(1);
    document.getElementById(T).classList.toggle('on',x===t);
    document.getElementById(P).classList.toggle('on',x===t);
  });
  chkBtn();
}
function chkBtn(){
  const act=document.querySelector('.mtab.on');
  const tid=act?act.id:'tabType';
  let has=(tid==='tabType')?document.getElementById('typeName').value.trim().length>0
         :(tid==='tabDraw')?S.drawHas
         :!!S.imgData;
  document.getElementById('btnConfirm').disabled=!has;
}
function clrSetup(){
  document.getElementById('typeName').value='';
  if(S.dCtx){const c=document.getElementById('drawCv');S.dCtx.clearRect(0,0,c.width,c.height);c._strokes=[];}
  S.drawHas=false;S.imgData=null;
  document.getElementById('imgPrev').style.display='none';
  drawTyped();chkBtn();
}
function confirmSetup(){
  const act=document.querySelector('.mtab.on');
  const tid=act?act.id:'tabType';
  let dataURL;
  if(tid==='tabType')dataURL=document.getElementById('typePrevCv').toDataURL('image/png');
  else if(tid==='tabDraw')dataURL=document.getElementById('drawCv').toDataURL('image/png');
  else dataURL=S.imgData;
  S.savedSig=dataURL;
  closeSetup();
  if(S.sigTarget==='paraphe'){
    S.paraphes[S.sigPage]=dataURL;
    updateBar();renderZones();
    setTimeout(()=>{if(S.sigPage<S.total)gotoPage(S.sigPage+1);else goNext();},300);
  } else {
    // Initial setup — scroll to first signature field
    setTimeout(goNext,200);
  }
}

/* ---- TYPED SIG ---- */
function buildFonts(){
  const r=document.getElementById('fontRow');
  FONTS.forEach((f,i)=>{
    const b=document.createElement('button');
    b.className='font-btn'+(i===0?' on':'');
    b.style.fontFamily=f.css;b.textContent=f.label;
    b.onclick=()=>{S.font=f.css;r.querySelectorAll('.font-btn').forEach(x=>x.classList.remove('on'));b.classList.add('on');drawTyped();};
    r.appendChild(b);
  });
}
function buildColors(cid,onChange){
  const r=document.getElementById(cid);
  COLORS.forEach((col,i)=>{
    const b=document.createElement('button');
    b.className='col-btn'+(i===0?' on':'');
    b.style.background=col;b.title=col;
    b.onclick=()=>{r.querySelectorAll('.col-btn').forEach(x=>x.classList.remove('on'));b.classList.add('on');onChange(col);};
    r.appendChild(b);
  });
}
function drawTyped(){
  const name=document.getElementById('typeName').value.trim();
  const cv=document.getElementById('typePrevCv');
  const box=document.getElementById('typePrev');
  const w=box.offsetWidth||340;cv.width=w;cv.height=160;
  const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,60);
  if(name){let fs=100;ctx.font=fs+'px '+S.font;while(ctx.measureText(name).width>w*0.88&&fs>28){fs-=4;ctx.font=fs+'px '+S.font;}const yOff=Math.round(cv.height/2+fs*0.15);ctx.fillStyle=S.color;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(name,w/2,yOff);}
  chkBtn();
}

/* ---- DRAW CANVAS ---- */
function initDraw(){
  const cv=document.getElementById('drawCv');szCv(cv);
  S.dCtx=cv.getContext('2d');S.drawHas=false;
  attachDraw(cv,S.dCtx,()=>{S.drawHas=true;chkBtn();},()=>S.color);
}

/* ---- IMG UPLOAD ---- */
function onImgFile(){
  const f=document.getElementById('imgFile').files[0];
  if(!f)return;
  if(f.size>5*1024*1024){alert('File too large (max 5MB)');return;}
  const rd=new FileReader();
  rd.onload=e=>{S.imgData=e.target.result;const img=document.getElementById('imgPrev');img.src=e.target.result;img.style.display='block';chkBtn();};
  rd.readAsDataURL(f);
}

/* ---- LU CANVAS ---- */
function initLu(){
  const cv=document.getElementById('luCv');szCv(cv);
  S.luCtx=cv.getContext('2d');S.luHas=false;
  attachDraw(cv,S.luCtx,()=>{S.luHas=true;S.ff.lu=true;updateBar();},()=>S.color);
}
function clrLu(){const cv=document.getElementById('luCv');if(S.luCtx)S.luCtx.clearRect(0,0,cv.width,cv.height);S.luHas=false;S.ff.lu=false;updateBar();}

/* ---- FINAL SIG CANVAS ---- */
function initFs(){
  const cv=document.getElementById('fsCv');szCv(cv);
  S.fsCtx=cv.getContext('2d');S.fsHas=false;
  attachDraw(cv,S.fsCtx,()=>{S.fsHas=true;S.ff.fs=true;updateBar();},()=>S.color);
}
function clrFs(){const cv=document.getElementById('fsCv');if(S.fsCtx)S.fsCtx.clearRect(0,0,cv.width,cv.height);S.fsHas=false;S.ff.fs=false;updateBar();}
function useSavedSig(){
  if(!S.savedSig){alert(L.warn_no_saved||'Please sign a page first.');return;}
  const cv=document.getElementById('fsCv');szCv(cv);
  const ctx=cv.getContext('2d');const img=new Image();
  img.onload=()=>{ctx.clearRect(0,0,cv.width,cv.height);ctx.drawImage(img,0,0,cv.width,cv.height);};
  img.src=S.savedSig;
  S.fsHas=true;S.ff.fs=true;updateBar();
}

/* ---- FINAL FIELDS ---- */
function onFF(){
  S.ff.cb1=document.getElementById('cb1').checked;
  S.ff.cb2=document.getElementById('cb2').checked;
  S.ff.city=document.getElementById('cityInp').value.trim().length>0;
  updateBar();
}

/* ---- DRAW UTILITY ---- */
function szCv(cv){
  // Always measure parent to avoid circular dependency (canvas CSS width = its pixel width when no CSS width set)
  const par=cv.parentElement;
  const pr=par?par.getBoundingClientRect():null;
  const r=cv.getBoundingClientRect();
  const w=(pr&&pr.width>0?pr.width:0)||(r.width>0?r.width:0)||(par?par.offsetWidth:0)||320;
  const h=parseInt(cv.getAttribute('height')||cv.height||90);
  cv.width=Math.round(w);cv.height=h;
}
function attachDraw(cv,ctx,onDraw,getColor){
  let drawing=false,lx=0,ly=0,mx=0,my=0;
  cv._strokes=[];
  function getCtx(){return cv.getContext('2d');}
  function pos(e){
    const r=cv.getBoundingClientRect(),s=e.touches?e.touches[0]:e;
    // Guard: if canvas pixel width doesn't match display, use 1:1 scale
    const scaleX=r.width>0?cv.width/r.width:1,scaleY=r.height>0?cv.height/r.height:1;
    return[(s.clientX-r.left)*scaleX,(s.clientY-r.top)*scaleY];
  }
  function dn(e){
    e.preventDefault();
    // Auto-correct canvas pixel width on every stroke start
    const r=cv.getBoundingClientRect();
    if(r.width>0&&Math.abs(cv.width-r.width)>1){cv.width=Math.round(r.width);}
    [lx,ly]=pos(e);[mx,my]=[lx,ly];
    drawing=true;
    cv._strokes.push({color:getColor(),pts:[[lx,ly]]});
  }
  function mv(e){
    if(!drawing)return;e.preventDefault();
    const[x,y]=pos(e);
    const c=getCtx();
    const nx=(lx+x)/2,ny=(ly+y)/2;
    c.beginPath();c.moveTo(mx,my);
    c.quadraticCurveTo(lx,ly,nx,ny);
    c.strokeStyle=getColor();c.lineWidth=5;c.lineCap='round';c.lineJoin='round';
    c.stroke();
    mx=nx;my=ny;[lx,ly]=[x,y];
    if(cv._strokes.length)cv._strokes[cv._strokes.length-1].pts.push([x,y]);
    if(onDraw)onDraw();
  }
  function up(){drawing=false;}
  cv.addEventListener('mousedown',dn);
  document.addEventListener('mousemove',mv);
  document.addEventListener('mouseup',up);
  cv.addEventListener('touchstart',dn,{passive:false});cv.addEventListener('touchmove',mv,{passive:false});
  cv.addEventListener('touchend',up);
}
function undoDraw(){
  const cv=document.getElementById('drawCv');
  if(!cv._strokes||!cv._strokes.length)return;
  cv._strokes.pop();
  const ctx=cv.getContext('2d');ctx.clearRect(0,0,cv.width,cv.height);
  cv._strokes.forEach(stroke=>{
    if(stroke.pts.length<2)return;
    ctx.strokeStyle=stroke.color;ctx.lineWidth=5;ctx.lineCap='round';ctx.lineJoin='round';
    let[lx,ly]=stroke.pts[0],[pmx,pmy]=[lx,ly];
    for(let i=1;i<stroke.pts.length;i++){
      const[x,y]=stroke.pts[i],nx=(lx+x)/2,ny=(ly+y)/2;
      ctx.beginPath();ctx.moveTo(pmx,pmy);ctx.quadraticCurveTo(lx,ly,nx,ny);ctx.stroke();
      pmx=nx;pmy=ny;[lx,ly]=[x,y];
    }
  });
  S.drawHas=cv._strokes.length>0;chkBtn();
}

/* ---- MORE ACTIONS ---- */
function toggleMore(e){e.stopPropagation();const m=document.getElementById('moreMenu');m.style.display=m.style.display==='none'?'block':'none';}
document.addEventListener('click',e=>{if(!e.target.closest('#topActions'))document.getElementById('moreMenu').style.display='none';});
function openDeclineModal(){document.getElementById('moreMenu').style.display='none';document.getElementById('declineModal').style.display='flex';}
function openNameModal(){document.getElementById('moreMenu').style.display='none';document.getElementById('nameInp').value=C.signer_name||'';document.getElementById('nameModal').style.display='flex';}
function openChangeSign(){document.getElementById('moreMenu').style.display='none';S.sigTarget=null;S.sigPage=null;openSetup();}
function applyName(){C.signer_name=document.getElementById('nameInp').value.trim()||C.signer_name;closeModal('nameModal');drawTyped();}

/* ---- DECLINE ---- */
function doDecline(){
  const reason=document.getElementById('decReason').value.trim();
  closeModal('declineModal');
  document.getElementById('appWrap').style.display='none';
  document.getElementById('topBar').style.display='none';
  document.getElementById('resultArea').style.display='block';
  document.getElementById('resultArea').innerHTML='<div class="res-dec"><h2>\\u2717</h2><p>'+(L.declined_msg||'Vous avez refus\\u00e9 de signer ce document.')+'</p></div>';
  fetch('/esign/'+C.doc_id+'/decline'+(C.token?'?token='+C.token:''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason,token:C.token})}).catch(()=>{});
}

/* ---- OTP ---- */
let _otpVerified=false;
function openFinish(){
  if(_otpVerified){_showFinishModal();return;}
  sendOTP();
}
function sendOTP(){
  document.getElementById('otpError').textContent='';
  document.getElementById('otpInput').value='';
  fetch('/esign/'+C.doc_id+'/otp/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:C.token})})
    .then(r=>r.json()).then(d=>{
      if(d.status==='sent'){document.getElementById('otpModal').style.display='flex';document.getElementById('otpInput').focus();}
      else{alert('Erreur envoi code: '+(d.error||'?'));}
    }).catch(()=>alert('Erreur r\u00e9seau'));
}
function resendOTP(){
  document.getElementById('otpError').textContent='';
  document.getElementById('otpInput').value='';
  document.getElementById('otpResend').disabled=true;
  fetch('/esign/'+C.doc_id+'/otp/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:C.token})})
    .then(()=>{setTimeout(()=>{document.getElementById('otpResend').disabled=false;},30000);})
    .catch(()=>{document.getElementById('otpResend').disabled=false;});
}
function verifyOTP(){
  const code=document.getElementById('otpInput').value.trim();
  if(code.length!==6){document.getElementById('otpError').textContent='Entrez un code \u00e0 6 chiffres';return;}
  document.getElementById('otpVerifyBtn').disabled=true;
  fetch('/esign/'+C.doc_id+'/otp/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:C.token,otp:code})})
    .then(r=>r.json()).then(d=>{
      document.getElementById('otpVerifyBtn').disabled=false;
      if(d.verified){_otpVerified=true;closeModal('otpModal');_showFinishModal();}
      else{document.getElementById('otpError').textContent=d.error||'Code invalide';}
    }).catch(()=>{document.getElementById('otpVerifyBtn').disabled=false;document.getElementById('otpError').textContent='Erreur r\u00e9seau';});
}
function _showFinishModal(){document.getElementById('finCb').checked=false;document.getElementById('btnDoFin').disabled=true;document.getElementById('finishModal').style.display='flex';}
/* ---- FINISH ---- */
function doFinish(){
  closeModal('finishModal');
  const paraphes={};Object.entries(S.paraphes).forEach(([k,v])=>{paraphes[k]=v;});
  const luPng=S.luHas?document.getElementById('luCv').toDataURL('image/png'):'';
  const fsPng=S.fsHas?document.getElementById('fsCv').toDataURL('image/png'):(S.savedSig||'');
  const city=document.getElementById('cityInp').value.trim();
  document.getElementById('btnNextField').disabled=true;
  fetch('/esign/'+C.doc_id+'/sign'+(C.token?'?token='+C.token:''),{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:C.token,paraphes,signature_png:fsPng,lu_approuve_png:luPng,city,accept_conditions:document.getElementById("cb1").checked,accept_email:true,paraphe_pos:S.adjustedParaphePos})
  }).then(r=>r.json()).then(d=>{
    document.getElementById('appWrap').style.display='none';
    document.getElementById('topBar').style.display='none';
    document.getElementById('resultArea').style.display='block';
    if(d.success){
      document.getElementById('resultArea').innerHTML='<div class="res-ok"><h2>'+(L.success_title||'\\u2705 Signature enregistr\\u00e9e')+'</h2><p>'+(L.success_msg||'Votre signature a \\u00e9t\\u00e9 enregistr\\u00e9e avec succ\\u00e8s.')+'</p><p style="margin-top:8px">'+(L.success_pending_school||'Le document est en attente de la signature de l\\u0027administration scolaire.')+'</p><p style="margin-top:8px">'+(L.success_email_note||'Vous recevrez un email de confirmation une fois le document finalis\\u00e9.')+'</p></div>';
    } else {
      document.getElementById('resultArea').innerHTML='<div class="res-dec"><p>Error: '+(d.error||'Unknown')+'</p></div>';
    }
  }).catch(err=>{document.getElementById('btnNextField').disabled=false;alert('Error: '+err.message);});
}

/* ---- MODAL HELPERS ---- */
function closeModal(id){document.getElementById(id).style.display='none';}
function bgClick(e,id){if(e.target===e.currentTarget)closeModal(id);}

/* ---- SET ALL TEXT (from L) ---- */
function setTexts(){
  document.getElementById('btnMore').textContent=(L.more_actions||'Plus d\\u0027actions')+' \u25be';
  document.getElementById('maCN').textContent=L.change_name||'Modifier le nom';
  document.getElementById('maCN').onclick=openNameModal;
  document.getElementById('maCS').textContent=L.change_sign||'Changer de signature';
  document.getElementById('maCS').onclick=openChangeSign;
  document.getElementById('maDecline').textContent=L.decline_btn||'Refuser de signer';
  document.getElementById('maDecline').onclick=openDeclineModal;
  document.getElementById('maDL').textContent=L.download_pdf||'T\u00e9l\u00e9charger le PDF';
  document.getElementById('maDL').href='/esign/'+C.doc_id+'/preview.pdf';
  document.getElementById('maPrint').textContent=L.print_pdf||'Imprimer';
  document.getElementById('btnPrev').textContent='\u2190 '+(L.prev||'Pr\u00e9c\u00e9dent');
  document.getElementById('btnNext2').textContent=(L.next||'Suivant')+' \u2192';
  document.getElementById('finalH3').textContent=L.final_title||'Informations de signature';
  document.getElementById('lb1').innerHTML=L.cb1||'';
  document.getElementById('lb2').textContent=L.cb2||'';
  document.getElementById('cityLbl').textContent=L.city_label||'Ville';
  document.getElementById('luLbl').textContent=L.lu_label||'Lu et approuv\u00e9';
  document.getElementById('sigLbl').textContent=L.sig_label||'Signature';
  document.getElementById('luClrBtn').textContent=L.clear||'Effacer';
  document.getElementById('fsClrBtn').textContent=L.clear||'Effacer';
  document.getElementById('useSavedBtn').textContent='\u21a9 '+(L.use_saved_sig||'Utiliser ma signature');
  document.getElementById('mSetupH3').textContent=L.setup_title||'\u270d Configurez votre signature';
  document.getElementById('tabType').textContent=L.tab_type||'Type';
  document.getElementById('tabDraw').textContent=L.tab_draw||'Dessin';
  document.getElementById('tabImg').textContent=L.tab_image||'Image';
  document.getElementById('typeName').placeholder=L.name_placeholder||'Votre nom';
  document.getElementById('cvHint').textContent=L.canvas_hint||'Signez avec votre doigt ou souris';
  document.getElementById('uploadTxt').textContent=L.click_upload||'Cliquer pour choisir un fichier';
  document.getElementById('clrBtn').textContent=L.clear||'Effacer';
  document.getElementById('btnConfirm').textContent='\u270d '+(L.sign_btn||'Signer');
  document.getElementById('finH3').textContent=L.finish_confirm_title||'Confirmer la signature';
  document.getElementById('finLbl').textContent=L.finish_confirm_text||'Je confirme que toutes les signatures sont les miennes et j\\u0027accepte les termes du contrat.';
  document.getElementById('finCancel').textContent=L.cancel||'Annuler';
  document.getElementById('btnDoFin').textContent='\u2705 '+(L.confirm||'Confirmer');
  document.getElementById('decH3').textContent=L.decline_title||'Refuser de signer';
  document.getElementById('decReason').placeholder=L.decline_reason_hint||'Motif (optionnel)';
  document.getElementById('decCancel').textContent=L.cancel||'Annuler';
  document.getElementById('decBtn').textContent='\u274c '+(L.decline_btn||'Refuser');
  document.getElementById('nameH3').textContent=L.change_name||'Modifier le nom';
  document.getElementById('nameCancel').textContent=L.cancel||'Annuler';
}

/* ---- INIT ---- */
function init(){
  setTexts();
  buildFonts();
  buildColors('tColRow',c=>{S.color=c;drawTyped();});
  buildColors('dColRow',c=>{S.color=c;});
  initDraw(); initLu(); initFs();
  document.getElementById('typeName').value=C.signer_name||'';
  drawTyped();
  gotoPage(1);
  updateBar();
  openSetup();
}
window.addEventListener('DOMContentLoaded',init);
window.addEventListener('resize',()=>{
  if(S.cur===S.total){szCv(document.getElementById('luCv'));szCv(document.getElementById('fsCv'));}
});
</script>
</body>
</html>"""


_LABELS = {
    "fr": {
        "zone_sign": "Signer ici",
        "zone_signed": "\u2713 Sign\u00e9",
        "tab_draw": "Dessin",
        "tab_type": "Type",
        "tab_image": "Image",
        "canvas_hint": "Signez avec votre doigt (mobile) ou votre souris",
        "name_placeholder": "Entrez votre nom",
        "final_title": "Informations de signature",
        "cb1": "J'ai lu et j'accepte les <a href=\"https://clawshow.ai/cgu\" target=\"_blank\" style=\"color:#1976d2\">CGU</a> et la <a href=\"https://clawshow.ai/confidentialite\" target=\"_blank\" style=\"color:#1976d2\">Politique de confidentialité</a>. ✅ Valeur juridique AES (eIDAS).",
        "cb2": "J'accepte les \u00e9changes par email en remplacement du courrier postal.",
        "city_label": "Fait \u00e0 (ville) :",
        "lu_label": "\u00c9crivez \u00ab lu et approuv\u00e9 \u00bb :",
        "sig_label": "Signature :",
        "decline_btn": "Refuser de signer",
        "submitting": "Envoi en cours...",
        "prev": "Pr\u00e9c\u00e9dent",
        "next": "Suivant",
        "success_msg": "Votre signature a \u00e9t\u00e9 enregistr\u00e9e avec succ\u00e8s.",
        "download": "T\u00e9l\u00e9charger le document sign\u00e9",
        "declined_msg": "Vous avez refus\u00e9 de signer ce document.",
        "req_left": "Champs restants",
        "finish_btn": "Terminer",
        "next_field": "Champ suivant requis",
        "success_title": "\u2705 Signature enregistr\u00e9e",
        "success_pending_school": "Le document est en attente de la signature de l\u0027administration scolaire.",
        "success_email_note": "Vous recevrez un email de confirmation une fois le document finalis\u00e9.",
        "finish_confirm_title": "Confirmer la signature",
        "finish_confirm_text": "Je confirme que toutes les signatures sont les miennes et j\u0027accepte les termes du contrat.",
        "cancel": "Annuler",
        "confirm": "Confirmer",
        "decline_title": "Refuser de signer",
        "decline_reason_hint": "Motif du refus (optionnel)",
        "setup_title": "\u270d Configurez votre signature",
        "click_upload": "Cliquer pour choisir un fichier",
        "legal_label": "J'ai lu et j'accepte les <a href=\"https://clawshow.ai/cgu\" target=\"_blank\" style=\"color:#1976d2\">CGU</a> et la <a href=\"https://clawshow.ai/confidentialite\" target=\"_blank\" style=\"color:#1976d2\">Politique de confidentialit\u00e9</a>. \u2705 Valeur juridique AES (eIDAS).",
        "sign_btn": "Signer",
        "more_actions": "Plus d\u0027actions",
        "change_name": "Modifier le nom",
        "change_sign": "Changer de signature",
        "download_pdf": "T\u00e9l\u00e9charger le PDF",
        "print_pdf": "Imprimer",
        "clear": "Effacer",
        "use_saved_sig": "Utiliser ma signature",
        "warn_no_saved": "Veuillez d'abord signer une page.",
    },
    "en": {
        "zone_sign": "Sign here",
        "zone_signed": "\u2713 Signed",
        "tab_draw": "Draw",
        "tab_type": "Type",
        "tab_image": "Image",
        "canvas_hint": "Sign with your finger (mobile) or mouse",
        "name_placeholder": "Enter your name",
        "final_title": "Signature information",
        "cb1": "I certify that I have read the enrollment terms and accept them.",
        "cb2": "I accept email communications in place of postal mail.",
        "city_label": "Signed at (city):",
        "lu_label": "Write \"read and approved\":",
        "sig_label": "Signature:",
        "decline_btn": "Decline to sign",
        "submitting": "Submitting...",
        "prev": "Previous",
        "next": "Next",
        "success_msg": "Your signature has been recorded successfully.",
        "download": "Download signed document",
        "declined_msg": "You have declined to sign this document.",
        "req_left": "Required Fields Left",
        "finish_btn": "Finish",
        "next_field": "Next Required Field",
        "success_title": "\u2705 Signature recorded",
        "success_pending_school": "The document is awaiting the school administration's signature.",
        "success_email_note": "You will receive a confirmation email once the document is finalized.",
        "finish_confirm_title": "Confirm your signature",
        "finish_confirm_text": "I confirm that all signatures are mine and I accept the terms of the contract.",
        "cancel": "Cancel",
        "confirm": "Confirm",
        "decline_title": "Decline to sign",
        "decline_reason_hint": "Reason for declining (optional)",
        "setup_title": "\u270d Configure your signature",
        "click_upload": "Click to choose a file",
        "legal_label": "I accept that this signature is my legal representation",
        "sign_btn": "Sign",
        "more_actions": "More Actions",
        "change_name": "Change Name",
        "change_sign": "Change Sign",
        "download_pdf": "Download PDF",
        "print_pdf": "Print PDF",
        "clear": "Clear",
        "use_saved_sig": "Use my saved signature",
        "warn_no_saved": "Please sign a page first.",
    },
}


def _render_signing_page(doc: dict, token: str = "") -> str:
    """Build the V3 multi-page Foxit-match signing page."""
    import json as _json
    lang = doc.get("language", "fr")
    labels = _LABELS.get(lang, _LABELS["en"])
    total_pages = doc.get("total_pages") or 1
    sig_pos_raw = doc.get("signature_positions")
    if sig_pos_raw:
        zone_positions = _json.loads(sig_pos_raw) if isinstance(sig_pos_raw, str) else sig_pos_raw
    else:
        from tools.esign import _DEFAULT_SIG_POSITIONS as _dp
        zone_positions = {k: v for k, v in _dp.items()}
    config = _json.dumps({
        "doc_id": doc["id"],
        "token": token,
        "total_pages": total_pages,
        "signer_name": doc.get("signer_name", ""),
        "lang": lang,
        "labels": labels,
        "zone_positions": zone_positions,
    }, ensure_ascii=False)
    page = _SIGNING_PAGE_TEMPLATE
    page = page.replace("__CONFIG_JSON__", config)
    page = page.replace("__LANG__", lang)
    return page



# ---------------------------------------------------------------------------
# eSign V2 — HTTP handlers (REST endpoints, not MCP tools)
# ---------------------------------------------------------------------------

from adapters.esign.otp_handler import esign_otp_send, esign_otp_verify
from adapters.esign.auth import (
    auth_signup, auth_login_otp_send, auth_login_verify,
    auth_logout, auth_me, auth_user_documents, get_session_user,
)
from adapters.esign.pages import (
    page_signup, page_login, page_login_verify,
    page_dashboard, page_new_esign,
)

# ─────────────────────────────────────────────────────────────────
# eSign Setup — drag-and-drop zone position editor
# ─────────────────────────────────────────────────────────────────
_ESIGN_SETUP_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClawShow eSign — Zones</title>
<style>
*{box-sizing:border-box}body{font-family:Arial,sans-serif;margin:0;background:#f0f2f5}
.hdr{background:#1976d2;color:#fff;padding:12px 20px;display:flex;align-items:center;justify-content:space-between}
.hdr h2{margin:0;font-size:15px}.container{max-width:760px;margin:20px auto;padding:0 16px}
.page-wrap{position:relative;margin-bottom:28px;display:block}
.page-img{display:block;width:100%;box-shadow:0 2px 10px rgba(0,0,0,.25)}
.zone{position:absolute;border:2px dashed #E6A817;background:rgba(230,168,23,.18);cursor:move;
  border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:10px;
  color:#7B5B00;font-weight:700;user-select:none;min-height:44px}
.zone.fz{border-color:#1976d2;background:rgba(25,118,210,.14);color:#0d47a1}
.zlbl{position:absolute;top:-17px;left:0;font-size:10px;white-space:nowrap;
  background:#444;color:#fff;padding:1px 6px;border-radius:2px;pointer-events:none}
.sec{font-size:13px;font-weight:700;color:#444;padding:6px 10px;background:#fff;
  border-left:3px solid #1976d2;border-radius:2px;margin:0 0 8px}
.bar{position:sticky;bottom:0;background:#fff;border-top:2px solid #ddd;
  padding:12px 20px;display:flex;gap:12px;align-items:center}
.btn{padding:9px 22px;border:none;border-radius:5px;cursor:pointer;font-size:14px;font-weight:700}
.bsave{background:#1976d2;color:#fff}.bsave:hover{background:#1565c0}
.msg{font-size:13px;color:#666}
</style></head><body>
<div class="hdr">
  <h2>ClawShow eSign &mdash; R&eacute;glage des zones</h2>
  <span style="font-size:12px;opacity:.85">__DOC_ID__</span>
</div>
<div class="container">
  <p style="color:#555;font-size:13px;margin:0 0 16px">
    Glissez les zones pour les aligner avec les emplacements de signature dans le document.
  </p>
  <div class="sec">Page repr&eacute;sentative &mdash; Paraphe (toutes les pages)</div>
  <div class="page-wrap" id="pRep">
    <img class="page-img" id="iRep" src="/esign/__DOC_ID__/page/1.png" alt="p1"/>
  </div>
  <div class="sec">Derni&egrave;re page &mdash; Signature finale</div>
  <div class="page-wrap" id="pLast">
    <img class="page-img" id="iLast" src="/esign/__DOC_ID__/page/__LAST__.png" alt="last"/>
  </div>
</div>
<div class="bar">
  <button class="btn bsave" onclick="save()">Enregistrer</button>
  <span class="msg" id="msg"></span>
</div>
<script>
const ID='__DOC_ID__',W=595,H=842,SC=2.0;
let Z=__ZONES__;
let dr=null;

function p2i(px,py,pw,ph,iw){const s=iw/(W*SC);
  return{l:px*SC*s,t:(H-py-ph)*SC*s,w:pw*SC*s,h:ph*SC*s};}
function i2p(il,it,iw,ih,imgW){const s=imgW/(W*SC);
  return{x:Math.round(il/(SC*s)),y:Math.round(H-it/(SC*s)-ih/(SC*s)),
         w:Math.round(iw/(SC*s)),h:Math.round(ih/(SC*s))};}

function mkZone(id,lbl,fin,wrap,img){
  const z=document.createElement('div');
  z.className='zone'+(fin?' fz':'');z.dataset.z=id;
  z.innerHTML='<span class="zlbl">'+lbl+'</span>'+lbl;
  z.addEventListener('mousedown',e=>{e.preventDefault();
    const r=z.getBoundingClientRect();
    dr={z,ox:e.clientX-r.left,oy:e.clientY-r.top,wrap,img};});
  z.addEventListener('touchstart',e=>{e.preventDefault();
    const t=e.touches[0],r=z.getBoundingClientRect();
    dr={z,ox:t.clientX-r.left,oy:t.clientY-r.top,wrap,img};},{passive:false});
  posZ(z,id,img);wrap.appendChild(z);}

function posZ(z,id,img){
  const p=Z[id];if(!p)return;
  const d=p2i(p.x,p.y,p.w,p.h,img.offsetWidth);
  z.style.left=d.l+'px';z.style.top=d.t+'px';z.style.width=d.w+'px';z.style.height=d.h+'px';}

function mv(cx,cy){if(!dr)return;
  const r=dr.wrap.getBoundingClientRect();
  dr.z.style.left=Math.max(0,cx-r.left-dr.ox)+'px';
  dr.z.style.top=Math.max(0,cy-r.top-dr.oy)+'px';}
document.addEventListener('mousemove',e=>mv(e.clientX,e.clientY));
document.addEventListener('touchmove',e=>{e.preventDefault();
  mv(e.touches[0].clientX,e.touches[0].clientY);},{passive:false});

function endDr(){if(!dr)return;
  const z=dr.z,id=z.dataset.z,img=dr.img;
  const p=i2p(parseFloat(z.style.left)||0,parseFloat(z.style.top)||0,
               parseFloat(z.style.width)||80,parseFloat(z.style.height)||20,img.offsetWidth);
  Z[id]={...Z[id],...p};dr=null;}
document.addEventListener('mouseup',endDr);
document.addEventListener('touchend',endDr);

function save(){
  const m=document.getElementById('msg');m.textContent='...';m.style.color='#666';
  fetch('/esign/'+ID+'/setup',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({positions:Z})})
  .then(r=>r.json()).then(d=>{
    if(d.success){m.textContent='\u2713 Enregistr\u00e9';m.style.color='#2e7d32';}
    else{m.textContent='Erreur: '+(d.error||'?');m.style.color='#c62828';}
  }).catch(()=>{m.textContent='Erreur r\u00e9seau';m.style.color='#c62828';});}

function init(){
  const rWrap=document.getElementById('pRep'),rImg=document.getElementById('iRep');
  const lWrap=document.getElementById('pLast'),lImg=document.getElementById('iLast');
  function s1(){mkZone('paraphe','Paraphe',false,rWrap,rImg);}
  function s2(){mkZone('final_lu','Lu et approuv\u00e9',true,lWrap,lImg);
                mkZone('final_sig','Signature finale',true,lWrap,lImg);}
  if(rImg.complete)s1();else rImg.onload=s1;
  if(lImg.complete)s2();else lImg.onload=s2;}
init();
window.addEventListener('resize',()=>{
  document.querySelectorAll('.zone').forEach(z=>{
    const img=z.parentElement.querySelector('img');posZ(z,z.dataset.z,img);});});
</script></body></html>"""


async def esign_setup_get(request: Request):
    """GET /esign/{document_id}/setup — visual zone position editor."""
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return HTMLResponse("<h2>Document not found</h2>", status_code=404)
    total_pages = doc.get("total_pages") or 1
    sig_raw = doc.get("signature_positions")
    if sig_raw:
        cur = json.loads(sig_raw) if isinstance(sig_raw, str) else sig_raw
    else:
        from tools.esign import _DEFAULT_SIG_POSITIONS as _dp
        cur = {k: v for k, v in _dp.items() if k != "school_sig"}
    html = (_ESIGN_SETUP_TEMPLATE
            .replace("__DOC_ID__", doc_id)
            .replace("__LAST__", str(total_pages))
            .replace("__ZONES__", json.dumps(cur)))
    return HTMLResponse(html)


async def esign_setup_post(request: Request):
    """POST /esign/{document_id}/setup — save zone positions."""
    doc_id = request.path_params["document_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    positions = body.get("positions")
    if not positions or not all(k in positions for k in ("paraphe", "final_lu", "final_sig")):
        return JSONResponse({"error": "positions must contain paraphe, final_lu, final_sig"}, status_code=400)
    db.update_esign_signature_positions(doc_id, positions)
    return JSONResponse({"success": True, "document_id": doc_id})


async def esign_page_image(request: Request):
    """GET /esign/{document_id}/page/{page_num}.png — serve a PDF page as PNG."""
    from starlette.responses import FileResponse
    doc_id = request.path_params["document_id"]
    page_num = request.path_params.get("page_num", "1")
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    namespace = doc["namespace"]
    img_path = ESIGN_DATA_DIR / namespace / f"{doc_id}_pages" / f"page_{page_num}.png"
    if not img_path.exists():
        pdf_path = doc.get("original_pdf_path", "")
        if pdf_path and Path(pdf_path).exists():
            try:
                from tools.esign import _generate_page_images
                pages_dir = str(ESIGN_DATA_DIR / namespace / f"{doc_id}_pages")
                total = _generate_page_images(pdf_path, pages_dir)
                if total != doc.get("total_pages", 1):
                    with db.get_conn() as conn:
                        conn.execute(
                            "UPDATE esign_documents SET total_pages=? WHERE id=?",
                            (total, doc_id),
                        )
            except Exception:
                pass
    if not img_path.exists():
        return JSONResponse({"error": f"Page {page_num} not found"}, status_code=404)
    return FileResponse(
        str(img_path),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )



async def esign_user_create(request: Request) -> JSONResponse:
    # POST /esign/user/create - SaaS portal. Requires session cookie. Deducts 1 quota.
    from adapters.esign.auth import get_session_user as _gsu
    user = _gsu(request)
    if not user:
        return JSONResponse({"error": "Non authentifie"}, status_code=401)

    remaining = user["free_quota_total"] - user["free_quota_used"]
    if remaining <= 0:
        return JSONResponse({
            "error": "quota_exceeded",
            "message": "Quota gratuit epuise. Contactez-nous pour upgrader.",
            "upgrade_url": "mailto:contact@clawshow.ai?subject=Upgrade%20eSign",
        }, status_code=402)

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "Formulaire invalide"}, status_code=400)

    signer_name = str(form.get("signer_name") or "").strip()
    signer_email = str(form.get("signer_email") or "").strip()
    message = str(form.get("message") or "").strip()
    pdf_upload = form.get("pdf")

    if not signer_name or not signer_email:
        return JSONResponse({"error": "Nom et e-mail du signataire requis"}, status_code=400)
    if not pdf_upload or not hasattr(pdf_upload, "read"):
        return JSONResponse({"error": "Document PDF requis"}, status_code=400)

    import asyncio as _aio
    import threading as _th
    import secrets as _sec
    from tools.esign import _next_doc_id, _generate_page_images, _send_signing_email
    from pathlib import Path as _Path

    namespace = "user_" + str(user["id"])
    doc_id = _next_doc_id(namespace)

    esign_dir = ESIGN_DATA_DIR / namespace
    esign_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = str(esign_dir / (doc_id + ".pdf"))

    pdf_bytes = await pdf_upload.read()
    with open(pdf_path, "wb") as fh:
        fh.write(pdf_bytes)

    from pypdf import PdfReader
    try:
        total_pages = len(PdfReader(pdf_path).pages)
    except Exception:
        total_pages = 1

    loop = _aio.get_event_loop()
    pages_dir = str(ESIGN_DATA_DIR / namespace / (doc_id + "_pages"))
    await loop.run_in_executor(None, _generate_page_images, pdf_path, pages_dir)

    signer_token = _sec.token_urlsafe(32)

    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO esign_documents "
            "(id, namespace, status, signer_name, signer_email, template, total_pages, original_pdf_path, creator_user_id) "
            "VALUES (?, ?, 'pending', ?, ?, 'upload', ?, ?, ?)",
            (doc_id, namespace, signer_name, signer_email, total_pages, pdf_path, user["id"]),
        )
        conn.execute(
            "INSERT INTO esign_signers (document_id, signer_name, signer_email, token, signing_order) "
            "VALUES (?, ?, ?, ?, 1)",
            (doc_id, signer_name, signer_email, signer_token),
        )
        conn.execute(
            "UPDATE esign_users SET free_quota_used = free_quota_used + 1 WHERE id = ?",
            (user["id"],),
        )

    esign_base = os.getenv("MCP_BASE_URL", "https://esign-api.clawshow.ai")
    signing_url = esign_base + "/esign/" + doc_id + "?token=" + signer_token

    def _send_invite():
        _send_signing_email(signer_name, signer_email, signing_url, doc_id, message or None)
    _th.Thread(target=_send_invite, daemon=True).start()

    import logging as _log
    _log.getLogger(__name__).info(
        "esign_user_create: user=%s*** doc=%s quota=%d/%d",
        user["email"][:4], doc_id,
        user["free_quota_used"] + 1, user["free_quota_total"],
    )

    return JSONResponse({
        "doc_id": doc_id,
        "signing_url": signing_url,
        "status": "pending",
        "quota_remaining": remaining - 1,
    })


async def esign_create(request: Request) -> JSONResponse:
    """POST /esign/create — create signing request.

    Supports two formats:
    1. ClawShow native:  { template, signer_name, signer_email, fields, namespace, ... }
    2. FocusingPro compat: { file_url, signers:[{role,name,email,order}], signature_fields, ... }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    from tools.esign import _next_doc_id, _generate_pdf, _generate_page_images, _send_signing_email
    import threading as _th
    import uuid as _uuid

    namespace = body.get("namespace") or request.headers.get("X-Namespace", "demo")
    reference_id = body.get("reference_id", "")
    callback_url = body.get("callback_url", "")
    language = body.get("language", "fr")
    expiration_days = int(body.get("expiration_days", 30))
    reminder_freq = body.get("reminder_frequency", "EVERY_THIRD_DAY")
    send_email = body.get("send_email", True)
    signature_fields = body.get("signature_fields") or body.get("fields_config")

    # Quota check — skip for 'demo' namespace (testing/internal)
    if namespace != "demo":
        from tools.subscriptions import check_quota, increment_usage
        import sqlite3 as _sqlite3
        _qconn = _sqlite3.connect(str(db.DB_PATH))
        _qconn.row_factory = _sqlite3.Row
        _qcur = _qconn.cursor()
        _quota = check_quota(namespace, _qcur)
        if not _quota.allowed:
            _qconn.close()
            return JSONResponse(
                {"error": _quota.reason or "Envelope quota exceeded", "quota_exceeded": True},
                status_code=402,
            )
        _qconn.close()

    doc_id = _next_doc_id(namespace)
    pdf_preview_url = f"{MCP_BASE_URL}/esign/{doc_id}/preview.pdf"
    html_path = ""

    # ---- Determine PDF source ----
    file_url = body.get("file_url", "")
    if file_url:
        # FocusingPro compat: download external PDF (run in executor to avoid blocking event loop)
        import asyncio as _aio
        import requests as _req
        pdf_path = str(ESIGN_DATA_DIR / namespace / f"{doc_id}.pdf")
        Path(pdf_path).parent.mkdir(parents=True, exist_ok=True)

        def _download():
            r = _req.get(file_url, timeout=60)
            r.raise_for_status()
            with open(pdf_path, "wb") as fh:
                fh.write(r.content)

        try:
            loop = _aio.get_event_loop()
            await loop.run_in_executor(None, _download)
        except Exception as e:
            return JSONResponse({"error": f"Failed to download file_url: {e}"}, status_code=400)
        from pypdf import PdfReader
        try:
            total_pages = len(PdfReader(pdf_path).pages)
        except Exception:
            total_pages = 1
        # Build signers list from body
        signers_raw = body.get("signers", [])
        if not signers_raw:
            return JSONResponse({"error": "signers array is required with file_url"}, status_code=400)
        fields = body.get("fields", {})
        template = "external"
        signer_name = next((s.get("name", "") for s in signers_raw if s.get("order", 1) == 1), "")
        signer_email = next((s.get("email", "") for s in signers_raw if s.get("order", 1) == 1), "")
    else:
        # ClawShow native: generate from template
        template = body.get("template", "enrollment_contract")
        fields = body.get("fields", {})
        # Support both native (signer_name) and FocusingPro signers array
        signers_raw = body.get("signers", [])
        if signers_raw:
            signer_name = next((s.get("name", "") for s in signers_raw if s.get("order", 1) == 1), "")
            signer_email = next((s.get("email", "") for s in signers_raw if s.get("order", 1) == 1), "")
        else:
            signer_name = body.get("signer_name", "")
            signer_email = body.get("signer_email", "")
            signers_raw = [{"name": signer_name, "email": signer_email, "order": 1, "role": "student"}]

        if not signer_name:
            return JSONResponse({"error": "signer_name is required"}, status_code=400)
        try:
            html_path, pdf_path, total_pages = _generate_pdf(doc_id, namespace, template, signer_name, fields)
        except Exception as e:
            return JSONResponse({"error": f"PDF generation failed: {e}"}, status_code=500)

    # Generate page images
    pages_dir = str(ESIGN_DATA_DIR / namespace / f"{doc_id}_pages")
    try:
        _generate_page_images(pdf_path, pages_dir)
    except Exception:
        pass

    # Build first signer's signing URL for document record
    first_token = str(_uuid.uuid4())
    signing_url = f"{MCP_BASE_URL}/esign/{doc_id}?token={first_token}"

    db.create_esign_document(
        doc_id=doc_id,
        namespace=namespace,
        template=template,
        signer_name=signer_name,
        signer_email=signer_email,
        fields=fields,
        signing_url=signing_url,
        original_pdf_path=pdf_path,
        rendered_html_path=html_path,
        reference_id=reference_id,
        callback_url=callback_url,
        language=language,
        send_email=send_email,
        total_pages=total_pages,
        signature_positions=signature_fields,
        initial_status="student_signing",
    )

    # Store expiration/reminder config in DB (new columns, optional migration)
    try:
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE esign_documents SET expiration_days=?, reminder_frequency=? WHERE id=?",
                (expiration_days, reminder_freq, doc_id),
            )
    except Exception:
        pass  # columns may not exist yet on older schema

    # Create signer records
    signer_responses = []
    for s in sorted(signers_raw, key=lambda x: x.get("order", 1)):
        tok = first_token if s.get("order", 1) == 1 else str(_uuid.uuid4())
        role = s.get("role", "student")
        name = s.get("name", s.get("signer_name", ""))
        email = s.get("email", s.get("signer_email", ""))
        order = s.get("order", 1)
        db.create_esign_signer(
            document_id=doc_id,
            role=role,
            signer_name=name,
            signer_email=email,
            signing_order=order,
            token=tok,
        )
        signer_url = f"{MCP_BASE_URL}/esign/{doc_id}?token={tok}"
        signer_responses.append({
            "role": role,
            "name": name,
            "email": email,
            "order": order,
            "status": "pending",
            "signing_url": signer_url,
        })

    db.log_esign_audit(doc_id, "created", {
        "signer_name": signer_name,
        "signer_email": signer_email,
        "total_pages": total_pages,
        "file_url": file_url or None,
        "signers_count": len(signers_raw),
    })

    # Record usage event (skip for demo namespace)
    if namespace != "demo":
        try:
            from tools.subscriptions import check_quota, increment_usage
            import sqlite3 as _sqlite3
            _uconn = _sqlite3.connect(str(db.DB_PATH))
            _uconn.row_factory = _sqlite3.Row
            _ucur = _uconn.cursor()
            _q = check_quota(namespace, _ucur)
            increment_usage(namespace, doc_id, _ucur, is_overage=_q.is_overage, overage_rate_cents=_q.overage_rate_cents)
            _uconn.commit()
            _uconn.close()
        except Exception:
            pass  # usage tracking failure must never block document creation

    if send_email and signer_email:
        _th.Thread(
            target=_send_signing_email,
            args=(signer_name, signer_email, signing_url, doc_id, language),
            daemon=True,
        ).start()

    return JSONResponse({
        "success": True,
        "document_id": doc_id,
        "signing_url": signing_url,
        "pdf_preview_url": pdf_preview_url,
        "total_pages": total_pages,
        "setup_url": f"{MCP_BASE_URL}/esign/{doc_id}/setup",
        "status": "student_signing",
        "signer_email": signer_email,
        "signers": signer_responses,
    })


async def esign_signing_page(request: Request):
    """GET /esign/{document_id}[?token=UUID] — serve the V2 multi-page signing page."""
    from starlette.responses import HTMLResponse
    doc_id = request.path_params["document_id"]
    token = request.query_params.get("token", "")
    doc = db.get_esign_document(doc_id)
    if not doc:
        return HTMLResponse(
            "<h2 style='font-family:Arial;padding:40px'>Document not found or link expired.</h2>",
            status_code=404,
        )
    if doc["status"] == "completed":
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>Ce document a déjà été signé. Merci !</h2>")
    if doc["status"] == "declined":
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>Ce document a été refusé.</h2>")

    # Validate token if provided
    if token:
        signer = db.get_signer_by_token(token)
        if signer:
            db.mark_signer_viewed(signer["id"])
            db.log_esign_audit(doc_id, "viewed", {"signer_role": signer["role"]}, signer_id=signer["id"])

    page_html = _render_signing_page(doc, token)
    return HTMLResponse(page_html)


async def esign_preview_pdf(request: Request):
    """GET /esign/{document_id}/preview.pdf — serve the unsigned PDF."""
    from starlette.responses import FileResponse
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    pdf_path = doc.get("original_pdf_path", "")
    if not pdf_path or not Path(pdf_path).exists():
        return JSONResponse({"error": "PDF not found"}, status_code=404)
    return FileResponse(pdf_path, media_type="application/pdf")


async def esign_signed_pdf(request: Request):
    """GET /esign/{document_id}/signed.pdf — serve the signed PDF."""
    from starlette.responses import FileResponse
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc or not doc.get("signed_pdf_path"):
        return JSONResponse({"error": "Signed document not found"}, status_code=404)
    path = doc["signed_pdf_path"]
    if not Path(path).exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc_id}-signed.pdf"'},
    )


async def esign_submit_signature(request: Request) -> JSONResponse:
    """POST /esign/{document_id}/sign — receive paraphes + final signature, generate signed PDF."""
    import base64 as _b64
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    if doc["status"] == "completed":
        return JSONResponse({"error": "Already completed"}, status_code=409)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    token = body.get("token", "")
    sig_data_url = body.get("signature_png", "")
    lu_data_url = body.get("lu_approuve_png", "")
    city = body.get("city", "Paris") or "Paris"
    paraphes_raw = body.get("paraphes", {})  # {page_num_str: dataURL}
    paraphe_pos_override = body.get("paraphe_pos")  # {x,y,w,h} dragged by signer

    # OTP gate: signer must have verified OTP before submitting
    if token:
        signer_for_otp = db.get_signer_by_token(token)
        if signer_for_otp and not db.is_otp_verified(doc_id, signer_for_otp["id"]):
            return JSONResponse(
                {"error": "OTP verification required before signing."},
                status_code=403,
            )

    if not sig_data_url or not sig_data_url.startswith("data:image/png;base64,"):
        return JSONResponse({"error": "Missing or invalid signature_png"}, status_code=400)

    sig_bytes = _b64.b64decode(sig_data_url.split(",", 1)[1])
    lu_bytes = b""
    if lu_data_url and lu_data_url.startswith("data:image/png;base64,"):
        lu_bytes = _b64.b64decode(lu_data_url.split(",", 1)[1])

    # Decode paraphes dict {page_num_int: bytes}
    paraphes_bytes: dict = {}
    for pg_str, data_url in paraphes_raw.items():
        if data_url and data_url.startswith("data:image/png;base64,"):
            try:
                paraphes_bytes[int(pg_str)] = _b64.b64decode(data_url.split(",", 1)[1])
            except Exception:
                pass

    signer_ip = _get_real_ip(request)
    signed_at = datetime.now(timezone.utc).isoformat()
    namespace = doc["namespace"]
    original_pdf = doc.get("original_pdf_path", "")

    if not original_pdf or not Path(original_pdf).exists():
        return JSONResponse({"error": "Source PDF not found"}, status_code=500)

    signed_pdf = str(ESIGN_DATA_DIR / namespace / f"{doc_id}-signed.pdf")
    original_html = doc.get("rendered_html_path", "")
    try:
        if original_html and Path(original_html).exists():
            # Template-based: re-render HTML with placeholders replaced → correct positions + city/date
            from tools.esign import _embed_signature_in_pdf
            _embed_signature_in_pdf(
                rendered_html_path=original_html,
                signed_pdf=signed_pdf,
                signature_png_bytes=sig_bytes,
                signer_name=doc["signer_name"],
                signed_at=signed_at,
                signer_ip=signer_ip,
                lu_approuve_png_bytes=lu_bytes,
                city=city,
            )
            # Overlay per-page paraphes on top of the re-rendered PDF
            if paraphes_bytes:
                import shutil as _shutil
                from tools.esign import _overlay_signatures_pdf
                _tmp = signed_pdf + ".tmp.pdf"
                _overlay_signatures_pdf(signed_pdf, _tmp, paraphes=paraphes_bytes, final_sig={})
                _shutil.move(_tmp, signed_pdf)
        else:
            # Uploaded PDF: append a signature certificate page (preserves original layout)
            _saved_pos_raw = doc.get("signature_positions")
            _saved_pos = (json.loads(_saved_pos_raw) if isinstance(_saved_pos_raw, str) else _saved_pos_raw) if _saved_pos_raw else None
            if paraphe_pos_override and isinstance(paraphe_pos_override, dict):
                if _saved_pos is None:
                    from tools.esign import _DEFAULT_SIG_POSITIONS as _dp_
                    _saved_pos = dict(_dp_)
                else:
                    _saved_pos = dict(_saved_pos)
                _saved_pos["paraphe"] = paraphe_pos_override
            from tools.esign import _overlay_signatures_pdf
            _overlay_signatures_pdf(
                original_pdf, signed_pdf,
                paraphes=paraphes_bytes,
                final_sig={
                    "sig_bytes": sig_bytes,
                    "lu_bytes": lu_bytes,
                    "city": city,
                    "signer_name": doc["signer_name"],
                    "signed_at": signed_at,
                    "signer_ip": signer_ip,
                    "doc_id": doc_id,
                },
                sig_positions=_saved_pos,
                certificate_page=True,
            )
    except Exception as e:
        return JSONResponse({"error": f"PDF signing failed: {e}"}, status_code=500)

    # Consent: apply TEST watermark if signer did not accept CGU
    _accept_cgu = bool(body.get("accept_conditions", False))
    if not _accept_cgu:
        try:
            from adapters.esign.watermark import apply_watermark as _apply_wm
            with open(signed_pdf, "rb") as _fw:
                _wm_in = _fw.read()
            _wm_out = _apply_wm(_wm_in)
            with open(signed_pdf, "wb") as _fw:
                _fw.write(_wm_out)
            logger.info("TEST watermark applied: doc_id=%s", doc_id)
        except Exception as _wm_err:
            logger.error("Watermark failed: %s", _wm_err)
    try:
        import sqlite3 as _sql3
        _cdb = _sql3.connect(str(db.DB_PATH))
        if _accept_cgu:
            _ccur = _cdb.execute(
                "INSERT INTO esign_consents (doc_id, role, user_email, ip_address) VALUES (?, 'signer', ?, ?)",
                (doc_id, doc.get("signer_email", ""), signer_ip)
            )
            _cdb.execute(
                "UPDATE esign_documents SET is_production=1, signer_consent_id=?, accept_conditions=1 WHERE id=?",
                (_ccur.lastrowid, doc_id)
            )
        else:
            _cdb.execute("UPDATE esign_documents SET is_production=0 WHERE id=?", (doc_id,))
        _cdb.commit()
        _cdb.close()
    except Exception as _ce:
        logger.error("Consent DB failed: %s", _ce)

    # S3 archive — upload signed PDF (non-blocking, failure does not abort signing)
    try:
        from adapters.esign.s3_archive import upload_signed_pdf
        with open(signed_pdf, "rb") as _f:
            _pdf_bytes = _f.read()
        _s3_info = upload_signed_pdf(
            doc_id=doc_id,
            signer_email=doc.get("signer_email", "unknown"),
            pdf_bytes=_pdf_bytes,
            doc_name=doc.get("doc_name", ""),
        )
        db.update_esign_s3_url(doc_id, _s3_info["s3_url"])
    except Exception as _s3_err:
        logger.error("S3 archive failed for %s: %s", doc_id, _s3_err)

    # Update signer record if token provided
    signer_id = None
    if token:
        signer = db.get_signer_by_token(token)
        if signer:
            signer_id = signer["id"]
            db.update_signer_signed(
                signer_id=signer_id,
                signer_ip=signer_ip,
                signature_png=sig_data_url,
                lu_approuve_png=lu_data_url,
                paraphes={k: v for k, v in paraphes_raw.items()},
                city=city,
            )

    db.complete_esign_document(doc_id, signed_pdf, signer_ip, city=city, lu_approuve="lu et approuvé")
    db.log_esign_audit(doc_id, "signed", {
        "city": city,
        "pages_paraphed": list(paraphes_bytes.keys()),
        "signer_ip": signer_ip,
    }, signer_id=signer_id)

    signed_pdf_url = f"{MCP_BASE_URL}/esign/{doc_id}/signed.pdf"

    # Send completion email to signer
    try:
        from tools.esign import _send_completion_email
        _send_completion_email(
            recipient_name=doc.get("signer_name", ""),
            recipient_email=doc.get("signer_email", ""),
            doc_id=doc_id,
            signed_pdf_url=signed_pdf_url,
        )
        logger.info("Completion email sent: doc_id=%s to=%s", doc_id, doc.get("signer_email", ""))
    except Exception as _mail_err:
        logger.error("Completion email failed: doc_id=%s error=%s", doc_id, _mail_err)

    # Resolve effective callback URL: per-doc > namespace-level
    callback_url = doc.get("callback_url", "")
    if not callback_url:
        from tools.webhooks import get_namespace_webhook_url
        callback_url = get_namespace_webhook_url(namespace) or ""

    if callback_url:
        def _fire_callback():
            import requests as _r
            payload = {
                "event": "signer.signed",
                "provider": "clawshow_esign",
                "document_id": doc_id,
                "reference_id": doc.get("reference_id", ""),
                "namespace": doc.get("namespace", ""),
                "status": "student_signed",
                "signer": {
                    "role": "student",
                    "name": doc["signer_name"],
                    "email": doc.get("signer_email", ""),
                    "signed_at": signed_at,
                    "ip": signer_ip,
                },
                "signed_pdf_url": signed_pdf_url,
                "audit_url": f"{MCP_BASE_URL}/esign/{doc_id}/audit",
            }
            hdrs = {
                "Content-Type": "application/json",
                "X-ClawShow-Event": "signer.signed",
                "X-ClawShow-Document-Id": doc_id,
                "X-ClawShow-Timestamp": signed_at,
            }
            for attempt in range(3):
                try:
                    resp = _r.post(callback_url, json=payload, headers=hdrs, timeout=10)
                    db.log_esign_audit(doc_id, "webhook_sent" if resp.status_code == 200 else "webhook_failed", {
                        "event": "signer.signed", "attempt": attempt + 1, "status": resp.status_code,
                    })
                    if resp.status_code == 200:
                        return
                except Exception as exc:
                    db.log_esign_audit(doc_id, "webhook_error", {
                        "event": "signer.signed", "attempt": attempt + 1, "error": str(exc),
                    })
                import time
                time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s, 4s

        threading.Thread(target=_fire_callback, daemon=True).start()

    return JSONResponse({
        "success": True,
        "document_id": doc_id,
        "signed_pdf_url": signed_pdf_url,
        "signed_at": signed_at,
    })


async def esign_decline(request: Request) -> JSONResponse:
    """POST /esign/{document_id}/decline — signer refuses to sign."""
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    if doc["status"] in ("completed", "declined"):
        return JSONResponse({"error": f"Document already {doc['status']}"}, status_code=409)

    try:
        body = await request.json()
        reason = body.get("reason", "")
        token = body.get("token", "")
    except Exception:
        reason = ""
        token = ""

    signer_ip = _get_real_ip(request)

    if token:
        signer = db.get_signer_by_token(token)
        if signer:
            db.update_signer_status(signer["id"], "declined", reason)

    result = db.decline_esign_document(doc_id, signer_ip, reason)
    db.log_esign_audit(doc_id, "declined", {"reason": reason, "signer_ip": signer_ip})
    return JSONResponse(result)


async def esign_documents_recent(request: Request) -> JSONResponse:
    """GET /esign-documents/recent?namespace=x&limit=10 — dashboard recent activity."""
    namespace = request.query_params.get("namespace", "")
    if not namespace:
        # Try session cookie fallback
        from tools.auth import get_session_user
        user = get_session_user(request)
        if user:
            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT namespace FROM user_namespaces WHERE user_id = ? LIMIT 1",
                    (user["id"],),
                ).fetchone()
                if row:
                    namespace = row[0]
    if not namespace:
        return JSONResponse({"error": "namespace required"}, status_code=400)
    limit = min(int(request.query_params.get("limit", 10)), 50)
    with db.get_conn() as conn:
        rows = conn.execute(
            """SELECT id, reference_id, signer_name, signer_email,
                      status, created_at, signed_at, completed_at
               FROM esign_documents
               WHERE namespace = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (namespace, limit),
        ).fetchall()
    return JSONResponse([
        {
            "id": r[0],
            "reference_id": r[1] or "",
            "signer_name": r[2] or "",
            "signer_email": r[3] or "",
            "status": r[4] or "",
            "created_at": r[5] or "",
            "signed_at": r[6],
            "completed_at": r[7],
        }
        for r in rows
    ])


async def esign_status(request: Request) -> JSONResponse:
    """GET /esign/{document_id}/status — return current signing status with signers."""
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    signers = db.get_signers_by_document(doc_id)
    return JSONResponse({
        "document_id": doc_id,
        "status": doc["status"],
        "signer_name": doc["signer_name"],
        "signer_email": doc["signer_email"],
        "total_pages": doc.get("total_pages", 1),
        "signed_at": doc.get("signed_at"),
        "completed_at": doc.get("completed_at"),
        "city": doc.get("city"),
        "reference_id": doc.get("reference_id", ""),
        "signed_pdf_url": f"{MCP_BASE_URL}/esign/{doc_id}/signed.pdf" if doc["status"] == "completed" else None,
        "created_at": doc.get("created_at"),
        "signers": [
            {
                "role": s["role"],
                "name": s["signer_name"],
                "email": s.get("signer_email", ""),
                "status": s["status"],
                "signed_at": s.get("signed_at"),
                "signing_order": s["signing_order"],
            }
            for s in signers
        ],
        "audit_url": f"{MCP_BASE_URL}/esign/{doc_id}/audit",
    })


async def esign_audit(request: Request) -> JSONResponse:
    """GET /esign/{document_id}/audit — return audit log for the document."""
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT action, detail, ip_address, created_at FROM esign_audit_log"
            " WHERE document_id = ? ORDER BY id",
            (doc_id,),
        ).fetchall()
    events = []
    for r in rows:
        detail = r["detail"]
        try:
            import json as _j
            detail = _j.loads(detail) if detail else {}
        except Exception:
            pass
        events.append({
            "action": r["action"],
            "detail": detail,
            "ip": r["ip_address"],
            "at": r["created_at"],
        })
    return JSONResponse({"document_id": doc_id, "events": events})


# ---------------------------------------------------------------------------
# Dragons Elysées 龙城酒楼 — API endpoints
# ---------------------------------------------------------------------------

import hmac as _hmac
import hashlib as _hashlib
import random as _random
import string as _string
import base64 as _b64

try:
    import tools.dragons_elysees_db as _de_db
    _de_db.init_tables()
    logger.info("Dragons Elysées DB initialized")
except Exception as _de_init_err:
    logger.error(f"Dragons Elysées DB init failed: {_de_init_err}")
    _de_db = None  # type: ignore

_DE_JWT_SECRET = os.environ.get("DRAGONS_JWT_SECRET", "dragons-elysees-default-secret-change-me")
_DE_GMAIL_USER = os.environ.get("DRAGONS_GMAIL_USER", os.environ.get("GMAIL_USER", ""))
_DE_GMAIL_PASS = os.environ.get("DRAGONS_GMAIL_APP_PASSWORD", os.environ.get("GMAIL_APP_PASSWORD", ""))


def _de_create_token(customer_id: int, email: str) -> str:
    """Create a signed JWT (HS256) without external dependencies."""
    import time as _time
    import json as _j
    header = _b64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = _b64.urlsafe_b64encode(
        _j.dumps({"customer_id": customer_id, "email": email, "iat": int(_time.time())}).encode()
    ).rstrip(b"=").decode()
    unsigned = f"{header}.{payload}"
    sig = _hmac.new(_DE_JWT_SECRET.encode(), unsigned.encode(), _hashlib.sha256).digest()
    sig_b64 = _b64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{unsigned}.{sig_b64}"


def _de_verify_token(token: str) -> dict | None:
    """Verify HS256 JWT and return payload dict, or None if invalid."""
    import json as _j
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload_b64, sig = parts
        unsigned = f"{header}.{payload_b64}"
        expected = _hmac.new(_DE_JWT_SECRET.encode(), unsigned.encode(), _hashlib.sha256).digest()
        expected_b64 = _b64.urlsafe_b64encode(expected).rstrip(b"=").decode()
        if not _hmac.compare_digest(sig, expected_b64):
            return None
        padding = (4 - len(payload_b64) % 4) % 4
        return _j.loads(_b64.urlsafe_b64decode(payload_b64 + "=" * padding))
    except Exception:
        return None


def _de_get_customer_from_request(request: Request) -> dict | None:
    """Extract and verify Bearer token, return payload or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return _de_verify_token(auth[7:])


def _de_send_otp_email(email: str, code: str) -> None:
    """Send OTP code via Gmail SMTP (runs in background thread)."""
    if _os.environ.get("DEV_MODE", "false") == "true":
        logger.info(f"[DEV MODE] Dragons OTP for {email}: {code}")
        print(f"\n{'='*50}\n[DEV MODE] OTP for {email} -> {code}\n{'='*50}\n", flush=True)
        return
    if not _DE_GMAIL_USER or not _DE_GMAIL_PASS:
        logger.warning("Dragons Elysées: Gmail not configured, skipping OTP email")
        return
    try:
        text = (
            f"Bonjour,\n\n"
            f"Votre code de connexion : {code}\n\n"
            f"Ce code expire dans 10 minutes.\n\n"
            f"Dragons Elysées 龙城酒楼\n"
            f"11 Rue de Berri, 75008 Paris"
        )
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Dragons Elysées - Votre code de connexion"
        msg["From"] = f"Dragons Elysées <{_DE_GMAIL_USER}>"
        msg["To"] = email
        msg.attach(MIMEText(text, "plain", "utf-8"))
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as srv:
            srv.login(_DE_GMAIL_USER, _DE_GMAIL_PASS)
            srv.send_message(msg)
        logger.info(f"Dragons OTP sent to {email}")
    except Exception:
        logger.exception("Failed to send Dragons OTP email")


# ── Auth endpoints ──

async def de_send_otp(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/auth/send-otp"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    email = data.get("email", "").strip().lower()
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    code = "".join(_random.choices(_string.digits, k=6))
    _de_db.save_otp(email, code)
    threading.Thread(target=_de_send_otp_email, args=(email, code), daemon=True).start()
    return JSONResponse({"success": True, "message": "Code envoyé"})


async def de_verify_otp(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/auth/verify-otp"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    email = data.get("email", "").strip().lower()
    code = data.get("code", "").strip()
    referral_code_input = data.get("referral_code")
    if not email or not code:
        return JSONResponse({"error": "email and code required"}, status_code=400)
    if not _de_db.verify_and_consume_otp(email, code):
        return JSONResponse({"error": "Code invalide ou expiré"}, status_code=401)
    customer = _de_db.get_or_create_customer(email)
    token = _de_create_token(customer["id"], email)
    bal = _de_db.get_balance(customer["id"])

    # ──────────────────────────────────────────────────────
    # Referral binding (idempotent, anti-abuse)
    # 仅在以下条件全满足时绑定：
    #   1. 前端传了 referral_code
    #   2. 当前 customer 还没有 referred_by_code（首次机会）
    #   3. referral_code 对应的推荐人存在
    #   4. 推荐人不是自己（自荐拦截）
    # ──────────────────────────────────────────────────────
    referred_by_response = None
    if referral_code_input:
        ref_code_clean = referral_code_input.strip().upper()
        try:
            with _de_db.get_conn() as conn:
                # 查当前 customer 是否已有 referred_by_code
                row = conn.execute(
                    "SELECT referred_by_code FROM customers WHERE id = ?",
                    (customer["id"],),
                ).fetchone()
                already_bound = row and row["referred_by_code"]

                if not already_bound:
                    # 查推荐人
                    referrer = _de_db.get_customer_by_referral_code(ref_code_clean)
                    if referrer and referrer["id"] != customer["id"]:
                        # 写入 referred_by_code
                        conn.execute(
                            "UPDATE customers SET referred_by_code = ? WHERE id = ? AND (referred_by_code IS NULL OR referred_by_code = '')",
                            (ref_code_clean, customer["id"]),
                        )
                        referred_by_response = ref_code_clean
                        logger.info(
                            "[DRAGONS REFERRAL] customer_id=%s bound to referrer_code=%s",
                            customer["id"], ref_code_clean
                        )
                    else:
                        if referrer and referrer["id"] == customer["id"]:
                            logger.warning(
                                "[DRAGONS REFERRAL] self-referral blocked: customer_id=%s",
                                customer["id"]
                            )
                        else:
                            logger.warning(
                                "[DRAGONS REFERRAL] invalid code: %s (customer_id=%s)",
                                ref_code_clean, customer["id"]
                            )
        except Exception as e:
            # Referral 失败不能影响登录 - 记录后继续
            logger.error("[DRAGONS REFERRAL] binding error: %s", e, exc_info=True)

    return JSONResponse({
        "success": True,
        "token": token,
        "customer": {
            "id": customer["id"],
            "email": customer["email"],
            "name": customer.get("name"),
            "balance": bal["balance"],
        },
        "referred_by": referred_by_response,
    })


async def de_me(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/auth/me"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    payload = _de_get_customer_from_request(request)
    if not payload:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    customer = _de_db.get_customer_by_id(payload["customer_id"])
    if not customer:
        return JSONResponse({"error": "Customer not found"}, status_code=404)
    bal = _de_db.get_balance(payload["customer_id"])
    return JSONResponse({
        "id": customer["id"],
        "email": customer["email"],
        "name": customer.get("name"),
        "balance": bal["balance"],
    })


# ── Order endpoints ──

async def de_create_order(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/orders"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    order = _de_db.create_order(data)
    if not order:
        return JSONResponse({"error": "Failed to create order"}, status_code=500)
    return JSONResponse(order, status_code=201)


async def de_get_orders(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/orders"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    status = request.query_params.get("status", "")
    date = request.query_params.get("date", "")
    order_number = request.query_params.get("order_number", "")
    order_type = request.query_params.get("order_type", "")
    cid_str = request.query_params.get("customer_id", "")
    customer_id = int(cid_str) if cid_str.isdigit() else None
    orders = _de_db.query_orders(
        status=status, date=date, customer_id=customer_id,
        order_number=order_number, order_type=order_type,
    )
    return JSONResponse({"orders": orders, "total": len(orders)})


async def de_get_stats(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/stats?date=YYYY-MM-DD"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    from datetime import date as _date
    date = request.query_params.get("date", _date.today().isoformat())
    return JSONResponse(_de_db.get_stats(date))


async def de_get_order(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/orders/{id}"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    order_id = int(request.path_params["id"])
    order = _de_db.get_order_by_id(order_id)
    if not order:
        return JSONResponse({"error": "Order not found"}, status_code=404)
    return JSONResponse(order)


async def de_update_order(request: Request) -> JSONResponse:
    """PATCH /api/dragons-elysees/orders/{id}"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    order_id = int(request.path_params["id"])
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    new_status = data.get("status", "")
    if not new_status:
        return JSONResponse({"error": "status required"}, status_code=400)
    changed_by = data.get("changed_by", "system")
    old_order = _de_db.get_order_by_id(order_id)
    if not old_order:
        return JSONResponse({"error": "Order not found"}, status_code=404)
    order = _de_db.update_order_status(order_id, new_status, changed_by=changed_by)
    if new_status == "paid" and old_order.get("status") != "paid":
        _de_db.apply_cashback(order_id)
        order = _de_db.get_order_by_id(order_id)
    return JSONResponse(order)


async def de_track_order(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/orders/track/{order_number}"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    order_number = request.path_params["order_number"]
    order = _de_db.get_order_tracking(order_number)
    if not order:
        return JSONResponse({"error": "Order not found"}, status_code=404)
    return JSONResponse(order)


async def de_delivery_config(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/delivery-config"""
    return JSONResponse({
        "base_fee": 5.00,
        "free_threshold": 50.00,
        "max_distance_km": 5,
        "restaurant_lat": 48.8738,
        "restaurant_lng": 2.3065,
    })


# ── Balance endpoints ──

async def de_get_balance(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/balance"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    payload = _de_get_customer_from_request(request)
    if not payload:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_de_db.get_balance(payload["customer_id"]))


async def de_get_transactions(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/balance/transactions"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    payload = _de_get_customer_from_request(request)
    if not payload:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    limit = int(request.query_params.get("limit", "20"))
    offset = int(request.query_params.get("offset", "0"))
    return JSONResponse(_de_db.get_transactions(payload["customer_id"], limit=limit, offset=offset))


# ── Payment endpoints ──

async def de_payment_create(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/payment/create

    Supports payment_method: stancer (default) | sumup
    For sumup, also accepts payment_mode: online (default) | in_person_solo | in_person_caisse | cash
    """
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    order_id = data.get("order_id")
    order_number = data.get("order_number")
    amount = data.get("amount", 0)
    return_url = data.get("return_url", "")
    payment_method = (data.get("payment_method") or "stancer").strip().lower()
    payment_mode = (data.get("payment_mode") or "online").strip().lower()

    # resolve order — accept either order_id or order_number
    if order_id:
        order = _de_db.get_order_by_id(int(order_id))
    elif order_number:
        order = _de_db.get_order_by_number(str(order_number))
    else:
        return JSONResponse({"error": "order_id or order_number required"}, status_code=400)

    if not amount:
        return JSONResponse({"error": "amount required"}, status_code=400)
    if not order:
        return JSONResponse({"error": "Order not found"}, status_code=404)

    resolved_order_id = order["id"]

    if not return_url:
        return_url = (
            f"https://jason2016.github.io/dragons-elysees/"
            f"#/payment-success?order={order['order_number']}"
        )

    # ── SumUp path ────────────────────────────────────────────────────────────
    if payment_method == "sumup":
        import logging as _log_de_pay
        _de_pay_log = _log_de_pay.getLogger("de_payment_create")
        try:
            from tools.payment import _create_sumup_by_mode
            amount_cents = int(round(float(amount) * 100))
            result = _create_sumup_by_mode(
                namespace="dragons-elysees",
                amount=amount_cents,
                currency="EUR",
                description=f"Dragons Elysées - {order['order_number']}",
                payment_mode=payment_mode,
                device_id=data.get("device_id") or None,
                return_url=return_url,
                external_reference=str(resolved_order_id),
                items=data.get("items"),
            )
        except Exception as exc:
            _de_pay_log.error(f"[DRAGONS SUMUP] _create_sumup_by_mode error: {exc}", exc_info=True)
            return JSONResponse({"error": "payment_unavailable", "detail": str(exc)}, status_code=502)

        if not result.get("success"):
            return JSONResponse({"error": result.get("error", "SumUp error")}, status_code=502)

        checkout_id = result.get("payment_id", "")
        payment_url = result.get("payment_url", "")

        # mock online: append query params so /mock-checkout page can show amount + trigger success
        if result.get("is_mock") and payment_mode == "online" and payment_url:
            from urllib.parse import urlencode as _urlencode
            _qp = {
                "namespace": "dragons-elysees",
                "order_id": str(resolved_order_id),
                "amount": f"{float(amount):.2f}",
                "currency": "EUR",
                "return_url": return_url,
            }
            payment_url = payment_url + "?" + _urlencode(_qp)

        _de_db.update_order_payment(resolved_order_id, checkout_id, "sumup")
        _de_pay_log.info(
            f"[DRAGONS SUMUP] order_id={resolved_order_id} mode={payment_mode} "
            f"checkout_id={checkout_id} is_mock={result.get('is_mock')}"
        )
        return JSONResponse({
            "payment_url": payment_url,
            "payment_id": checkout_id,
            "provider": "sumup",
            "mode": payment_mode,
            "status": result.get("status", "pending"),
            "is_mock": result.get("is_mock", False),
        })

    # ── Stancer path (default, preserved) ─────────────────────────────────────
    if payment_method != "stancer":
        return JSONResponse({"error": f"Unknown payment_method: {payment_method}"}, status_code=400)

    stancer_key = _os.environ.get("STANCER_SECRET_KEY", "")
    if not stancer_key or stancer_key.startswith("stest_your"):
        return JSONResponse({"error": "Stancer not configured"}, status_code=500)
    auth = _b64.b64encode(f"{stancer_key}:".encode()).decode()
    amount_cents = int(float(amount) * 100)
    try:
        resp = _req_lib.post(
            "https://api.stancer.com/v2/payment_intents/",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            json={
                "amount": amount_cents,
                "currency": "eur",
                "description": f"Dragons Elysées - {order['order_number']}",
                "return_url": return_url,
            },
            timeout=15,
        )
        result = resp.json()
    except Exception as e:
        return JSONResponse({"error": "payment_unavailable", "fallback": True, "detail": str(e)}, status_code=502)
    if resp.status_code not in (200, 201):
        return JSONResponse(
            {"error": "payment_unavailable", "fallback": True, "detail": result.get("error", f"Stancer {resp.status_code}")},
            status_code=502,
        )
    payment_id = result.get("id", "")
    payment_url = result.get("url", "")
    if not payment_url:
        return JSONResponse({"error": "payment_unavailable", "fallback": True, "detail": "No payment URL from Stancer"}, status_code=502)
    _de_db.update_order_payment(resolved_order_id, payment_id, "stancer")
    return JSONResponse({"payment_url": payment_url, "payment_id": payment_id})


async def de_payment_webhook(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/payment/webhook"""
    if _de_db is None:
        return JSONResponse({"received": True})
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"received": True})
    payment_id = data.get("id") or data.get("payment_id", "")  # Stancer sends "id"
    status = data.get("status", "")
    if not payment_id:
        return JSONResponse({"received": True})
    order = _de_db.get_order_by_payment_id(payment_id)
    if not order:
        return JSONResponse({"received": True})
    paid_statuses = {"paid", "succeeded", "captured", "to_capture", "authorized"}
    if status in paid_statuses and order.get("status") != "paid":
        _de_db.update_order_status(order["id"], "paid")
        _de_db.apply_cashback(order["id"])
    return JSONResponse({"received": True})


# ── Referral & Loyalty endpoints ────────────────────────────────────────────

_DE_REFERRAL_COMMISSION = 0.10
_DE_REFERRAL_MIN_ORDER = 10.00
_DE_REFERRAL_MAX_24H = 5


async def de_get_referral_info(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/customer/{id}/referral"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        customer_id = int(request.path_params["id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "Invalid id"}, status_code=400)
    customer = _de_db.get_customer_by_id(customer_id)
    if not customer:
        return JSONResponse({"error": "Customer not found"}, status_code=404)
    if not customer.get("referral_code"):
        return JSONResponse({"error": "Referral not enabled for this customer"}, status_code=409)
    stats = _de_db.get_referral_stats(customer_id)
    referral_url = f"https://jason2016.github.io/dragons-elysees/?ref={customer['referral_code']}"
    import urllib.parse as _uparse
    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={_uparse.quote(referral_url)}"
    return JSONResponse({
        "customer_id": customer_id,
        "referral_code": customer["referral_code"],
        "referral_url": referral_url,
        "qr_code_url": qr_url,
        "stats": stats,
    })


async def de_register_customer(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/customer/register"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip().lower()
    name = (data.get("name") or "").strip()
    ref_code = (data.get("referral_code") or "").strip().upper()
    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)
    if _de_db.get_customer_by_phone(phone):
        return JSONResponse({"error": "Phone already registered"}, status_code=409)

    referred_by_code = None
    referrer_id = None
    if ref_code:
        referrer = _de_db.get_customer_by_referral_code(ref_code)
        if referrer:
            if referrer["phone"] == phone:
                _de_db.log_fraud_attempt(referrer["phone"], phone, "self_referral")
            elif _de_db.count_referrals_by_referrer_recent(referrer["id"]) >= _DE_REFERRAL_MAX_24H:
                _de_db.log_fraud_attempt(referrer["phone"], phone, "rate_limit")
            else:
                referred_by_code = ref_code
                referrer_id = referrer["id"]

    new_code = _de_db.generate_unique_referral_code()
    new_id = _de_db.create_customer_with_referral({
        "phone": phone,
        "email": email,
        "name": name,
        "referral_code": new_code,
        "referred_by_code": referred_by_code,
    })
    return JSONResponse({
        "success": True,
        "customer_id": new_id,
        "referral_code": new_code,
        "referred_by": referrer_id,
    }, status_code=201)


async def de_order_complete_referral(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/order/complete — trigger referral reward if applicable"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    customer_id = data.get("customer_id")
    order_ref = str(data.get("order_id") or "")
    try:
        order_amount = float(data.get("order_amount", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "order_amount must be numeric"}, status_code=400)
    if not customer_id:
        return JSONResponse({"error": "customer_id required"}, status_code=400)

    customer = _de_db.get_customer_by_id(int(customer_id))
    if not customer:
        return JSONResponse({"success": False, "reason": "customer_not_found"})
    if not customer.get("referred_by_code"):
        return JSONResponse({"success": True, "rewarded": False, "reason": "no_referrer"})
    if _de_db.has_triggered_referral_reward(int(customer_id)):
        return JSONResponse({"success": True, "rewarded": False, "reason": "already_rewarded"})
    if order_amount < _DE_REFERRAL_MIN_ORDER:
        return JSONResponse({"success": True, "rewarded": False, "reason": "below_min_order"})

    referrer = _de_db.get_customer_by_referral_code(customer["referred_by_code"])
    if not referrer:
        return JSONResponse({"success": True, "rewarded": False, "reason": "referrer_not_found"})

    commission = round(order_amount * _DE_REFERRAL_COMMISSION, 2)
    try:
        event_id = _de_db.create_referral_event({
            "referrer_customer_id": referrer["id"],
            "referred_customer_id": int(customer_id),
            "order_ref": order_ref,
            "order_amount": order_amount,
            "commission_amount": commission,
            "event_type": "referral_first_order",
            "status": "credited",
        })
    except Exception:
        return JSONResponse({"success": True, "rewarded": False, "reason": "duplicate_event"})

    referred_name = customer.get("name") or customer.get("phone") or "client"
    _de_db.add_balance_transaction(
        referrer["id"],
        "referral_reward",
        commission,
        f"Recommandation: {referred_name} (commande €{order_amount:.2f})",
    )
    return JSONResponse({
        "success": True,
        "rewarded": True,
        "referrer_id": referrer["id"],
        "commission_amount": commission,
        "event_id": event_id,
    })


async def de_redeem_balance(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/order/redeem-balance"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        customer_id = int(data["customer_id"])
        order_ref = str(data.get("order_id") or "")
        order_amount = float(data["order_amount"])
        redeem_amount = float(data["redeem_amount"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "customer_id, order_id, order_amount, redeem_amount required"}, status_code=400)

    if redeem_amount <= 0:
        return JSONResponse({"error": "redeem_amount must be positive"}, status_code=400)
    if redeem_amount > order_amount:
        return JSONResponse({"error": "redeem_amount exceeds order amount"}, status_code=400)

    bal = _de_db.get_balance(customer_id)
    if redeem_amount > bal["balance"]:
        return JSONResponse({"error": "Insufficient balance", "available": bal["balance"]}, status_code=400)

    _de_db.add_balance_transaction(
        customer_id,
        "payment",
        -redeem_amount,
        f"Utilisation solde - Commande {order_ref}",
    )
    new_balance = round(bal["balance"] - redeem_amount, 2)
    return JSONResponse({
        "success": True,
        "redeemed": redeem_amount,
        "new_balance": new_balance,
        "order_amount_after": round(order_amount - redeem_amount, 2),
    })


async def de_customer_balance_history(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/customer/{id}/balance/history"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        customer_id = int(request.path_params["id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "Invalid id"}, status_code=400)
    customer = _de_db.get_customer_by_id(customer_id)
    if not customer:
        return JSONResponse({"error": "Customer not found"}, status_code=404)
    limit = int(request.query_params.get("limit", "20"))
    bal = _de_db.get_balance(customer_id)
    txns = _de_db.get_transactions(customer_id, limit=limit)
    return JSONResponse({
        "customer_id": customer_id,
        "current_balance": bal["balance"],
        "transactions": txns["transactions"],
        "total": txns["total"],
    })


async def de_admin_simulate_review(request: Request) -> JSONResponse:
    """POST /api/dragons-elysees/admin/simulate-google-review (demo mode only)"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        customer_id = int(data["customer_id"])
        rating = int(data.get("rating", 5))
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "customer_id required"}, status_code=400)
    review_text = str(data.get("review_text") or "")

    customer = _de_db.get_customer_by_id(customer_id)
    if not customer:
        return JSONResponse({"error": "Customer not found"}, status_code=404)
    last_order = _de_db.get_customer_last_order(customer_id)
    if not last_order:
        return JSONResponse({"error": "Customer has no paid orders"}, status_code=400)

    from datetime import timezone as _tz
    now_iso = datetime.now(_tz.utc).isoformat()
    review_id = _de_db.insert_google_review({
        "google_reviewer_name": customer.get("name", ""),
        "google_reviewer_email": customer.get("email", ""),
        "rating": rating,
        "review_text": review_text,
        "review_date": now_iso,
        "matched_customer_id": customer_id,
        "matched_at": now_iso,
        "raw_email_content": "[SIMULATED FOR DEMO - production mode uses IMAP after 5/19]",
    })
    reward_amount = _de_reward_for_review(customer_id, last_order, rating, review_text, review_id)
    bal_after = _de_db.get_balance(customer_id)["balance"]
    return JSONResponse({
        "success": True,
        "review_id": review_id,
        "reward_amount": reward_amount,
        "customer_balance_after": bal_after,
        "message": "Demo: Google review reward triggered. Production mode (5/19+) will use IMAP listener.",
    })


def _de_reward_for_review(customer_id: int, last_order: dict, rating: int, review_text: str, review_id: int) -> float:
    """Credit google review reward. Returns amount credited (0 if already rewarded)."""
    reward_amount = round(float(last_order.get("total_paid", 0)) * _DE_REFERRAL_COMMISSION, 2)
    if reward_amount <= 0:
        return 0.0
    try:
        _de_db.create_referral_event({
            "referrer_customer_id": customer_id,
            "referred_customer_id": customer_id,
            "order_ref": str(last_order.get("order_number", last_order.get("id", ""))),
            "order_amount": float(last_order.get("total_paid", 0)),
            "commission_amount": reward_amount,
            "event_type": "google_review",
            "status": "credited",
            "metadata": {"google_review_id": review_id, "rating": rating, "review_text": review_text},
        })
    except Exception:
        return 0.0  # UNIQUE constraint: already rewarded this customer
    _de_db.add_balance_transaction(
        customer_id,
        "review_reward",
        reward_amount,
        f"Avis Google ({rating}★) - récompense fidélité",
    )
    _de_db.mark_review_rewarded(review_id)
    return reward_amount


async def de_admin_customers_with_orders(request: Request) -> JSONResponse:
    """GET /api/dragons-elysees/admin/customers-with-orders"""
    if _de_db is None:
        return JSONResponse({"error": "Service unavailable"}, status_code=503)
    customers = _de_db.get_customers_with_orders_list()
    for c in customers:
        c["balance"] = _de_db.get_balance(c["id"])["balance"]
    return JSONResponse({"customers": customers, "total": len(customers)})

# ---------------------------------------------------------------------------
# Documentation page
# ---------------------------------------------------------------------------

_DOCS_DIR = Path(__file__).parent / "docs"


async def serve_docs_esign(request: Request):
    """GET /docs/esign — render esign API reference as HTML with sidebar TOC."""
    from starlette.responses import HTMLResponse
    md_path = _DOCS_DIR / "esign-api-reference.md"
    if not md_path.exists():
        return HTMLResponse("<p>Documentation not found.</p>", status_code=404)
    md_content = md_path.read_text(encoding="utf-8")
    md_escaped = md_content.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>ClawShow eSign API Reference</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.1/github-markdown-light.min.css"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css"/>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
/* TOP BAR */
.topbar{{background:#0f172a;padding:0 24px;height:48px;display:flex;align-items:center;gap:12px;position:fixed;top:0;left:0;right:0;z-index:100}}
.topbar-brand{{color:#fff;font-weight:700;font-size:15px;text-decoration:none}}
.topbar-badge{{font-size:11px;background:rgba(59,130,246,0.25);color:#93c5fd;padding:2px 8px;border-radius:99px;border:1px solid rgba(59,130,246,0.3)}}
.topbar-link{{color:#94a3b8;font-size:13px;text-decoration:none;margin-left:auto}}
.topbar-link:hover{{color:#fff}}
/* LAYOUT */
.layout{{display:flex;padding-top:48px;min-height:100vh}}
/* SIDEBAR */
.sidebar{{width:256px;flex-shrink:0;background:#fff;border-right:1px solid #e2e8f0;position:fixed;top:48px;bottom:0;overflow-y:auto;padding:20px 0}}
.sidebar::-webkit-scrollbar{{width:4px}}
.sidebar::-webkit-scrollbar-track{{background:transparent}}
.sidebar::-webkit-scrollbar-thumb{{background:#cbd5e1;border-radius:2px}}
.toc-section{{margin-bottom:4px}}
.toc-h2{{display:flex;align-items:center;gap:6px;padding:6px 16px;font-size:13px;font-weight:600;color:#334155;text-decoration:none;cursor:pointer;border-left:3px solid transparent;transition:all .15s}}
.toc-h2:hover{{background:#f1f5f9;color:#1e293b}}
.toc-h2.active{{color:#2563eb;border-left-color:#2563eb;background:#eff6ff}}
.toc-h2 .arrow{{font-size:10px;color:#94a3b8;margin-left:auto;transition:transform .15s}}
.toc-h2.open .arrow{{transform:rotate(90deg)}}
.toc-children{{overflow:hidden;max-height:0;transition:max-height .2s ease}}
.toc-children.open{{max-height:600px}}
.toc-h3{{display:block;padding:4px 16px 4px 28px;font-size:12px;color:#64748b;text-decoration:none;border-left:3px solid transparent;transition:all .15s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.toc-h3:hover{{background:#f8fafc;color:#334155}}
.toc-h3.active{{color:#2563eb;border-left-color:#2563eb;background:#eff6ff}}
/* CONTENT */
.main{{margin-left:256px;flex:1;padding:40px 48px 80px;max-width:960px}}
.markdown-body{{background:transparent;padding:0}}
.markdown-body pre{{border-radius:8px}}
.markdown-body h1,.markdown-body h2,.markdown-body h3{{scroll-margin-top:72px}}
.markdown-body h2{{border-top:1px solid #e2e8f0;padding-top:32px;margin-top:40px}}
.markdown-body h2:first-child{{border-top:none;padding-top:0;margin-top:0}}
/* MOBILE */
@media(max-width:768px){{
  .sidebar{{display:none}}
  .main{{margin-left:0;padding:24px 16px 60px}}
}}
</style>
</head>
<body>
<div class="topbar">
  <a class="topbar-brand" href="https://clawshow.ai">ClawShow</a>
  <span class="topbar-badge">eSign API</span>
  <a class="topbar-link" href="https://app.clawshow.ai">Dashboard →</a>
</div>
<div class="layout">
  <nav class="sidebar" id="sidebar"></nav>
  <main class="main">
    <div class="markdown-body" id="content"></div>
  </main>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>
const md = `{md_escaped}`;

// Slug helper matching marked's default
function slug(text){{
  return text.toLowerCase().replace(/[^\w\s-]/g,'').replace(/\s+/g,'-').replace(/-+/g,'-');
}}

// Custom renderer: add id anchors to headings
const renderer = new marked.Renderer();
renderer.heading = function(text, level){{
  const id = slug(text.replace(/<[^>]*>/g,''));
  return `<h${{level}} id="${{id}}">${{text}}</h${{level}}>\n`;
}};
renderer.code = function(code, lang){{
  const hl = lang && hljs.getLanguage(lang)
    ? hljs.highlight(code, {{language:lang}}).value
    : hljs.highlightAuto(code).value;
  return `<pre><code class="hljs language-${{lang||''}}">${{hl}}</code></pre>\n`;
}};

marked.use({{renderer, gfm:true, breaks:false}});
document.getElementById('content').innerHTML = marked.parse(md);

// Build sidebar TOC from rendered headings
const headings = Array.from(document.querySelectorAll('#content h2, #content h3'));
const sidebar = document.getElementById('sidebar');
let currentSection = null;
let currentChildren = null;

headings.forEach(h => {{
  if(h.tagName === 'H2'){{
    const sec = document.createElement('div');
    sec.className = 'toc-section';
    const a = document.createElement('a');
    a.className = 'toc-h2';
    a.href = '#' + h.id;
    a.dataset.id = h.id;
    a.innerHTML = h.textContent + '<span class="arrow">›</span>';
    const children = document.createElement('div');
    children.className = 'toc-children';
    a.addEventListener('click', e => {{
      e.preventDefault();
      // Toggle children
      const isOpen = children.classList.contains('open');
      children.classList.toggle('open', !isOpen);
      a.classList.toggle('open', !isOpen);
      // Scroll to heading
      document.getElementById(h.id)?.scrollIntoView({{behavior:'smooth',block:'start'}});
    }});
    sec.appendChild(a);
    sec.appendChild(children);
    sidebar.appendChild(sec);
    currentSection = a;
    currentChildren = children;
  }} else if(h.tagName === 'H3' && currentChildren){{
    const a = document.createElement('a');
    a.className = 'toc-h3';
    a.href = '#' + h.id;
    a.dataset.id = h.id;
    a.textContent = h.textContent;
    a.addEventListener('click', e => {{
      e.preventDefault();
      document.getElementById(h.id)?.scrollIntoView({{behavior:'smooth',block:'start'}});
    }});
    currentChildren.appendChild(a);
    // Auto-open parent when h3 added
    currentChildren.classList.add('open');
    if(currentSection) currentSection.classList.add('open');
  }}
}});

// Scroll spy
const allLinks = sidebar.querySelectorAll('[data-id]');
const allHeadings = Array.from(document.querySelectorAll('#content h2,#content h3'));

function updateActive(){{
  const scrollY = window.scrollY + 80;
  let active = null;
  for(const h of allHeadings){{
    if(h.offsetTop <= scrollY) active = h.id;
    else break;
  }}
  allLinks.forEach(a => {{
    const on = a.dataset.id === active;
    a.classList.toggle('active', on);
    // auto-open parent section if child is active
    if(on && a.classList.contains('toc-h3')){{
      const parent = a.closest('.toc-section');
      if(parent){{
        parent.querySelector('.toc-children')?.classList.add('open');
        parent.querySelector('.toc-h2')?.classList.add('open');
      }}
    }}
  }});
}}

window.addEventListener('scroll', updateActive, {{passive:true}});
updateActive();

// Open first section by default
const firstSection = sidebar.querySelector('.toc-section');
if(firstSection){{
  firstSection.querySelector('.toc-children')?.classList.add('open');
  firstSection.querySelector('.toc-h2')?.classList.add('open');
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


async def serve_docs_esign_md(request: Request):
    """GET /docs/esign-api-reference.md — raw markdown download."""
    from starlette.responses import Response
    md_path = _DOCS_DIR / "esign-api-reference.md"
    if not md_path.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return Response(
        content=md_path.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "inline; filename=\"esign-api-reference.md\""},
    )


# ---------------------------------------------------------------------------
# Billing Public Payment Page API (P0-1)
# ---------------------------------------------------------------------------
from api.billing_public import (
    billing_public_get,
    billing_public_pay,
    billing_public_status,
    billing_public_options,
)

# ---------------------------------------------------------------------------
# Combined ASGI app (MCP SSE + /stats + /webhook/stripe + /reports + /api)
# ---------------------------------------------------------------------------

def _build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/docs/esign", serve_docs_esign, methods=["GET"]),
            Route("/docs/esign-api-reference.md", serve_docs_esign_md, methods=["GET"]),
            Route("/stats", stats, methods=["GET"]),
            Route("/webhook/stripe", stripe_webhook, methods=["POST"]),
            Route("/mock-checkout/{checkout_id}", mock_checkout_page, methods=["GET"]),
            Route("/webhook/{namespace}/sumup", sumup_webhook_handler, methods=["POST"]),
            Route("/api/dev/mock-payment-success/{namespace}/{order_id}", mock_payment_success, methods=["POST"]),
            Route("/webhooks/mollie", mollie_webhook, methods=["POST"]),
            Route("/webhooks/stripe", stripe_billing_webhook, methods=["POST"]),
            Route("/webhooks/esign", billing_esign_webhook, methods=["POST"]),
            Route("/reports/{namespace}/{filename}", serve_report, methods=["GET"]),
            Route("/api/booking", api_create_booking, methods=["POST"]),
            Route("/api/booking/checkin", api_checkin_booking, methods=["PATCH"]),
            Route("/api/booking/{id:int}", api_update_booking, methods=["PATCH"]),
            Route("/api/bookings", api_query_bookings, methods=["GET"]),
            Route("/api/bookings/summary", api_booking_summary, methods=["GET"]),
            Route("/api/order/create", api_create_dine_order, methods=["POST"]),
            Route("/api/order/queue", api_order_queue, methods=["GET"]),
            Route("/api/order/history", api_order_history, methods=["GET"]),
            Route("/api/order/{id:int}/complete", api_order_complete, methods=["PATCH"]),
            Route("/api/order/{id:int}/confirm-payment", api_order_confirm_payment, methods=["POST"]),
            Route("/api/inventory", api_inventory_get, methods=["GET"]),
            Route("/api/inventory/set", api_inventory_set, methods=["POST"]),
            Route("/api/inventory/restore-yesterday", api_inventory_restore_yesterday, methods=["POST"]),
            Route("/api/order/{id:int}/picked", api_order_picked, methods=["PATCH"]),
            Route("/api/order/{id:int}/mark-printed", api_order_mark_printed, methods=["PATCH"]),
            Route("/api/payment/create", api_payment_create, methods=["POST"]),
            Route("/api/payment/verify", api_payment_verify, methods=["GET"]),
            Route("/api/order/{id:int}/status", api_order_status, methods=["GET"]),
            Route("/esign-documents/recent", esign_documents_recent, methods=["GET"]),
            # eSign user portal (esign.clawshow.ai)
            Route("/signup", page_signup, methods=["GET"]),
            Route("/login", page_login, methods=["GET"]),
            Route("/login/verify", page_login_verify, methods=["GET"]),
            Route("/dashboard", page_dashboard, methods=["GET"]),
            Route("/new", page_new_esign, methods=["GET"]),
            Route("/esign/auth/signup", auth_signup, methods=["POST"]),
            Route("/esign/auth/login/otp/send", auth_login_otp_send, methods=["POST"]),
            Route("/esign/auth/login/verify", auth_login_verify, methods=["POST"]),
            Route("/esign/auth/logout", auth_logout, methods=["POST"]),
            Route("/esign/auth/me", auth_me, methods=["GET"]),
            Route("/esign/user/documents", auth_user_documents, methods=["GET"]),
            Route("/esign/user/create", esign_user_create, methods=["POST"]),
            Route("/esign/create", esign_create, methods=["POST"]),
            Route("/esign/{document_id}/setup", esign_setup_get, methods=["GET"]),
            Route("/esign/{document_id}/setup", esign_setup_post, methods=["POST"]),
            Route("/esign/{document_id}/page/{page_num}.png", esign_page_image, methods=["GET"]),
            Route("/esign/{document_id}/otp/send", esign_otp_send, methods=["POST"]),
            Route("/esign/{document_id}/otp/verify", esign_otp_verify, methods=["POST"]),
            Route("/esign/{document_id}/sign", esign_submit_signature, methods=["POST"]),
            Route("/esign/{document_id}/decline", esign_decline, methods=["POST"]),
            Route("/esign/{document_id}/status", esign_status, methods=["GET"]),
            Route("/esign/{document_id}/audit", esign_audit, methods=["GET"]),
            Route("/esign/{document_id}/preview.pdf", esign_preview_pdf, methods=["GET"]),
            Route("/esign/{document_id}/signed.pdf", esign_signed_pdf, methods=["GET"]),
            Route("/esign/{document_id}", esign_signing_page, methods=["GET"]),
            # Dragons Elysées 龙城酒楼
            Route("/api/dragons-elysees/auth/send-otp", de_send_otp, methods=["POST"]),
            Route("/api/dragons-elysees/auth/verify-otp", de_verify_otp, methods=["POST"]),
            Route("/api/dragons-elysees/auth/me", de_me, methods=["GET"]),
            Route("/api/dragons-elysees/orders", de_get_orders, methods=["GET"]),
            Route("/api/dragons-elysees/orders", de_create_order, methods=["POST"]),
            Route("/api/dragons-elysees/orders/track/{order_number}", de_track_order, methods=["GET"]),
            Route("/api/dragons-elysees/orders/{id:int}", de_get_order, methods=["GET"]),
            Route("/api/dragons-elysees/orders/{id:int}", de_update_order, methods=["PATCH"]),
            Route("/api/dragons-elysees/delivery-config", de_delivery_config, methods=["GET"]),
            Route("/api/dragons-elysees/balance/transactions", de_get_transactions, methods=["GET"]),
            Route("/api/dragons-elysees/balance", de_get_balance, methods=["GET"]),
            Route("/api/dragons-elysees/stats", de_get_stats, methods=["GET"]),
            Route("/api/dragons-elysees/payment/create", de_payment_create, methods=["POST"]),
            Route("/api/dragons-elysees/payment/webhook", de_payment_webhook, methods=["POST"]),
            # Dragons Elysées — referral & loyalty
            Route("/api/dragons-elysees/customer/register", de_register_customer, methods=["POST"]),
            Route("/api/dragons-elysees/customer/{id:int}/referral", de_get_referral_info, methods=["GET"]),
            Route("/api/dragons-elysees/customer/{id:int}/balance/history", de_customer_balance_history, methods=["GET"]),
            Route("/api/dragons-elysees/order/complete", de_order_complete_referral, methods=["POST"]),
            Route("/api/dragons-elysees/order/redeem-balance", de_redeem_balance, methods=["POST"]),
            Route("/api/dragons-elysees/admin/simulate-google-review", de_admin_simulate_review, methods=["POST"]),
            Route("/api/dragons-elysees/admin/customers-with-orders", de_admin_customers_with_orders, methods=["GET"]),
            # Neige Rouge 红雪餐厅 — receipts & invoices
            Route("/api/neige-rouge/orders/{id:int}/receipt", api_nr_receipt, methods=["GET"]),
            Route("/api/neige-rouge/orders/{id:int}/invoice", api_nr_invoice_get, methods=["GET"]),
            Route("/api/neige-rouge/orders/{id:int}/invoice", api_nr_invoice_post, methods=["POST"]),
            # Neige Rouge 红雪餐厅 — booking deposit & arrive
            Route("/api/neige-rouge/bookings/verify", api_nr_booking_verify, methods=["GET"]),
            Route("/api/neige-rouge/bookings/{id:int}/arrive", api_nr_booking_arrive, methods=["POST"]),
            Route("/api/neige-rouge/bookings/{id:int}/receipt", api_nr_booking_receipt, methods=["GET"]),
            Route("/api/neige-rouge/bookings/{id:int}/use-deposit", api_nr_booking_use_deposit, methods=["POST"]),
            Route("/api/neige-rouge/bookings/{id:int}/refund", api_nr_booking_refund, methods=["POST"]),
            Route("/api/neige-rouge/orders/{id:int}/checkout", api_nr_checkout, methods=["POST"]),
            # ClawShow SaaS — auth
            Route("/auth/request-login", auth_request_login, methods=["POST"]),
            Route("/auth/verify",        auth_verify,         methods=["GET"]),
            Route("/auth/logout",        auth_logout,         methods=["POST"]),
            # ClawShow SaaS — accounts (namespace management)
            Route("/accounts/me",            accounts_me,             methods=["GET"]),
            Route("/accounts",               accounts_create,         methods=["POST"]),
            Route("/accounts/{namespace}",   accounts_get,            methods=["GET"]),
            Route("/internal/invite-founding", internal_invite_founding, methods=["POST"]),
            # ClawShow SaaS — subscriptions & quota
            Route("/subscriptions/current",        subscriptions_current,         methods=["GET"]),
            Route("/subscriptions/upgrade-intent", subscriptions_upgrade_intent,  methods=["POST"]),
            # ClawShow SaaS — API keys
            Route("/api-keys",       api_keys_create, methods=["POST"]),
            Route("/api-keys",       api_keys_list,   methods=["GET"]),
            Route("/api-keys/{id}",  api_keys_revoke, methods=["DELETE"]),
            # ClawShow SaaS — webhook config
            Route("/webhooks/config", webhooks_config_get,   methods=["GET"]),
            Route("/webhooks/config", webhooks_config_patch, methods=["PATCH"]),
            # Billing public payment page (P0-1)
            Route("/api/billing/public/{token}", billing_public_options, methods=["OPTIONS"]),
            Route("/api/billing/public/{token}", billing_public_get,     methods=["GET"]),
            Route("/api/billing/public/{token}/pay",    billing_public_pay,    methods=["POST"]),
            Route("/api/billing/public/{token}/status", billing_public_status, methods=["GET"]),
            Mount("/", app=mcp.sse_app()),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origin_regex=r"https://.*\.github\.io",
                allow_origins=[
                    "https://clawshow.ai",
                    "https://www.clawshow.ai",
                    "https://mcp.clawshow.ai",
                    "https://app.clawshow.ai",
                    "http://localhost:5173",
                    "http://localhost:3000",
                ],
                allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                allow_headers=["*"],
                allow_credentials=True,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stdio", action="store_true",
        help="Run with stdio transport (Claude Desktop)"
    )
    args = parser.parse_args()

    if args.stdio:
        mcp.run(transport="stdio")
    else:
        print(f"ClawShow MCP Server starting — http://{_host}:{_port}/sse")
        print(f"Stats endpoint           — http://{_host}:{_port}/stats")
        from engines.billing_engine.scheduler import start_scheduler, start_unsigned_contract_cleanup
        start_scheduler()
        start_unsigned_contract_cleanup()
        app = _build_app()
        uvicorn.run(app, host=_host, port=_port)
