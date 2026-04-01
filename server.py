"""
ClawShow MCP Server
-------------------
Hosts: mcp.clawshow.ai/sse

Transport:
  - SSE (production on Railway, also local via http)
  - stdio (local Claude Desktop testing)

Endpoints (SSE mode):
  GET /sse      — MCP SSE stream
  GET /stats    — Tool call counts (JSON, CORS-enabled)

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

_register_rental_website(mcp, _record_call)
_register_finance_extract(mcp, _record_call)

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
# Combined ASGI app (MCP SSE + /stats)
# ---------------------------------------------------------------------------

def _build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/stats", stats, methods=["GET"]),
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
                allow_methods=["GET", "OPTIONS"],
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
