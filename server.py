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
from tools.stripe_payment import register as _register_stripe_payment
from tools.notification import register as _register_notification
from tools.orders import register as _register_orders
from tools.business_page import register as _register_business_page
from tools.inventory import register as _register_inventory
from tools.report import register as _register_report
from tools.bookings import register as _register_bookings

_register_rental_website(mcp, _record_call)
_register_finance_extract(mcp, _record_call)
_register_stripe_payment(mcp, _record_call)
_register_notification(mcp, _record_call)
_register_orders(mcp, _record_call)
_register_business_page(mcp, _record_call)
_register_inventory(mcp, _record_call)
_register_report(mcp, _record_call)
_register_bookings(mcp, _record_call)

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
