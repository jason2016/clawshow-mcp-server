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
# eSign endpoints
# ---------------------------------------------------------------------------

ESIGN_DATA_DIR = Path("/opt/clawshow-data/esign")
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai")

_ESIGN_LABELS = {
    "fr": {
        "title": "Signer le document",
        "greeting": "Bonjour",
        "doc_label": "Veuillez lire et signer le document ci-dessous.",
        "confirm_conditions_label": "Confirmations requises",
        "cb1_label": "Je certifie avoir pris connaissance des conditions d'inscription et les accepter.",
        "cb2_label": "J'accepte les échanges par email en remplacement du courrier postal.",
        "city_label": "Fait à (ville) :",
        "lu_label": "Écrivez « lu et approuvé » :",
        "sign_label": "Votre signature :",
        "canvas_hint": "Signez avec votre doigt (mobile) ou votre souris",
        "clear_label": "Effacer",
        "submit_label": "✅ Confirmer la signature",
        "decline_label": "❌ Refuser de signer",
        "sending_label": "Envoi en cours...",
        "warn_checkboxes": "Veuillez cocher les deux cases avant de signer.",
        "warn_lu": "Veuillez écrire \"lu et approuvé\" avant de confirmer.",
        "warn_sig": "Veuillez signer avant de confirmer.",
        "success_msg": "Document signé avec succès !",
        "download_label": "Télécharger le document signé",
        "declined_msg": "Vous avez refusé de signer ce document.",
        "decline_prompt": "Motif du refus (optionnel) :",
    },
    "en": {
        "title": "Sign Document",
        "greeting": "Hello",
        "doc_label": "Please read and sign the document below.",
        "confirm_conditions_label": "Required confirmations",
        "cb1_label": "I certify that I have read the terms and conditions and accept them.",
        "cb2_label": "I accept email communications instead of postal mail.",
        "city_label": "Signed at (city):",
        "lu_label": "Write \"read and approved\":",
        "sign_label": "Your signature:",
        "canvas_hint": "Sign with your finger (mobile) or mouse",
        "clear_label": "Clear",
        "submit_label": "✅ Confirm signature",
        "decline_label": "❌ Decline to sign",
        "sending_label": "Submitting...",
        "warn_checkboxes": "Please check both boxes before signing.",
        "warn_lu": "Please write \"read and approved\" before confirming.",
        "warn_sig": "Please sign before confirming.",
        "success_msg": "Document signed successfully!",
        "download_label": "Download signed document",
        "declined_msg": "You have declined to sign this document.",
        "decline_prompt": "Reason for declining (optional):",
    },
    "zh": {
        "title": "签署文件",
        "greeting": "您好",
        "doc_label": "请阅读以下文件并签名。",
        "confirm_conditions_label": "必要确认",
        "cb1_label": "我确认已阅读并接受条款和条件。",
        "cb2_label": "我接受通过电子邮件替代邮政通信。",
        "city_label": "签署地点（城市）：",
        "lu_label": "请手写「已阅读并同意」：",
        "sign_label": "您的签名：",
        "canvas_hint": "请用手指（手机）或鼠标签名",
        "clear_label": "清除",
        "submit_label": "✅ 确认签名",
        "decline_label": "❌ 拒绝签署",
        "sending_label": "提交中...",
        "warn_checkboxes": "请勾选两个复选框后再签名。",
        "warn_lu": "请先手写确认文字再提交。",
        "warn_sig": "请先签名再确认。",
        "success_msg": "文件签署成功！",
        "download_label": "下载已签署文件",
        "declined_msg": "您已拒绝签署此文件。",
        "decline_prompt": "拒绝原因（可选）：",
    },
}

_ESIGN_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;background:#f0f2f5;color:#333;font-size:14px}}
.header{{background:#1a1a2e;color:white;padding:14px 20px;text-align:center}}
.header h1{{font-size:17px;font-weight:600}}
.container{{max-width:760px;margin:16px auto;padding:0 12px 48px}}
.card{{background:white;border-radius:10px;padding:20px;margin-bottom:14px;box-shadow:0 1px 6px rgba(0,0,0,.09)}}
.contract-wrap{{max-height:460px;overflow-y:auto;border:1px solid #e0e0e0;border-radius:6px;padding:16px;font-size:10.5pt;line-height:1.65;background:#fefefe}}
.contract-wrap h1{{font-size:14pt;text-align:center;margin-bottom:6px}}
.contract-wrap h3{{font-size:10.5pt;text-transform:uppercase;margin:14px 0 4px;font-weight:700}}
.section-label{{font-size:14px;font-weight:700;color:#222;margin:0 0 10px}}
canvas{{border:2px dashed #bbb;border-radius:6px;width:100%;height:110px;cursor:crosshair;touch-action:none;display:block;background:#f8f8f8}}
.hint{{font-size:11px;color:#aaa;margin:4px 0 8px}}
.btn-row{{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}}
.btn{{padding:10px 16px;border:none;border-radius:8px;font-size:14px;cursor:pointer;font-weight:600;transition:opacity .15s}}
.btn:active{{opacity:.8}}
.btn-clear{{background:#ebebeb;color:#555;font-size:12px;padding:7px 14px}}
.btn-submit{{background:#1a1a2e;color:white;flex:1;padding:14px;font-size:15px}}
.btn-submit:disabled{{opacity:.45;cursor:not-allowed}}
.btn-decline{{background:white;color:#c0392b;border:1.5px solid #c0392b;width:100%;padding:11px;margin-top:8px}}
.cb-group{{margin:8px 0}}
.cb-group label{{display:flex;align-items:flex-start;gap:10px;font-size:13.5px;cursor:pointer;line-height:1.5}}
.cb-group input[type=checkbox]{{margin-top:2px;min-width:18px;min-height:18px;cursor:pointer}}
.city-row label{{display:block;font-size:13px;font-weight:600;color:#555;margin:12px 0 5px}}
.city-row input{{width:100%;padding:9px 12px;border:1.5px solid #ddd;border-radius:7px;font-size:14px}}
.city-row input:focus{{outline:none;border-color:#1a1a2e}}
.pad-label{{font-size:13px;font-weight:600;color:#444;margin:12px 0 5px}}
.result-success{{text-align:center;padding:28px 20px;color:#27ae60}}
.result-success .check{{font-size:48px}}
.result-success p{{margin-top:10px;font-size:16px;font-weight:700}}
.result-success a{{color:#1a1a2e;text-decoration:underline;font-size:14px}}
.result-declined{{text-align:center;padding:28px 20px;color:#888}}
</style>
</head>
<body>
<div class="header"><h1>ClawShow eSign</h1></div>
<div class="container">
  <div class="card">
    <p style="font-size:12px;color:#999;margin-bottom:10px">{greeting} <b>{signer_name}</b> — {doc_label}</p>
    <div class="contract-wrap">{contract_html}</div>
  </div>
  <div class="card" id="sign-card">
    <div class="section-label">{confirm_conditions_label}</div>
    <div class="cb-group"><label><input type="checkbox" id="cb1"> {cb1_label}</label></div>
    <div class="cb-group"><label><input type="checkbox" id="cb2"> {cb2_label}</label></div>
    <div class="city-row">
      <label for="cityInp">{city_label}</label>
      <input type="text" id="cityInp" value="Paris" autocomplete="address-level2"/>
    </div>
    <div class="pad-label">{lu_label}</div>
    <canvas id="luPad"></canvas>
    <div class="hint">{canvas_hint}</div>
    <div class="btn-row"><button class="btn btn-clear" onclick="luPad.clear()">{clear_label}</button></div>
    <div class="pad-label" style="margin-top:16px">{sign_label}</div>
    <canvas id="sigPad"></canvas>
    <div class="hint">{canvas_hint}</div>
    <div class="btn-row"><button class="btn btn-clear" onclick="sigPad.clear()">{clear_label}</button></div>
    <div class="btn-row" style="margin-top:18px">
      <button class="btn btn-submit" id="submitBtn" onclick="doSubmit()">{submit_label}</button>
    </div>
    <button class="btn btn-decline" onclick="doDecline()">{decline_label}</button>
  </div>
  <div id="result"></div>
</div>
<script>
const DOC_ID="{doc_id}";
class Pad{{
  constructor(id){{
    this.el=document.getElementById(id);
    this.ctx=this.el.getContext('2d');
    this.drawing=false;this.empty=true;
    this._init();
    const obs=new ResizeObserver(()=>this._init());obs.observe(this.el);
    this.el.addEventListener('mousedown',e=>{{this.drawing=true;const[x,y]=this._xy(e);this.ctx.beginPath();this.ctx.moveTo(x,y);}});
    this.el.addEventListener('mousemove',e=>{{if(!this.drawing)return;const[x,y]=this._xy(e);this.ctx.lineTo(x,y);this.ctx.stroke();this.empty=false;}});
    this.el.addEventListener('mouseup',()=>this.drawing=false);
    this.el.addEventListener('mouseleave',()=>this.drawing=false);
    this.el.addEventListener('touchstart',e=>{{e.preventDefault();this.drawing=true;const[x,y]=this._xy(e.touches[0]);this.ctx.beginPath();this.ctx.moveTo(x,y);}},{{passive:false}});
    this.el.addEventListener('touchmove',e=>{{e.preventDefault();if(!this.drawing)return;const[x,y]=this._xy(e.touches[0]);this.ctx.lineTo(x,y);this.ctx.stroke();this.empty=false;}},{{passive:false}});
    this.el.addEventListener('touchend',()=>this.drawing=false);
  }}
  _init(){{
    const dpr=window.devicePixelRatio||1,w=this.el.offsetWidth||300,h=110;
    this.el.width=w*dpr;this.el.height=h*dpr;this.el.style.height=h+'px';
    this.ctx.scale(dpr,dpr);this.ctx.strokeStyle='#111';this.ctx.lineWidth=2;
    this.ctx.lineCap='round';this.ctx.lineJoin='round';this.empty=true;
  }}
  _xy(e){{const r=this.el.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top];}}
  clear(){{this.ctx.clearRect(0,0,this.el.width,this.el.height);this.empty=true;}}
  png(){{return this.el.toDataURL('image/png');}}
}}
const luPad=new Pad('luPad'),sigPad=new Pad('sigPad');

async function doSubmit(){{
  if(!document.getElementById('cb1').checked||!document.getElementById('cb2').checked){{alert('{warn_checkboxes}');return;}}
  if(luPad.empty){{alert('{warn_lu}');return;}}
  if(sigPad.empty){{alert('{warn_sig}');return;}}
  const btn=document.getElementById('submitBtn');
  btn.disabled=true;btn.textContent='{sending_label}';
  const city=document.getElementById('cityInp').value||'Paris';
  try{{
    const r=await fetch('/esign/'+DOC_ID+'/sign',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{signature_png:sigPad.png(),lu_approuve_png:luPad.png(),city:city,accept_conditions:true,accept_email:true}})}});
    const d=await r.json();
    if(d.success){{
      document.getElementById('sign-card').style.display='none';
      document.getElementById('result').innerHTML=
        '<div class="card result-success"><div class="check">✓</div><p>{success_msg}</p>'+
        '<p style="margin-top:12px"><a href="'+d.signed_pdf_url+'" target="_blank">{download_label}</a></p></div>';
    }}else{{btn.disabled=false;btn.textContent='{submit_label}';alert(d.error||'Erreur');}}
  }}catch(e){{btn.disabled=false;btn.textContent='{submit_label}';alert('Erreur réseau');}}
}}

function doDecline(){{
  const reason=prompt('{decline_prompt}')||'';
  fetch('/esign/'+DOC_ID+'/decline',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{reason:reason}})}})
  .then(()=>{{
    document.getElementById('sign-card').style.display='none';
    document.getElementById('result').innerHTML='<div class="card result-declined"><p style="font-size:22px">✗</p><p style="margin-top:8px">{declined_msg}</p></div>';
  }});
}}
</script>
</body>
</html>"""


def _extract_body_html(html: str) -> str:
    """Extract <body>…</body> content from an HTML document."""
    import re as _re
    m = _re.search(r"<body[^>]*>(.*)</body>", html, _re.DOTALL | _re.IGNORECASE)
    return m.group(1).strip() if m else html


async def esign_create(request: Request) -> JSONResponse:
    """POST /esign/create — create a signing request via REST (no MCP required)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    template = body.get("template", "enrollment_contract")
    signer_name = body.get("signer_name", "")
    signer_email = body.get("signer_email", "")
    fields = body.get("fields", {})
    namespace = body.get("namespace", "demo")
    reference_id = body.get("reference_id", "")
    callback_url = body.get("callback_url", "")
    send_email = body.get("send_email", False)
    language = body.get("language", "fr")

    if not signer_name:
        return JSONResponse({"error": "signer_name is required"}, status_code=400)

    from tools.esign import _next_doc_id, _generate_pdf, _send_signing_email
    import threading as _th

    doc_id = _next_doc_id(namespace)
    signing_url = f"{MCP_BASE_URL}/esign/{doc_id}"
    pdf_preview_url = f"{MCP_BASE_URL}/esign/{doc_id}/preview.pdf"

    try:
        html_path, pdf_path = _generate_pdf(doc_id, namespace, template, signer_name, fields)
    except Exception as e:
        return JSONResponse({"error": f"PDF generation failed: {e}"}, status_code=500)

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
    )

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
        "status": "pending",
        "signer_email": signer_email,
    })


async def esign_signing_page(request: Request):
    """GET /esign/{document_id} — serve the 2-canvas signing page."""
    from starlette.responses import HTMLResponse
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>Document not found or link expired.</h2>", status_code=404)
    if doc["status"] in ("completed", "signed"):
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>Ce document a déjà été signé. Merci !</h2>")
    if doc["status"] == "declined":
        return HTMLResponse("<h2 style='font-family:Arial;padding:40px'>Ce document a été refusé.</h2>")

    lang = doc.get("language", "fr")
    labels = _ESIGN_LABELS.get(lang, _ESIGN_LABELS["en"])

    # Load rendered contract HTML for inline display
    html_path = doc.get("rendered_html_path", "")
    contract_html = ""
    if html_path and Path(html_path).exists():
        contract_html = _extract_body_html(Path(html_path).read_text(encoding="utf-8"))
    else:
        # Fallback: just show a download link
        contract_html = f'<p><a href="{MCP_BASE_URL}/esign/{doc_id}/preview.pdf" target="_blank">Ouvrir le document PDF</a></p>'

    page = _ESIGN_PAGE_TEMPLATE.format(
        lang=lang,
        doc_id=doc_id,
        signer_name=doc["signer_name"],
        contract_html=contract_html,
        **labels,
    )
    return HTMLResponse(page)


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
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{doc_id}-signed.pdf"'})


async def esign_submit_signature(request: Request) -> JSONResponse:
    """POST /esign/{document_id}/sign — receive signature + lu_approuve PNGs, generate signed PDF."""
    import base64 as _b64
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    if doc["status"] in ("completed", "signed"):
        return JSONResponse({"error": "Already signed"}, status_code=409)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    sig_data_url = body.get("signature_png", "")
    lu_data_url = body.get("lu_approuve_png", "")
    city = body.get("city", "Paris") or "Paris"

    if not sig_data_url or not sig_data_url.startswith("data:image/png;base64,"):
        return JSONResponse({"error": "Missing or invalid signature_png"}, status_code=400)

    sig_bytes = _b64.b64decode(sig_data_url.split(",", 1)[1])
    lu_bytes = b""
    if lu_data_url and lu_data_url.startswith("data:image/png;base64,"):
        lu_bytes = _b64.b64decode(lu_data_url.split(",", 1)[1])

    signer_ip = request.client.host if request.client else "unknown"
    signed_at = datetime.now(timezone.utc).isoformat()

    rendered_html = doc.get("rendered_html_path", "")
    if not rendered_html or not Path(rendered_html).exists():
        # Fallback: try original PDF path (old-style documents without saved HTML)
        original_pdf = doc.get("original_pdf_path", "")
        if not original_pdf or not Path(original_pdf).exists():
            return JSONResponse({"error": "Source document not found"}, status_code=500)
        # Use reportlab overlay fallback
        namespace = doc["namespace"]
        signed_pdf = str(ESIGN_DATA_DIR / namespace / f"{doc_id}-signed.pdf")
        try:
            from tools.esign import _embed_signature_in_pdf as _old_embed
            _old_embed(original_pdf, signed_pdf, sig_bytes, doc["signer_name"], signed_at, signer_ip)
        except Exception as e:
            return JSONResponse({"error": f"PDF signing failed: {e}"}, status_code=500)
    else:
        namespace = doc["namespace"]
        signed_pdf = str(ESIGN_DATA_DIR / namespace / f"{doc_id}-signed.pdf")
        try:
            from tools.esign import _embed_signature_in_pdf
            _embed_signature_in_pdf(
                rendered_html, signed_pdf, sig_bytes,
                doc["signer_name"], signed_at, signer_ip,
                lu_approuve_png_bytes=lu_bytes,
                city=city,
            )
        except Exception as e:
            return JSONResponse({"error": f"PDF signing failed: {e}"}, status_code=500)

    lu_text = body.get("lu_approuve_text", "lu et approuvé")
    db.complete_esign_document(doc_id, signed_pdf, signer_ip, city=city, lu_approuve=lu_text)

    signed_pdf_url = f"{MCP_BASE_URL}/esign/{doc_id}/signed.pdf"

    callback_url = doc.get("callback_url", "")
    if callback_url:
        def _fire_callback():
            try:
                import requests as _r
                _r.post(callback_url, json={
                    "event": "esign.completed",
                    "document_id": doc_id,
                    "reference_id": doc.get("reference_id", ""),
                    "signed_pdf_url": signed_pdf_url,
                    "signed_at": signed_at,
                    "signer_name": doc["signer_name"],
                    "signer_ip": signer_ip,
                    "city": city,
                }, timeout=10)
            except Exception:
                pass
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
    if doc["status"] in ("completed", "signed", "declined"):
        return JSONResponse({"error": f"Document already {doc['status']}"}, status_code=409)

    try:
        body = await request.json()
        reason = body.get("reason", "")
    except Exception:
        reason = ""

    signer_ip = request.client.host if request.client else "unknown"
    result = db.decline_esign_document(doc_id, signer_ip, reason)
    return JSONResponse(result)


async def esign_status(request: Request) -> JSONResponse:
    """GET /esign/{document_id}/status — return current signing status."""
    doc_id = request.path_params["document_id"]
    doc = db.get_esign_document(doc_id)
    if not doc:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    return JSONResponse({
        "document_id": doc_id,
        "status": doc["status"],
        "signer_name": doc["signer_name"],
        "signer_email": doc["signer_email"],
        "signed_at": doc.get("signed_at"),
        "city": doc.get("city"),
        "signed_pdf_url": f"{MCP_BASE_URL}/esign/{doc_id}/signed.pdf" if doc["status"] in ("completed", "signed") else None,
        "created_at": doc.get("created_at"),
    })


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
            Route("/esign/{document_id}/sign", esign_submit_signature, methods=["POST"]),
            Route("/esign/{document_id}/decline", esign_decline, methods=["POST"]),
            Route("/esign/{document_id}/status", esign_status, methods=["GET"]),
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
