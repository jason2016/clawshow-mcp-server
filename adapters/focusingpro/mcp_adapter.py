"""
FocusingPro MCP Adapter — Week 2 (2026-04-22, v2)

Protocol: MCP HTTP+SSE (mcp.focusingpro.com/mcp)
  1. POST /mcp  initialize          → get mcp-session-id header
  2. POST /mcp  notifications/initialized  (no response needed)
  3. POST /mcp  tools/call          → SSE event with result

URL env var: FOCUSINGPRO_MCP_URL (full URL, e.g. https://mcp.focusingpro.com/mcp)
Token env var: FOCUSINGPRO_TOKEN_{NAMESPACE_UPPER}
  Fallback: FOCUSINGPRO_ADMIN_TOKEN
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_MCP_URL = "https://mcp.focusingpro.com/mcp"


def _get_mcp_url() -> str:
    return os.environ.get("FOCUSINGPRO_MCP_URL", _DEFAULT_MCP_URL)


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
        self.mcp_url = _get_mcp_url()

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
        self.module = os.environ.get(module_env, "enseignement_superieur")

        self._client: Optional[httpx.AsyncClient] = None
        self._session_id: Optional[str] = None
        self._call_counter = 0

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        await self._initialize_session()
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_id = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _initialize_session(self) -> None:
        """
        MCP HTTP+SSE handshake:
          POST initialize → capture mcp-session-id header
          POST notifications/initialized (fire-and-forget)
        """
        payload = {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "clawshow-mcp-server", "version": "2.0"},
            },
        }
        try:
            resp = await self._client.post(self.mcp_url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise FocusingProMCPError(
                f"FocusingPro initialize failed: HTTP {exc.response.status_code}"
            ) from exc

        self._session_id = resp.headers.get("mcp-session-id")
        if not self._session_id:
            raise FocusingProMCPError(
                "FocusingPro initialize returned no mcp-session-id header"
            )

        logger.debug("FocusingPro session: %s", self._session_id)

        # Notify server we're ready (fire-and-forget)
        try:
            await self._client.post(
                self.mcp_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                headers={"mcp-session-id": self._session_id},
            )
        except Exception:
            pass  # non-fatal

    # ------------------------------------------------------------------
    # Core MCP call
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict:
        """
        Call a FocusingPro MCP tool via HTTP+SSE JSON-RPC.

        Response is SSE: parse 'data: {...}' lines and unwrap content[0].text.
        Raises FocusingProMCPError on any failure.
        """
        if not self._client or not self._session_id:
            raise RuntimeError("Use FocusingProMCPAdapter as async context manager")

        self._call_counter += 1
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._call_counter),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        logger.info("FocusingPro call: tool=%s ns=%s", tool_name, self.namespace)

        try:
            resp = await self._client.post(
                self.mcp_url,
                json=payload,
                headers={"mcp-session-id": self._session_id},
            )
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

        return self._parse_sse_response(resp.text, tool_name)

    def _parse_sse_response(self, raw: str, tool_name: str) -> Dict:
        """Parse SSE 'data: {...}' lines and unwrap MCP content."""
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            if "error" in data:
                err = data["error"]
                raise FocusingProMCPError(
                    f"MCP error {err.get('code')}: {err.get('message')}"
                )

            result = data.get("result", {})
            content = result.get("content", [])
            if content and isinstance(content, list):
                first = content[0]
                if isinstance(first, dict) and first.get("type") == "text":
                    try:
                        return json.loads(first["text"])
                    except Exception:
                        return {"raw": first["text"]}
            return result

        raise FocusingProMCPError(
            f"No parseable SSE data line in response for tool={tool_name}"
        )

    # ------------------------------------------------------------------
    # Step 1: Find inscription
    # ------------------------------------------------------------------

    async def find_inscription(self, inscription_code: str) -> Optional[Dict]:
        """Find student inscription by code. Returns first match or None."""
        result = await self.call_tool("inscription_query", {
            "params": {
                "inscription_code": inscription_code,
                "page_size": 1,
                "page": 1,
            },
            "module": self.module,
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
        """
        result = await self.call_tool("payment_register_online_receipt", {
            "params": {
                "inscription_id": inscription_id,
                "amount": amount,
                "external_reference": transaction_id,
                "paid_at": paid_at,
                "payment_method": "Online",
                "note": f"ClawShow auto-writeback via {gateway}",
            },
            "module": self.module,
        })

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
            "params": {
                "collect_pay_id": collect_pay_id,
            },
        })

    # ------------------------------------------------------------------
    # Idempotency check
    # ------------------------------------------------------------------

    async def verify_payment_exists(self, transaction_id: str) -> bool:
        """
        Check if this transaction_id is already recorded in FocusingPro.
        Conservative: returns False on error.
        """
        try:
            result = await self.call_tool("collect_pay_query", {
                "params": {
                    "external_reference": transaction_id,
                    "page_size": 1,
                    "page": 1,
                },
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            exists = len(records) > 0
            if exists:
                logger.info("verify_payment_exists: %s already in FocusingPro", transaction_id)
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
                "steps_completed": list[str],
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
                or inscription.get("code")
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
