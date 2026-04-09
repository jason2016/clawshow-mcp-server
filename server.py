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
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP

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


def _send_booking_email(data: dict, booking_code: str) -> None:
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
          </div>
          <div style="background:#faf8f5;padding:20px;text-align:center;border:1px solid #eee;border-top:none;border-radius:0 0 12px 12px">
            <p style="margin:0 0 8px;font-size:14px"><strong>7 rue des Ursulines, 75005 Paris</strong></p>
            <p style="margin:0 0 12px;font-size:14px">📞 01 72 60 46 89</p>
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
    if result.get("success"):
        threading.Thread(
            target=_send_booking_email,
            args=(data, result.get("booking_code", "")),
            daemon=True,
        ).start()
    return JSONResponse(result, status_code=201 if result.get("success") else 400)


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


import base64
import requests as _req_lib
import os as _os

async def api_payment_create(request: Request) -> JSONResponse:
    """POST /api/payment/create — create a Stancer payment for a dine order."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    namespace = data.get("namespace", "")
    amount = data.get("amount", 0)       # in cents
    description = data.get("description", "Commande Neige Rouge")
    order_id = data.get("order_id")
    if not namespace or not amount or not order_id:
        return JSONResponse({"error": "namespace, amount, order_id required"}, status_code=400)
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
.cvwrap canvas{display:block;cursor:crosshair}
.cv-clr{position:absolute;top:5px;right:8px;border:none;background:transparent;font-size:12px;color:#999;padding:2px 6px}
.use-saved-btn{margin-top:6px;padding:5px 10px;border:1px solid #1976d2;background:#e3f2fd;color:#1976d2;border-radius:5px;font-size:12px}
/* MODALS */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:300;display:flex;align-items:center;justify-content:center;padding:16px}
.modal-box{background:#fff;border-radius:12px;max-width:480px;width:100%;padding:24px;box-shadow:0 8px 32px rgba(0,0,0,.18);max-height:92vh;overflow-y:auto}
.modal-box h3{font-size:17px;margin-bottom:14px;color:#222}
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
#typePrev{border:1px solid #e0e0e0;border-radius:6px;height:68px;background:#fafafa;overflow:hidden;margin-bottom:10px;display:flex;align-items:center;justify-content:center}
#typePrevCv{max-width:100%}
.draw-wrap{border:1px solid #ddd;border-radius:6px;background:#fafafa;margin-bottom:6px}
#drawCv{width:100%;display:block;height:120px;cursor:crosshair}
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
        <div class="cvwrap"><canvas id="luCv" height="60"></canvas><button class="cv-clr" id="luClrBtn" onclick="clrLu()"></button></div>
      </div>
      <div class="ff">
        <label id="sigLbl"></label>
        <div class="cvwrap"><canvas id="fsCv" height="90"></canvas><button class="cv-clr" id="fsClrBtn" onclick="clrFs()"></button></div>
        <button class="use-saved-btn" id="useSavedBtn" onclick="useSavedSig()"></button>
      </div>
    </div>
  </div>
</div>

<!-- RESULT -->
<div id="resultArea" style="display:none"></div>

<!-- MODAL: SIGNATURE SETUP -->
<div id="sigModal" class="modal-bg" style="display:none" onclick="bgClick(event,'sigModal')">
  <div class="modal-box" onclick="event.stopPropagation()">
    <h3 id="mSetupH3"></h3>
    <div class="mtabs">
      <button class="mtab on" id="tabType" onclick="switchTab('type')"></button>
      <button class="mtab" id="tabDraw" onclick="switchTab('draw')"></button>
      <button class="mtab" id="tabImg" onclick="switchTab('img')"></button>
    </div>
    <div class="tp on" id="tpType">
      <input class="name-inp" id="typeName" oninput="drawTyped()"/>
      <div class="font-row" id="fontRow"></div>
      <div class="color-row" id="tColRow"></div>
      <div id="typePrev"><canvas id="typePrevCv" height="60"></canvas></div>
    </div>
    <div class="tp" id="tpDraw">
      <div class="color-row" id="dColRow"></div>
      <div class="draw-wrap"><canvas id="drawCv" height="120"></canvas></div>
      <p class="cv-hint" id="cvHint"></p>
    </div>
    <div class="tp" id="tpImg">
      <div class="upload-zone" onclick="document.getElementById('imgFile').click()">
        <p id="uploadTxt" style="font-size:14px;color:#666"></p>
        <p style="font-size:11px;color:#aaa;margin-top:4px">PNG, JPG, JPEG — max 5 MB</p>
      </div>
      <input type="file" id="imgFile" accept=".png,.jpg,.jpeg" style="display:none" onchange="onImgFile()"/>
      <img id="imgPrev" alt=""/>
    </div>
    <div class="legal-row"><input type="checkbox" id="legalCb" onchange="chkBtn()"/><label for="legalCb" id="legalLbl"></label></div>
    <div class="btn-row">
      <button class="btn-sec" id="clrBtn" onclick="clrSetup()"></button>
      <button class="btn-pri" id="btnConfirm" onclick="confirmSetup()" disabled></button>
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
  sigTarget:null, sigPage:null,
  font:FONTS[0].css, color:'#000000',
  drawHas:false, imgData:null,
  luHas:false, fsHas:false,
  dCtx:null, luCtx:null, fsCtx:null
};

/* ---- PROGRESS ---- */
function reqLeft(){
  let d=Object.keys(S.paraphes).length;
  if(S.ff.cb1)d++;if(S.ff.cb2)d++;if(S.ff.city)d++;if(S.ff.lu)d++;if(S.ff.fs)d++;
  return (S.total+5)-d;
}
function updateBar(){
  const total=S.total+5,done=total-reqLeft();
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
function goNext(){
  for(let p=1;p<=S.total;p++){if(!S.paraphes[p]){gotoPage(p);return;}}
  if(S.cur!==S.total){gotoPage(S.total);return;}
  const fs=document.getElementById('finalSec');
  if(!S.ff.cb1){document.getElementById('cb1').focus();fs.scrollIntoView({behavior:'smooth',block:'start'});return;}
  if(!S.ff.cb2){document.getElementById('cb2').focus();return;}
  if(!S.ff.city){document.getElementById('cityInp').focus();return;}
  if(!S.ff.lu){document.getElementById('luCv').scrollIntoView({behavior:'smooth',block:'center'});return;}
  if(!S.ff.fs){document.getElementById('fsCv').scrollIntoView({behavior:'smooth',block:'center'});}
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
  const n=S.cur;
  if(C.school_mode){
    // Read-only student paraphe overlay
    const sp=C.student_paraphes&&C.student_paraphes[String(n)];
    if(sp){const zs=document.createElement('div');zs.className='sz done';zs.style.cssText='right:3.5%;bottom:2%;width:18%;height:6%';const img=document.createElement('img');img.src=sp;zs.appendChild(img);c.appendChild(zs);}
    // School signature zone on last page only
    if(n===S.total){
      const sdone=S.ff.fs;
      const zsch=document.createElement('div');
      zsch.className='sz '+(sdone?'done':'pend');
      zsch.style.cssText='left:3.5%;bottom:8%;width:22%;height:9%';
      if(sdone){const img=document.createElement('img');img.src=S.savedSig||'';zsch.appendChild(img);}
      else{zsch.innerHTML='<span class="zi">✍</span><span class="zh">Administration scolaire</span>';zsch.addEventListener('click',()=>{S.sigTarget='school';openSetup();});}
      c.appendChild(zsch);
    }
    return;
  }
  const done=!!S.paraphes[n];
  const z=document.createElement('div');
  z.className='sz '+(done?'done':'pend');
  z.style.cssText='right:3.5%;bottom:2%;width:18%;height:6%';
  if(done){const img=document.createElement('img');img.src=S.paraphes[n];z.appendChild(img);}
  else{z.innerHTML='<span class="zi">✍</span><span class="zh">'+(L.zone_sign||'Signer ici')+'</span>';z.addEventListener('click',()=>zoneClick(n));}
  c.appendChild(z);
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

/* ---- SETUP MODAL ---- */
function openSetup(){
  document.getElementById('legalCb').checked=false;
  chkBtn();
  if(C.signer_name&&!document.getElementById('typeName').value)
    document.getElementById('typeName').value=C.signer_name;
  drawTyped();
  document.getElementById('sigModal').style.display='flex';
}
function closeSetup(){document.getElementById('sigModal').style.display='none';}
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
  const legal=document.getElementById('legalCb').checked;
  const act=document.querySelector('.mtab.on');
  const tid=act?act.id:'tabType';
  let has=(tid==='tabType')?document.getElementById('typeName').value.trim().length>0
         :(tid==='tabDraw')?S.drawHas
         :!!S.imgData;
  document.getElementById('btnConfirm').disabled=!(legal&&has);
}
function clrSetup(){
  document.getElementById('typeName').value='';
  if(S.dCtx){const c=document.getElementById('drawCv');S.dCtx.clearRect(0,0,c.width,c.height);}
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
    setTimeout(()=>{if(S.sigPage<S.total)gotoPage(S.sigPage+1);},300);
  } else if(S.sigTarget==='school'){
    const cv=document.getElementById('fsCv');szCv(cv);
    const ctx=cv.getContext('2d');const img=new Image();
    img.onload=()=>{ctx.clearRect(0,0,cv.width,cv.height);ctx.drawImage(img,0,0,cv.width,cv.height);};
    img.src=dataURL;S.fsHas=true;S.ff.fs=true;updateBar();renderZones();
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
  const w=box.offsetWidth||340;cv.width=w;cv.height=60;
  const ctx=cv.getContext('2d');ctx.clearRect(0,0,w,60);
  if(name){ctx.font='36px '+S.font;ctx.fillStyle=S.color;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(name,w/2,30);}
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
function szCv(cv){const w=(cv.parentElement?cv.parentElement.offsetWidth:320)||320;const h=cv.height||90;cv.width=w;cv.height=h;}
function attachDraw(cv,ctx,onDraw,getColor){
  let drawing=false,lx=0,ly=0;
  function pos(e){const r=cv.getBoundingClientRect(),s=e.touches?e.touches[0]:e;return[s.clientX-r.left,s.clientY-r.top];}
  function dn(e){e.preventDefault();[lx,ly]=pos(e);drawing=true;}
  function mv(e){
    if(!drawing)return;e.preventDefault();
    const[x,y]=pos(e);
    ctx.beginPath();ctx.moveTo(lx,ly);ctx.quadraticCurveTo(lx,ly,(lx+x)/2,(ly+y)/2);
    ctx.strokeStyle=getColor();ctx.lineWidth=2;ctx.lineCap='round';ctx.stroke();
    [lx,ly]=[x,y];if(onDraw)onDraw();
  }
  function up(){drawing=false;}
  cv.addEventListener('mousedown',dn);cv.addEventListener('mousemove',mv);
  cv.addEventListener('mouseup',up);cv.addEventListener('mouseleave',up);
  cv.addEventListener('touchstart',dn,{passive:false});cv.addEventListener('touchmove',mv,{passive:false});
  cv.addEventListener('touchend',up);
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

/* ---- FINISH ---- */
function openFinish(){document.getElementById('finCb').checked=false;document.getElementById('btnDoFin').disabled=true;document.getElementById('finishModal').style.display='flex';}
function doFinish(){
  closeModal('finishModal');
  const paraphes={};Object.entries(S.paraphes).forEach(([k,v])=>{paraphes[k]=v;});
  const luPng=S.luHas?document.getElementById('luCv').toDataURL('image/png'):'';
  const fsPng=S.fsHas?document.getElementById('fsCv').toDataURL('image/png'):(S.savedSig||'');
  const city=document.getElementById('cityInp').value.trim();
  document.getElementById('btnNextField').disabled=true;
  fetch('/esign/'+C.doc_id+'/sign'+(C.token?'?token='+C.token:''),{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:C.token,paraphes,signature_png:fsPng,lu_approuve_png:luPng,city,accept_conditions:true,accept_email:true})
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
  document.getElementById('lb1').textContent=L.cb1||'';
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
  document.getElementById('legalLbl').textContent=L.legal_label||"J'accepte que cette signature soit ma repr\u00e9sentation l\u00e9gale";
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
}
window.addEventListener('DOMContentLoaded',init);
window.addEventListener('resize',()=>{
  if(S.cur===S.total){szCv(document.getElementById('luCv'));szCv(document.getElementById('fsCv'));}
});
</script>
</body>
</html>"""


_OTP_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClawShow eSign — Verification</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.wrap{background:white;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,.12);width:100%;max-width:420px;overflow:hidden}
.hdr{background:#1a1a2e;padding:20px;text-align:center;color:white}
.hdr h1{font-size:18px;margin:0}
.hdr p{font-size:12px;opacity:.7;margin-top:4px}
.body{padding:28px}
.icon{text-align:center;font-size:40px;margin-bottom:16px}
h2{text-align:center;font-size:17px;color:#1a1a2e;margin-bottom:8px}
.email-hint{text-align:center;color:#666;font-size:14px;margin-bottom:24px}
.digits{display:flex;gap:8px;justify-content:center;margin-bottom:20px}
.digits input{width:44px;height:54px;text-align:center;font-size:24px;font-weight:700;border:2px solid #ddd;border-radius:8px;outline:none;color:#1a1a2e;transition:border-color .15s}
.digits input:focus{border-color:#1a1a2e}
.digits input.filled{border-color:#4CAF50;background:#f8fff8}
.err{color:#d32f2f;font-size:13px;text-align:center;margin-bottom:12px;min-height:18px}
.btn-verify{width:100%;padding:14px;background:#1a1a2e;color:white;border:none;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .2s}
.btn-verify:disabled{opacity:.45;cursor:default}
.resend-row{text-align:center;margin-top:16px;font-size:13px;color:#666}
.btn-resend{background:none;border:none;color:#1a1a2e;font-weight:600;cursor:pointer;font-size:13px;text-decoration:underline}
.btn-resend:disabled{color:#999;cursor:default;text-decoration:none}
.timer{text-align:center;font-size:12px;color:#999;margin-top:10px}
.loading{text-align:center;color:#666;font-size:14px;display:none}
.aes-badge{margin-top:20px;padding:10px;background:#f8f9ff;border-radius:6px;font-size:11px;color:#666;text-align:center;border:1px solid #e8eaff}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>ClawShow eSign</h1>
    <p>Signature Electronique Avancee (AES)</p>
  </div>
  <div class="body">
    <div class="icon">&#x1F512;</div>
    <h2>Verification d&#x27;identite</h2>
    <p class="email-hint">Un code a 6 chiffres a ete envoye a<br><strong id="maskedEmail">__MASKED_EMAIL__</strong></p>
    <div class="digits">
      <input type="tel" maxlength="1" id="d0" inputmode="numeric" pattern="[0-9]">
      <input type="tel" maxlength="1" id="d1" inputmode="numeric" pattern="[0-9]">
      <input type="tel" maxlength="1" id="d2" inputmode="numeric" pattern="[0-9]">
      <input type="tel" maxlength="1" id="d3" inputmode="numeric" pattern="[0-9]">
      <input type="tel" maxlength="1" id="d4" inputmode="numeric" pattern="[0-9]">
      <input type="tel" maxlength="1" id="d5" inputmode="numeric" pattern="[0-9]">
    </div>
    <div class="err" id="errMsg"></div>
    <button class="btn-verify" id="btnVerify" onclick="verify()">Verifier</button>
    <div class="loading" id="loading">Verification en cours...</div>
    <div class="resend-row">
      Vous n&#x27;avez pas recu le code ?
      <button class="btn-resend" id="btnResend" onclick="resend()" disabled>Renvoyer le code</button>
      <span id="resendTimer"></span>
    </div>
    <div class="timer" id="expTimer"></div>
    <div class="aes-badge">&#x1F512; Verification OTP — Signature AES conforme eIDAS (UE) n&#xB0;910/2014, Art. 26</div>
  </div>
</div>
<script>
const DOC_ID = "__DOC_ID__";
const TOKEN = "__TOKEN__";
let expireAt = Date.now() + __EXPIRES_IN__ * 1000;
let resendCooldown = 60;
let resendInterval;

// Auto-focus and auto-advance digits
const inputs = [0,1,2,3,4,5].map(i => document.getElementById('d'+i));
inputs.forEach((inp, i) => {
  inp.addEventListener('input', e => {
    const v = e.target.value.replace(/[^0-9]/g,'');
    e.target.value = v;
    if(v) {
      e.target.classList.add('filled');
      if(i < 5) inputs[i+1].focus();
      else document.getElementById('btnVerify').focus();
    } else {
      e.target.classList.remove('filled');
    }
    checkReady();
  });
  inp.addEventListener('keydown', e => {
    if(e.key === 'Backspace' && !e.target.value && i > 0) {
      inputs[i-1].focus(); inputs[i-1].value=''; inputs[i-1].classList.remove('filled');
    }
    if(e.key === 'Enter') verify();
  });
  inp.addEventListener('paste', e => {
    const txt = (e.clipboardData||window.clipboardData).getData('text').replace(/[^0-9]/g,'');
    if(txt.length >= 6) {
      e.preventDefault();
      txt.slice(0,6).split('').forEach((c,j) => { inputs[j].value=c; inputs[j].classList.add('filled'); });
      checkReady();
      document.getElementById('btnVerify').focus();
    }
  });
});
inputs[0].focus();

function getCode() { return inputs.map(i=>i.value).join(''); }
function checkReady() {
  document.getElementById('btnVerify').disabled = getCode().length < 6;
}
checkReady();

function verify() {
  const code = getCode();
  if(code.length < 6) return;
  document.getElementById('btnVerify').style.display='none';
  document.getElementById('loading').style.display='block';
  document.getElementById('errMsg').textContent='';
  fetch('/esign/'+DOC_ID+'/otp/verify', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({code, token: TOKEN})
  }).then(r=>r.json()).then(d => {
    document.getElementById('btnVerify').style.display='block';
    document.getElementById('loading').style.display='none';
    if(d.verified) {
      window.location.href = '/esign/'+DOC_ID+'?token='+TOKEN+'&otp_ok=1';
    } else if(d.locked) {
      document.getElementById('errMsg').textContent = 'Trop de tentatives. Reessayez dans 30 minutes.';
      inputs.forEach(i=>{i.disabled=true;});
      document.getElementById('btnVerify').disabled=true;
    } else if(d.expired) {
      document.getElementById('errMsg').textContent = 'Code expire. Cliquez sur Renvoyer le code.';
    } else {
      const left = d.attempts_left !== undefined ? d.attempts_left : '';
      document.getElementById('errMsg').textContent = 'Code incorrect.' + (left ? ' ' + left + ' tentative(s) restante(s).' : '');
      inputs.forEach(i=>{i.value='';i.classList.remove('filled');});
      inputs[0].focus();
      checkReady();
    }
  }).catch(() => {
    document.getElementById('btnVerify').style.display='block';
    document.getElementById('loading').style.display='none';
    document.getElementById('errMsg').textContent = 'Erreur reseau. Reessayez.';
  });
}

function resend() {
  document.getElementById('btnResend').disabled=true;
  document.getElementById('errMsg').textContent='';
  fetch('/esign/'+DOC_ID+'/otp/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({token: TOKEN})
  }).then(r=>r.json()).then(d => {
    if(d.success) {
      expireAt = Date.now() + (d.expires_in || 600) * 1000;
      startResendCooldown();
      inputs.forEach(i=>{i.value='';i.classList.remove('filled');i.disabled=false;});
      inputs[0].focus();
      document.getElementById('btnVerify').disabled=true;
    }
  });
}

function startResendCooldown() {
  resendCooldown = 60;
  clearInterval(resendInterval);
  resendInterval = setInterval(() => {
    resendCooldown--;
    const el = document.getElementById('resendTimer');
    if(resendCooldown > 0) {
      el.textContent = '(disponible dans '+resendCooldown+'s)';
    } else {
      el.textContent='';
      document.getElementById('btnResend').disabled=false;
      clearInterval(resendInterval);
    }
  }, 1000);
}
startResendCooldown();

function updateExpTimer() {
  const remaining = Math.max(0, Math.floor((expireAt - Date.now()) / 1000));
  const m = Math.floor(remaining / 60), s = remaining % 60;
  document.getElementById('expTimer').textContent =
    remaining > 0 ? 'Le code expire dans ' + m + ':' + String(s).padStart(2,'0') : 'Code expire.';
  if(remaining === 0) clearInterval(expTimerInterval);
}
const expTimerInterval = setInterval(updateExpTimer, 1000);
updateExpTimer();
</script>
</body>
</html>"""


def _mask_email(email: str) -> str:
    """j***@gmail.com style masking."""
    if "@" not in email:
        return "****"
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"{local}***@{domain}"
    return f"{local[0]}***@{domain}"


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
        "cb1": "Je certifie avoir pris connaissance des conditions d'inscription et les accepter.",
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
        "legal_label": "J'accepte que cette signature soit ma repr\u00e9sentation l\u00e9gale",
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

    # Detect school mode: if the token belongs to a school_admin signer,
    # inject student paraphes as read-only overlays and reduce required fields.
    school_mode = False
    student_paraphes: dict = {}
    if token:
        signer = db.get_signer_by_token(token)
        if signer and signer.get("role") == "school_admin":
            school_mode = True
            for s in db.get_signers_by_document(doc["id"]):
                if s.get("role") == "student" and s.get("paraphes"):
                    try:
                        raw = s["paraphes"] if isinstance(s["paraphes"], dict) else _json.loads(s["paraphes"])
                        student_paraphes = raw
                    except Exception:
                        pass

    config = _json.dumps({
        "doc_id": doc["id"],
        "token": token,
        "total_pages": total_pages,
        "signer_name": doc.get("signer_name", ""),
        "lang": lang,
        "labels": labels,
        "school_mode": school_mode,
        "student_paraphes": student_paraphes,
    }, ensure_ascii=False)
    page = _SIGNING_PAGE_TEMPLATE
    page = page.replace("__CONFIG_JSON__", config)
    page = page.replace("__LANG__", lang)
    return page



# ---------------------------------------------------------------------------
# eSign V2 — HTTP handlers (REST endpoints, not MCP tools)
# ---------------------------------------------------------------------------

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
        "status": "student_signing",
        "signer_email": signer_email,
        "signers": signer_responses,
    })



async def esign_otp_send(request: Request) -> JSONResponse:
    """POST /esign/{document_id}/otp/send — generate and send OTP."""
    import secrets as _sec
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    token = body.get("token", "")
    signer = db.get_signer_by_token(token) if token else None
    if not signer:
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    # Check lock status
    lock = db.is_otp_locked(doc_id, signer["id"])
    if lock["locked"]:
        return JSONResponse({"error": "Too many attempts. Try again later.",
                             "locked_until": lock["locked_until"]}, status_code=429)

    code = str(_sec.randbelow(900000) + 100000)
    otp = db.create_otp(doc_id, signer["id"], code)
    db.log_esign_audit(doc_id, "otp_sent", {"signer_id": signer["id"]}, signer_id=signer["id"])

    def _send():
        try:
            from tools.esign import _send_otp_email
            _send_otp_email(signer.get("signer_name", ""), signer.get("signer_email", ""), code)
        except Exception as e:
            db.log_esign_audit(doc_id, "otp_send_error", {"error": str(e)})

    threading.Thread(target=_send, daemon=True).start()
    return JSONResponse({"success": True, "expires_in": 600})


async def esign_otp_verify(request: Request) -> JSONResponse:
    """POST /esign/{document_id}/otp/verify — verify OTP code."""
    doc_id = request.path_params["document_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    code = str(body.get("code", "")).strip()
    token = body.get("token", "")
    signer = db.get_signer_by_token(token) if token else None
    if not signer:
        return JSONResponse({"verified": False, "error": "Invalid token"}, status_code=401)

    # Check lock
    lock = db.is_otp_locked(doc_id, signer["id"])
    if lock["locked"]:
        db.log_esign_audit(doc_id, "otp_locked", {"signer_id": signer["id"]}, signer_id=signer["id"])
        return JSONResponse({"verified": False, "locked": True,
                             "locked_until": lock["locked_until"]})

    otp = db.get_active_otp(doc_id, signer["id"])
    if not otp:
        # Check if expired
        return JSONResponse({"verified": False, "expired": True})

    if otp["code"] != code:
        result = db.increment_otp_attempts(otp["id"], otp["attempts"])
        db.log_esign_audit(doc_id, "otp_failed",
                           {"attempts": result["attempts"], "locked": result["locked"]},
                           signer_id=signer["id"])
        if result["locked"]:
            return JSONResponse({"verified": False, "locked": True,
                                 "locked_until": result["locked_until"]})
        return JSONResponse({"verified": False,
                             "attempts_left": max(0, 5 - result["attempts"])})

    # Correct code
    db.verify_otp(otp["id"], doc_id, signer["id"])
    db.log_esign_audit(doc_id, "otp_verified", {}, signer_id=signer["id"])
    return JSONResponse({"verified": True})


async def esign_verify(request: Request) -> JSONResponse:
    """GET /esign/{document_id}/verify — verify PDF integrity."""
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)

    signers_info = db.get_signers_by_document(doc_id)
    signed_pdf = doc.get("signed_pdf_path", "")

    try:
        from tools.esign import verify_signed_pdf
        integrity = verify_signed_pdf(signed_pdf) if signed_pdf else {"integrity": "unsigned"}
    except Exception as e:
        integrity = {"integrity": "unknown", "error": str(e)}

    # Load cert info
    cert_info = {}
    try:
        import subprocess as _sp
        r = _sp.run(
            ["openssl", "x509", "-noout", "-subject", "-dates",
             "-in", "/opt/clawshow-data/certs/esign-cert.pem"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            cert_info = {"raw": " | ".join(lines)}
    except Exception:
        pass

    return JSONResponse({
        "document_id": doc_id,
        "status": doc.get("status"),
        "integrity": integrity.get("integrity", "unknown"),
        "signed_by": integrity.get("signed_by") or "ClawShow eSign Platform",
        "signed_at": integrity.get("signed_at"),
        "certificate": {
            "issuer": "ClawShow eSign",
            "algorithm": "SHA-256 with RSA",
            **cert_info,
        },
        "signers": [
            {
                "name": s.get("signer_name"),
                "role": s.get("role"),
                "email": s.get("signer_email"),
                "signed_at": s.get("signed_at"),
                "otp_verified": bool(s.get("otp_verified_at")),
            }
            for s in signers_info
        ],
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

    # If no token: redirect to token-bearing URL (creates signer for V1 docs)
    if not token:
        from starlette.responses import RedirectResponse as _Redir
        import uuid as _uuid
        signers_list = db.get_signers_by_document(doc_id)
        if signers_list:
            # Redirect to first pending/signing signer's token
            first_s = next((s for s in signers_list if s.get("status") in ("pending", "signing")), signers_list[0])
            return _Redir(f"/esign/{doc_id}?token={first_s['token']}", status_code=302)
        else:
            # V1 doc with no signer records — create one and redirect
            new_token = str(_uuid.uuid4())
            db.create_esign_signer(
                document_id=doc_id, role="student",
                signer_name=doc.get("signer_name", ""), signer_email=doc.get("signer_email", ""),
                signing_order=1, token=new_token,
            )
            with db.get_conn() as _c:
                _c.execute("UPDATE esign_documents SET signing_url=? WHERE id=?",
                           (f"{MCP_BASE_URL}/esign/{doc_id}?token={new_token}", doc_id))
            return _Redir(f"/esign/{doc_id}?token={new_token}", status_code=302)

    # Validate token and check OTP verification
    if token:
        signer = db.get_signer_by_token(token)
        if signer:
            db.mark_signer_viewed(signer["id"])
            db.log_esign_audit(doc_id, "viewed", {"signer_role": signer["role"]}, signer_id=signer["id"])

            # OTP gate: skip if otp_ok param present (just verified) or already verified
            otp_ok = request.query_params.get("otp_ok", "")
            already_verified = db.is_otp_verified(doc_id, signer["id"])

            if not already_verified and not otp_ok:
                # Send OTP if no pending one exists
                active_otp = db.get_active_otp(doc_id, signer["id"])
                if not active_otp:
                    import secrets as _sec
                    code = str(_sec.randbelow(900000) + 100000)
                    db.create_otp(doc_id, signer["id"], code)
                    db.log_esign_audit(doc_id, "otp_sent", {}, signer_id=signer["id"])
                    def _send_otp_bg(n=signer.get("signer_name",""), e=signer.get("signer_email",""), c=code):
                        try:
                            from tools.esign import _send_otp_email
                            _send_otp_email(n, e, c)
                        except Exception:
                            pass
                    threading.Thread(target=_send_otp_bg, daemon=True).start()

                masked = _mask_email(signer.get("signer_email", ""))
                active = db.get_active_otp(doc_id, signer["id"])
                expires_in = 600
                if active:
                    from datetime import datetime as _dt, timezone as _tz
                    try:
                        exp = _dt.fromisoformat(active["expires_at"].replace("Z", "+00:00"))
                        expires_in = max(0, int((exp - _dt.now(_tz.utc)).total_seconds()))
                    except Exception:
                        pass

                otp_page = _OTP_PAGE_TEMPLATE
                otp_page = otp_page.replace("__DOC_ID__", doc_id)
                otp_page = otp_page.replace("__TOKEN__", token)
                otp_page = otp_page.replace("__MASKED_EMAIL__", masked)
                otp_page = otp_page.replace("__EXPIRES_IN__", str(expires_in))
                return HTMLResponse(otp_page)

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
    """POST /esign/{document_id}/sign — role-aware 3-path handler (AES V2)."""
    import base64 as _b64
    import json as _json
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
    paraphes_raw = body.get("paraphes", {})

    if not sig_data_url or not sig_data_url.startswith("data:image/png;base64,"):
        return JSONResponse({"error": "Missing or invalid signature_png"}, status_code=400)

    sig_bytes = _b64.b64decode(sig_data_url.split(",", 1)[1])
    lu_bytes = b""
    if lu_data_url and lu_data_url.startswith("data:image/png;base64,"):
        lu_bytes = _b64.b64decode(lu_data_url.split(",", 1)[1])

    paraphes_bytes: dict = {}
    for pg_str, data_url in paraphes_raw.items():
        if data_url and data_url.startswith("data:image/png;base64,"):
            try:
                paraphes_bytes[int(pg_str)] = _b64.b64decode(data_url.split(",", 1)[1])
            except Exception:
                pass

    signer_ip = request.client.host if request.client else "unknown"
    signed_at = datetime.now(timezone.utc).isoformat()
    namespace = doc["namespace"]
    original_pdf = doc.get("original_pdf_path", "")

    if not original_pdf or not Path(original_pdf).exists():
        return JSONResponse({"error": "Source PDF not found"}, status_code=500)

    # Determine signer role
    signer = None
    signer_role = "student"
    if token:
        signer = db.get_signer_by_token(token)
        if signer:
            signer_role = signer.get("role", "student")

    def _complete_with_digital_sig(unsigned_pdf: str, doc_id: str, signers_info: list) -> str:
        """Apply pyhanko digital signature after visual overlay."""
        try:
            from tools.esign import digitally_sign_pdf
            ds_pdf = unsigned_pdf.replace("-unsigned.pdf", "-signed.pdf")
            result = digitally_sign_pdf(unsigned_pdf, ds_pdf, doc_id, signers_info)
            return result  # returns ds_pdf on success, unsigned_pdf on failure
        except Exception:
            return unsigned_pdf

    # ── PATH A: School admin counter-signs ──────────────────────────────────
    if signer_role == "school_admin":
        if signer:
            db.update_signer_signed(
                signer_id=signer["id"], signer_ip=signer_ip,
                signature_png=sig_data_url, lu_approuve_png=lu_data_url,
                paraphes={}, city=city,
            )

        student_paraphes_bytes: dict = {}
        student_sig_bytes = b""
        student_lu_bytes = b""
        student_city = "Paris"
        student_name = doc.get("signer_name", "")
        student_signer_ip = signer_ip
        student_signed_at = signed_at

        all_signers = db.get_signers_by_document(doc_id)
        for s in all_signers:
            if s.get("role") == "student":
                student_name = s.get("signer_name") or student_name
                student_city = s.get("city") or student_city
                student_signer_ip = s.get("signer_ip") or student_signer_ip
                student_signed_at = s.get("signed_at") or student_signed_at
                raw_par = s.get("paraphes")
                if raw_par:
                    try:
                        par_dict = raw_par if isinstance(raw_par, dict) else _json.loads(raw_par)
                        for pg_str, data_url in par_dict.items():
                            if data_url and data_url.startswith("data:image/png;base64,"):
                                student_paraphes_bytes[int(pg_str)] = _b64.b64decode(data_url.split(",", 1)[1])
                    except Exception:
                        pass
                raw_sig = s.get("signature_png")
                if raw_sig and raw_sig.startswith("data:image/png;base64,"):
                    student_sig_bytes = _b64.b64decode(raw_sig.split(",", 1)[1])
                raw_lu = s.get("lu_approuve_png")
                if raw_lu and raw_lu.startswith("data:image/png;base64,"):
                    student_lu_bytes = _b64.b64decode(raw_lu.split(",", 1)[1])
                break

        school_signer_name = signer.get("signer_name") if signer else ""
        unsigned_pdf = str(ESIGN_DATA_DIR / namespace / f"{doc_id}-unsigned.pdf")
        try:
            from tools.esign import _overlay_signatures_pdf
            _overlay_signatures_pdf(
                original_pdf, unsigned_pdf,
                paraphes=student_paraphes_bytes,
                final_sig={
                    "sig_bytes": student_sig_bytes, "lu_bytes": student_lu_bytes,
                    "city": student_city, "signer_name": student_name,
                    "signed_at": student_signed_at, "signer_ip": student_signer_ip,
                    "doc_id": doc_id,
                },
                school_sig={
                    "sig_bytes": sig_bytes, "city": city,
                    "signer_name": school_signer_name, "signed_at": signed_at,
                },
            )
        except Exception as e:
            return JSONResponse({"error": f"PDF overlay failed: {e}"}, status_code=500)

        # Apply digital signature (AES)
        signed_pdf = _complete_with_digital_sig(unsigned_pdf, doc_id, all_signers)

        db.complete_esign_document(doc_id, signed_pdf, signer_ip, city=city, lu_approuve="lu et approuve")
        db.log_esign_audit(doc_id, "school_signed", {
            "city": city, "signer_ip": signer_ip, "school_signer": school_signer_name,
        }, signer_id=signer["id"] if signer else None)

        signed_pdf_url = f"{MCP_BASE_URL}/esign/{doc_id}/signed.pdf"
        student_email = ""
        school_email = signer.get("signer_email", "") if signer else ""
        for s in all_signers:
            if s.get("role") == "student":
                student_email = s.get("signer_email", "")
                break

        def _send_completion_emails():
            try:
                from tools.esign import _send_completion_email
                if student_email:
                    _send_completion_email(student_name, student_email, doc_id, signed_pdf_url)
                if school_email and school_email != student_email:
                    _send_completion_email(school_signer_name, school_email, doc_id, signed_pdf_url)
            except Exception as exc:
                db.log_esign_audit(doc_id, "completion_email_error", {"error": str(exc)})

        threading.Thread(target=_send_completion_emails, daemon=True).start()

        callback_url = doc.get("callback_url", "")
        if callback_url:
            def _fire_completed():
                import requests as _r
                payload = {"event": "document.completed", "provider": "clawshow_esign",
                           "document_id": doc_id, "reference_id": doc.get("reference_id", ""),
                           "namespace": namespace, "status": "completed",
                           "signed_pdf_url": signed_pdf_url,
                           "audit_url": f"{MCP_BASE_URL}/esign/{doc_id}/audit",
                           "completed_at": signed_at}
                hdrs = {"Content-Type": "application/json",
                        "X-ClawShow-Event": "document.completed",
                        "X-ClawShow-Document-Id": doc_id, "X-ClawShow-Timestamp": signed_at}
                for attempt in range(3):
                    try:
                        resp = _r.post(callback_url, json=payload, headers=hdrs, timeout=10)
                        if resp.status_code == 200:
                            return
                    except Exception:
                        pass
                    import time; time.sleep(2 ** attempt)
            threading.Thread(target=_fire_completed, daemon=True).start()

        return JSONResponse({
            "success": True, "document_id": doc_id,
            "signed_pdf_url": signed_pdf_url, "signed_at": signed_at,
        })

    # ── Check for school_admin co-signer ────────────────────────────────────
    all_signers = db.get_signers_by_document(doc_id)
    has_school_admin = any(s.get("role") == "school_admin" for s in all_signers)

    # ── PATH B: Student signs, school admin exists ───────────────────────────
    if has_school_admin:
        if signer:
            db.update_signer_signed(
                signer_id=signer["id"], signer_ip=signer_ip,
                signature_png=sig_data_url, lu_approuve_png=lu_data_url,
                paraphes={k: v for k, v in paraphes_raw.items()}, city=city,
            )

        db.update_document_status(doc_id, "student_signed")
        db.log_esign_audit(doc_id, "student_signed", {
            "city": city, "pages_paraphed": list(paraphes_bytes.keys()), "signer_ip": signer_ip,
        }, signer_id=signer["id"] if signer else None)

        school_signer = next((s for s in all_signers if s.get("role") == "school_admin"), None)
        school_signing_url = ""
        if school_signer and school_signer.get("token"):
            school_signing_url = f"{MCP_BASE_URL}/esign/{doc_id}?token={school_signer['token']}"

        def _notify_school():
            try:
                from tools.esign import _send_school_notification_email
                school_email = school_signer.get("signer_email", "") if school_signer else ""
                school_name = school_signer.get("signer_name", "Administration") if school_signer else "Administration"
                student_name_val = signer.get("signer_name", doc.get("signer_name", "")) if signer else doc.get("signer_name", "")
                if school_email:
                    _send_school_notification_email(student_name_val, school_email, school_name,
                                                    school_signing_url, doc_id)
            except Exception as exc:
                db.log_esign_audit(doc_id, "school_notification_error", {"error": str(exc)})

        threading.Thread(target=_notify_school, daemon=True).start()

        callback_url = doc.get("callback_url", "")
        if callback_url:
            student_name_wh = signer.get("signer_name", doc.get("signer_name", "")) if signer else doc.get("signer_name", "")
            def _fire_student_signed():
                import requests as _r
                payload = {"event": "signer.signed", "provider": "clawshow_esign",
                           "document_id": doc_id, "reference_id": doc.get("reference_id", ""),
                           "namespace": namespace, "status": "student_signed",
                           "signer": {"role": "student", "name": student_name_wh,
                                      "email": signer.get("signer_email", "") if signer else "",
                                      "signed_at": signed_at, "ip": signer_ip},
                           "school_signing_url": school_signing_url,
                           "audit_url": f"{MCP_BASE_URL}/esign/{doc_id}/audit"}
                hdrs = {"Content-Type": "application/json", "X-ClawShow-Event": "signer.signed",
                        "X-ClawShow-Document-Id": doc_id, "X-ClawShow-Timestamp": signed_at}
                for attempt in range(3):
                    try:
                        resp = _r.post(callback_url, json=payload, headers=hdrs, timeout=10)
                        if resp.status_code == 200:
                            return
                    except Exception:
                        pass
                    import time; time.sleep(2 ** attempt)
            threading.Thread(target=_fire_student_signed, daemon=True).start()

        return JSONResponse({
            "success": True, "document_id": doc_id, "status": "student_signed",
            "school_signing_url": school_signing_url, "signed_at": signed_at,
        })

    # ── PATH C: Single-signer — generate PDF and complete ───────────────────
    unsigned_pdf = str(ESIGN_DATA_DIR / namespace / f"{doc_id}-unsigned.pdf")
    try:
        from tools.esign import _overlay_signatures_pdf
        _overlay_signatures_pdf(
            original_pdf, unsigned_pdf, paraphes=paraphes_bytes,
            final_sig={
                "sig_bytes": sig_bytes, "lu_bytes": lu_bytes, "city": city,
                "signer_name": doc["signer_name"], "signed_at": signed_at,
                "signer_ip": signer_ip, "doc_id": doc_id,
            },
        )
    except Exception as e:
        return JSONResponse({"error": f"PDF signing failed: {e}"}, status_code=500)

    all_signers_c = db.get_signers_by_document(doc_id)
    signed_pdf = _complete_with_digital_sig(unsigned_pdf, doc_id, all_signers_c)

    if signer:
        db.update_signer_signed(
            signer_id=signer["id"], signer_ip=signer_ip,
            signature_png=sig_data_url, lu_approuve_png=lu_data_url,
            paraphes={k: v for k, v in paraphes_raw.items()}, city=city,
        )

    db.complete_esign_document(doc_id, signed_pdf, signer_ip, city=city, lu_approuve="lu et approuve")
    db.log_esign_audit(doc_id, "signed", {
        "city": city, "pages_paraphed": list(paraphes_bytes.keys()), "signer_ip": signer_ip,
    }, signer_id=signer["id"] if signer else None)

    signed_pdf_url = f"{MCP_BASE_URL}/esign/{doc_id}/signed.pdf"

    callback_url = doc.get("callback_url", "")
    if callback_url:
        def _fire_callback():
            import requests as _r
            payload = {"event": "document.completed", "provider": "clawshow_esign",
                       "document_id": doc_id, "reference_id": doc.get("reference_id", ""),
                       "namespace": namespace, "status": "completed",
                       "signed_pdf_url": signed_pdf_url,
                       "audit_url": f"{MCP_BASE_URL}/esign/{doc_id}/audit",
                       "completed_at": signed_at}
            hdrs = {"Content-Type": "application/json", "X-ClawShow-Event": "document.completed",
                    "X-ClawShow-Document-Id": doc_id, "X-ClawShow-Timestamp": signed_at}
            for attempt in range(3):
                try:
                    resp = _r.post(callback_url, json=payload, headers=hdrs, timeout=10)
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                import time; time.sleep(2 ** attempt)
        threading.Thread(target=_fire_callback, daemon=True).start()

    return JSONResponse({
        "success": True, "document_id": doc_id,
        "signed_pdf_url": signed_pdf_url, "signed_at": signed_at,
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

    signer_ip = request.client.host if request.client else "unknown"

    if token:
        signer = db.get_signer_by_token(token)
        if signer:
            db.update_signer_status(signer["id"], "declined", reason)

    result = db.decline_esign_document(doc_id, signer_ip, reason)
    db.log_esign_audit(doc_id, "declined", {"reason": reason, "signer_ip": signer_ip})
    return JSONResponse(result)


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
# Combined ASGI app (MCP SSE + /stats + /webhook/stripe + /reports + /api)
# ---------------------------------------------------------------------------

def _build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/stats", stats, methods=["GET"]),
            Route("/webhook/stripe", stripe_webhook, methods=["POST"]),
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
            Route("/api/order/{id:int}/picked", api_order_picked, methods=["PATCH"]),
            Route("/api/payment/create", api_payment_create, methods=["POST"]),
            Route("/api/payment/verify", api_payment_verify, methods=["GET"]),
            Route("/esign/create", esign_create, methods=["POST"]),
            Route("/esign/{document_id}/page/{page_num}.png", esign_page_image, methods=["GET"]),
            Route("/esign/{document_id}/sign", esign_submit_signature, methods=["POST"]),
            Route("/esign/{document_id}/decline", esign_decline, methods=["POST"]),
            Route("/esign/{document_id}/status", esign_status, methods=["GET"]),
            Route("/esign/{document_id}/otp/send", esign_otp_send, methods=["POST"]),
            Route("/esign/{document_id}/otp/verify", esign_otp_verify, methods=["POST"]),
            Route("/esign/{document_id}/verify", esign_verify, methods=["GET"]),
            Route("/esign/{document_id}/audit", esign_audit, methods=["GET"]),
            Route("/esign/{document_id}/preview.pdf", esign_preview_pdf, methods=["GET"]),
            Route("/esign/{document_id}/signed.pdf", esign_signed_pdf, methods=["GET"]),
            Route("/esign/{document_id}", esign_signing_page, methods=["GET"]),
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
                    "http://localhost:5173",
                    "http://localhost:3000",
                ],
                allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
                allow_headers=["*"],
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
        app = _build_app()
        uvicorn.run(app, host=_host, port=_port)
