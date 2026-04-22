"""
FocusingPro Read-Only Integration Test — Day 3 (2026-04-22)

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

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

from adapters.focusingpro.mcp_adapter import FocusingProMCPAdapter, FocusingProMCPError, _build_mcp_url


def _print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _print_result(label: str, data: Any, max_items: int = 2):
    print(f"\n[{label}]")
    if isinstance(data, list):
        print(f"  total returned: {len(data)}")
        for i, item in enumerate(data[:max_items]):
            print(f"  item[{i}]: {json.dumps(item, ensure_ascii=False, indent=4)}")
    elif isinstance(data, dict):
        print(f"  {json.dumps(data, ensure_ascii=False, indent=4)}")
    else:
        print(f"  {data}")


async def run_tests(namespace: str):
    _print_section(f"FocusingPro Read-Only Test — namespace={namespace}")

    # 0. Show URL
    try:
        url = _build_mcp_url(namespace)
        print(f"\n✅ MCP URL: {url}")
    except ValueError as e:
        print(f"\n❌ URL build failed: {e}")
        sys.exit(1)

    async with FocusingProMCPAdapter(namespace=namespace) as fp:

        # ── 1. tools/list ─────────────────────────────────────────────
        _print_section("1. tools/list — all available tools")
        try:
            resp = await fp._client.post(fp.mcp_url, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {},
            })
            resp.raise_for_status()
            data = resp.json()
            tools = data.get("result", {}).get("tools", [])
            print(f"\n  Total tools: {len(tools)}")
            for t in tools:
                name = t.get("name", "?")
                desc = t.get("description", "")[:60]
                print(f"    - {name}: {desc}")
        except Exception as e:
            print(f"  ❌ tools/list failed: {e}")

        # ── 2. ES Module: inscription_query ───────────────────────────
        _print_section("2. ES Module — inscription_query (first 2 students)")
        try:
            result = await fp.call_tool("inscription_query", {
                "module": "ES",
                "page_size": 2,
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            _print_result("inscription_query (ES)", records)
            if records:
                print(f"\n  ✅ inscription_query ES OK — sample keys: {list(records[0].keys())}")
        except FocusingProMCPError as e:
            print(f"  ❌ inscription_query ES failed: {e}")

        # ── 3. LANG Module: inscription_query ─────────────────────────
        _print_section("3. LANG Module — inscription_query (first 2 students)")
        try:
            result = await fp.call_tool("inscription_query", {
                "module": "LANG",
                "page_size": 2,
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            _print_result("inscription_query (LANG)", records)
            if records:
                print(f"\n  ✅ inscription_query LANG OK — sample keys: {list(records[0].keys())}")
        except FocusingProMCPError as e:
            print(f"  ❌ inscription_query LANG failed: {e}")

        # ── 4. Collect&Pay: collect_pay_query ─────────────────────────
        _print_section("4. Collect&Pay — collect_pay_query (first 2 records)")
        try:
            result = await fp.call_tool("collect_pay_query", {
                "module": "ES",
                "page_size": 2,
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            _print_result("collect_pay_query", records)
            if records:
                print(f"\n  ✅ collect_pay_query OK — sample keys: {list(records[0].keys())}")
        except FocusingProMCPError as e:
            print(f"  ❌ collect_pay_query failed: {e}")

        # ── 5. payment_query ──────────────────────────────────────────
        _print_section("5. payment_query (first 2 records)")
        try:
            result = await fp.call_tool("payment_query", {
                "module": "ES",
                "page_size": 2,
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            _print_result("payment_query", records)
            if records:
                print(f"\n  ✅ payment_query OK — sample keys: {list(records[0].keys())}")
        except FocusingProMCPError as e:
            print(f"  ❌ payment_query failed: {e}")

        # ── 6. lookup_programmes ──────────────────────────────────────
        _print_section("6. lookup_programmes")
        try:
            result = await fp.call_tool("lookup_programmes", {
                "module": "ES",
            })
            items = result.get("records") or result.get("data") or result.get("items") or result.get("programmes") or []
            if isinstance(items, list):
                print(f"\n  Total programmes: {len(items)}")
                for p in items[:5]:
                    print(f"    {p}")
            else:
                print(f"  Result: {json.dumps(result, ensure_ascii=False)[:300]}")
            print(f"\n  ✅ lookup_programmes OK")
        except FocusingProMCPError as e:
            print(f"  ❌ lookup_programmes failed: {e}")

        # ── 7. balance_query ──────────────────────────────────────────
        _print_section("7. balance_query (first student balance)")
        try:
            result = await fp.call_tool("balance_query", {
                "module": "ES",
                "page_size": 1,
            })
            _print_result("balance_query", result)
            print(f"\n  ✅ balance_query OK")
        except FocusingProMCPError as e:
            print(f"  ❌ balance_query failed: {e}")

    _print_section("Test Complete")
    print("  Read-only. No data was modified.\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="ilci-william")
    args = parser.parse_args()
    asyncio.run(run_tests(args.namespace))


if __name__ == "__main__":
    main()
