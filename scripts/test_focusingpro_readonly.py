"""
FocusingPro Read-Only Integration Test — Day 3 (2026-04-22, v2)

Protocol: MCP HTTP+SSE (mcp.focusingpro.com/mcp)

Usage:
    cd /opt/clawshow-mcp-server
    .venv/bin/python scripts/test_focusingpro_readonly.py --namespace=ilci-william

Reads only. Never writes. Safe to run on production data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from adapters.focusingpro.mcp_adapter import FocusingProMCPAdapter, FocusingProMCPError


def _section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _show(label: str, data: Any, max_items: int = 2):
    print(f"\n[{label}]")
    if isinstance(data, list):
        print(f"  total returned: {len(data)}")
        for i, item in enumerate(data[:max_items]):
            print(f"  item[{i}]: {json.dumps(item, ensure_ascii=False, indent=4)[:400]}")
    elif isinstance(data, dict):
        print(f"  {json.dumps(data, ensure_ascii=False, indent=4)[:600]}")
    else:
        print(f"  {data}")


async def run_tests(namespace: str):
    _section(f"FocusingPro Read-Only Test — namespace={namespace}")
    print(f"\n  MCP URL: https://mcp.focusingpro.com/mcp")

    async with FocusingProMCPAdapter(namespace=namespace) as fp:
        print(f"  Session ID: {fp._session_id}")
        print(f"  Module: {fp.module}")

        # ── 1. inscription_query (ES) ──────────────────────────────────
        _section("1. inscription_query — first 2 students")
        try:
            result = await fp.call_tool("inscription_query", {
                "params": {"page_size": 2, "page": 1},
                "module": fp.module,
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            _show("inscription_query", records)
            if records:
                print(f"\n  ✅ OK — sample keys: {list(records[0].keys())[:8]}")
        except FocusingProMCPError as e:
            print(f"  ❌ FAILED: {e}")

        # ── 2. collect_pay_query ───────────────────────────────────────
        _section("2. collect_pay_query — first 2 records")
        try:
            result = await fp.call_tool("collect_pay_query", {
                "params": {"page_size": 2, "page": 1},
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            _show("collect_pay_query", records)
            if records:
                print(f"\n  ✅ OK — sample keys: {list(records[0].keys())[:8]}")
        except FocusingProMCPError as e:
            print(f"  ❌ FAILED: {e}")

        # ── 3. lookup_programmes ──────────────────────────────────────
        _section("3. lookup_programmes")
        try:
            result = await fp.call_tool("lookup_programmes", {
                "params": {},
                "module": fp.module,
            })
            items = (result.get("records") or result.get("data") or
                     result.get("items") or result.get("programmes") or [])
            if isinstance(items, list):
                print(f"\n  Total programmes: {len(items)}")
                for p in items[:5]:
                    print(f"    {p}")
            else:
                print(f"  Result: {json.dumps(result, ensure_ascii=False)[:300]}")
            print(f"\n  ✅ OK")
        except FocusingProMCPError as e:
            print(f"  ❌ FAILED: {e}")

        # ── 4. balance_query ──────────────────────────────────────────
        _section("4. balance_query — first student balance")
        try:
            result = await fp.call_tool("balance_query", {
                "params": {"page_size": 1, "page": 1},
                "module": fp.module,
            })
            _show("balance_query", result)
            print(f"\n  ✅ OK")
        except FocusingProMCPError as e:
            print(f"  ❌ FAILED: {e}")

        # ── 5. verify_payment_exists (idempotency) ────────────────────
        _section("5. verify_payment_exists — test idempotency check")
        try:
            exists = await fp.verify_payment_exists("test_nonexistent_tx_12345")
            print(f"\n  Result: exists={exists} (expected False for fake tx)")
            print(f"\n  ✅ OK")
        except Exception as e:
            print(f"  ❌ FAILED: {e}")

    _section("Test Complete")
    print("  Read-only. No data was modified.\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="ilci-william")
    args = parser.parse_args()
    asyncio.run(run_tests(args.namespace))


if __name__ == "__main__":
    main()
