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

_register_rental_website(mcp, _record_call)
_register_finance_extract(mcp, _record_call)
_register_stripe_payment(mcp, _record_call)
_register_notification(mcp, _record_call)
_register_orders(mcp, _record_call)

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
        session = event["data"]["object"]
        PAYMENTS_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "session_id":     session.get("id"),
            "amount":         session.get("amount_total"),
            "currency":       session.get("currency"),
            "customer_email": session.get("customer_details", {}).get("email"),
            "metadata":       session.get("metadata", {}),
            "completed_at":   datetime.now(timezone.utc).isoformat(),
        }
        out = PAYMENTS_DIR / f"{session.get('id', 'unknown')}.json"
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

        # Auto-mark linked order as paid
        order_id = session.get("metadata", {}).get("order_id")
        if order_id:
            from tools.orders import webhook_mark_paid
            webhook_mark_paid(order_id)

    return JSONResponse({"received": True})


# ---------------------------------------------------------------------------
# Combined ASGI app (MCP SSE + /stats + /webhook/stripe)
# ---------------------------------------------------------------------------

def _build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/stats", stats, methods=["GET"]),
            Route("/webhook/stripe", stripe_webhook, methods=["POST"]),
            Mount("/", app=mcp.sse_app()),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=[
                    "https://clawshow.ai",
                    "https://www.clawshow.ai",
                    "https://mcp.clawshow.ai",
                    "http://localhost:5173",   # clawshow-site dev
                    "http://localhost:3000",
                ],
                allow_methods=["GET", "POST", "OPTIONS"],
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
