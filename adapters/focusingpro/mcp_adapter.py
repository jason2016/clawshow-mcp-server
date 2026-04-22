"""
FocusingPro MCP Adapter — Week 2 (2026-04-22)

Calls FocusingPro MCP Server using standard MCP JSON-RPC over HTTP.
Uses 3 already-tested tools (no new development needed from Richard):
  1. inscription_query          — find student by inscription_code
  2. payment_register_online_receipt — register payment receipt
  3. payment_confirm_received    — confirm payment (NewCreate → Confirmed)

URL convention:
  {FOCUSINGPRO_MCP_BASE_URL_{ENV}}/FocusingPro/{enterprise_code}/mcp
  e.g. https://stand3.focusingpro.com:8443/FocusingPro/UEGroup/mcp

Token env var convention: FOCUSINGPRO_TOKEN_{NAMESPACE_UPPER}
  Fallback: FOCUSINGPRO_ADMIN_TOKEN
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


def _build_mcp_url(namespace: str) -> str:
    env = os.environ.get("FOCUSINGPRO_ENV", "test")
    base_env = f"FOCUSINGPRO_MCP_BASE_URL_{env.upper()}"
    base = os.environ.get(base_env)

    ent_env = f"FOCUSINGPRO_ENTERPRISE_{namespace.upper().replace('-', '_')}"
    enterprise = os.environ.get(ent_env)

    if base and enterprise:
        return f"{base}/FocusingPro/{enterprise}/mcp"

    # Legacy fallback: explicit full URL
    legacy = os.environ.get("FOCUSINGPRO_MCP_URL")
    if legacy:
        return legacy

    raise ValueError(
        f"Cannot build FocusingPro MCP URL. "
        f"Need either ({base_env} + {ent_env}) or FOCUSINGPRO_MCP_URL."
    )


class FocusingProMCPError(Exception):
    pass


class FocusingProMCPAdapter:
    """
    Async context manager for FocusingPro MCP calls.

    Usage:
        async with FocusingProMCPAdapter(namespace="ilci-william") as fp:
            result = await fp.writeback_payment(
                inscription_code="UEG2026_0001",
                amount=1200.0,
                transaction_id="tr_mollie_xxx",
                paid_at="2026-05-15",
                gateway="mollie",
            )
    """

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.mcp_url = _build_mcp_url(namespace)

        token_env = f"FOCUSINGPRO_TOKEN_{namespace.upper().replace('-', '_')}"
        self.token = (
            os.environ.get(token_env)
            or os.environ.get("FOCUSINGPRO_ADMIN_TOKEN")
        )
        if not self.token:
            raise ValueError(
                f"Missing FocusingPro token. Set {token_env} or FOCUSINGPRO_ADMIN_TOKEN."
            )

        module_env = f"FOCUSINGPRO_MODULE_{namespace.upper().replace('-', '_')}"
        self.module = os.environ.get(module_env, "ES")

        self._client: Optional[httpx.AsyncClient] = None
        self._call_counter = 0

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Core MCP call (standard JSON-RPC 2.0 over HTTP)
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict:
        """
        Call a FocusingPro MCP tool via JSON-RPC 2.0.

        The FocusingPro MCP server uses the standard MCP HTTP transport:
          POST /mcp
          {"jsonrpc":"2.0","id":N,"method":"tools/call","params":{"name":...,"arguments":{...}}}

        Returns the tool result dict (content unwrapped).
        Raises FocusingProMCPError on any failure.
        """
        if not self._client:
            raise RuntimeError("Use FocusingProMCPAdapter as async context manager")

        self._call_counter += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._call_counter,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        logger.info("FocusingPro MCP call: tool=%s ns=%s", tool_name, self.namespace)

        try:
            resp = await self._client.post(self.mcp_url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("FocusingPro HTTP error: tool=%s status=%d body=%s",
                         tool_name, exc.response.status_code, exc.response.text[:300])
            raise FocusingProMCPError(
                f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except Exception as exc:
            logger.error("FocusingPro network error: tool=%s error=%s", tool_name, exc)
            raise FocusingProMCPError(str(exc)) from exc

        data = resp.json()

        # JSON-RPC error
        if "error" in data:
            err = data["error"]
            raise FocusingProMCPError(f"MCP error {err.get('code')}: {err.get('message')}")

        # Unwrap MCP result content
        result = data.get("result", {})
        content = result.get("content", [])
        if content and isinstance(content, list):
            first = content[0]
            if isinstance(first, dict) and first.get("type") == "text":
                import json as _json
                try:
                    return _json.loads(first["text"])
                except Exception:
                    return {"raw": first["text"]}
        return result

    # ------------------------------------------------------------------
    # Step 1: Find inscription
    # ------------------------------------------------------------------

    async def find_inscription(self, inscription_code: str) -> Optional[Dict]:
        """Find student inscription by code. Returns first match or None."""
        result = await self.call_tool("inscription_query", {
            "inscription_code": inscription_code,
            "module": self.module,
            "page_size": 1,
        })
        records = result.get("records") or result.get("data") or result.get("items") or []
        if not records:
            logger.warning("inscription_query: no record for code=%s", inscription_code)
            return None
        return records[0]

    # ------------------------------------------------------------------
    # Step 2: Register online receipt
    # ------------------------------------------------------------------

    async def register_payment(
        self,
        inscription_id: str,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
    ) -> str:
        """
        Register an online payment receipt in FocusingPro.
        Returns the collect_pay_id for Step 3.

        Parameter name variants are handled by trying known aliases.
        """
        result = await self.call_tool("payment_register_online_receipt", {
            "inscription_id": inscription_id,
            "amount": amount,
            "external_reference": transaction_id,
            "paid_at": paid_at,
            "payment_method": "Online",
            "note": f"ClawShow auto-writeback via {gateway}",
            "module": self.module,
        })

        # Try known field name variants for the returned record ID
        collect_pay_id = (
            result.get("collect_pay_id")
            or result.get("MyRangeKey")
            or result.get("range_key")
            or result.get("code")
            or result.get("id")
        )
        if not collect_pay_id:
            raise FocusingProMCPError(
                f"payment_register_online_receipt returned no ID. Response: {result}"
            )
        return str(collect_pay_id)

    # ------------------------------------------------------------------
    # Step 3: Confirm payment
    # ------------------------------------------------------------------

    async def confirm_payment(self, collect_pay_id: str) -> Dict:
        """Confirm receipt (NewCreate → Confirmed)."""
        return await self.call_tool("payment_confirm_received", {
            "collect_pay_id": collect_pay_id,
            "module": self.module,
        })

    # ------------------------------------------------------------------
    # Idempotency check
    # ------------------------------------------------------------------

    async def verify_payment_exists(self, transaction_id: str) -> bool:
        """
        Check if this transaction_id is already recorded in FocusingPro.
        Conservative: returns False on error (let caller proceed and handle duplicate).
        """
        try:
            result = await self.call_tool("collect_pay_query", {
                "external_reference": transaction_id,
                "module": self.module,
                "page_size": 1,
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            exists = len(records) > 0
            if exists:
                logger.info("verify_payment_exists: transaction %s already in FocusingPro", transaction_id)
            return exists
        except FocusingProMCPError as exc:
            logger.warning("verify_payment_exists fallback (error: %s) — assuming not exists", exc)
            return False

    # ------------------------------------------------------------------
    # Combined 3-step writeback
    # ------------------------------------------------------------------

    async def writeback_payment(
        self,
        inscription_code: str,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
    ) -> Dict:
        """
        Full 3-step writeback: find → register → confirm.

        Returns:
            {
                "success": bool,
                "focusingpro_record_id": str | None,
                "inscription_code": str,
                "steps_completed": list[str],   # ["find", "register", "confirm"]
                "error": str | None,
            }
        """
        result: Dict = {
            "success": False,
            "focusingpro_record_id": None,
            "inscription_code": inscription_code,
            "steps_completed": [],
            "error": None,
        }

        try:
            # Step 1
            inscription = await self.find_inscription(inscription_code)
            if not inscription:
                result["error"] = f"Inscription not found: {inscription_code}"
                return result
            result["steps_completed"].append("find")

            inscription_id = (
                inscription.get("MyRangeKey")
                or inscription.get("range_key")
                or inscription.get("inscription_id")
                or inscription.get("id")
            )
            if not inscription_id:
                result["error"] = f"Cannot extract inscription_id from record: {list(inscription.keys())}"
                return result

            # Step 2
            collect_pay_id = await self.register_payment(
                inscription_id=str(inscription_id),
                amount=amount,
                transaction_id=transaction_id,
                paid_at=paid_at,
                gateway=gateway,
            )
            result["steps_completed"].append("register")
            result["focusingpro_record_id"] = collect_pay_id

            # Step 3
            await self.confirm_payment(collect_pay_id)
            result["steps_completed"].append("confirm")

            result["success"] = True
            logger.info("writeback_payment: success ns=%s inscription=%s amount=%.2f tx=%s",
                        self.namespace, inscription_code, amount, transaction_id)

        except FocusingProMCPError as exc:
            step_n = len(result["steps_completed"]) + 1
            result["error"] = f"Step {step_n} ({_step_name(step_n)}): {exc}"
            logger.error("writeback_payment failed: ns=%s inscription=%s error=%s",
                         self.namespace, inscription_code, result["error"])
        except Exception as exc:
            step_n = len(result["steps_completed"]) + 1
            result["error"] = f"Unexpected at step {step_n}: {exc}"
            logger.exception("writeback_payment unexpected error")

        return result


def _step_name(n: int) -> str:
    return {1: "find_inscription", 2: "register_payment", 3: "confirm_payment"}.get(n, "unknown")
