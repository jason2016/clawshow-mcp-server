"""
FocusingPro MCP Adapter — v3 (2026-04-23, backfill mode)

两层协议：
  1. MCP HTTP+SSE  (mcp.focusingpro.com/mcp)     → inscription 查询、CP 查询
  2. Raw JSON-RPC  (focusingpro.com:8443/.../mcp) → executeTableAction 补录写入

补录模式原理：
  MCP 自己构造完整 item 字段，直接调用 Collect&Pay 表 Action。
  绕过 inscription 表 Action（前端脚本不执行 → envViables 空 → 不创建 CP 记录）。

URL env vars:
  FOCUSINGPRO_MCP_URL      — MCP HTTP+SSE URL (default: https://mcp.focusingpro.com/mcp)
  FOCUSINGPRO_RPC_URL      — Raw JSON-RPC URL (default: https://focusingpro.com:8443/FocusingPro/UEGroup/mcp)
Token env vars:
  FOCUSINGPRO_TOKEN_{NAMESPACE_UPPER}   — namespace-specific token
  FOCUSINGPRO_ADMIN_TOKEN               — fallback admin token
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_MCP_URL = "https://mcp.focusingpro.com/mcp"
_DEFAULT_RPC_URL = "https://focusingpro.com:8443/FocusingPro/UEGroup/mcp"

# Collect&Pay table
COLLECT_PAY_TABLE = "eeb7989fa4014a7590d0ba60aadb60c1"

# Cash action code used for ALL online backfill (Online action broken via API)
COLLECT_ACTION_CASH = "de403ed135f2447487849a096f89572d"

COLLECT_ACTIONS = {
    "Cash":     "de403ed135f2447487849a096f89572d",
    "Check":    "20c94cfb2a404bb48119efd65390bb47",
    "Balance":  "12de747762574e0abf3dcd345163024d",
    "Transfer": "aaa8d0a258fc4277b5e83887149f3312",
    "BankCard": "1c1f4b955f6149b7a84a895dd372a08c",
    # Online uses Cash action code (Online action requires platform payment gateway)
    "Online":   "de403ed135f2447487849a096f89572d",
}

# Confirmed immediately vs needs manual confirmation
IMMEDIATE_MODES = {"Cash", "BankCard", "Balance", "Online"}

ES_MODULE = "dd1f960651e945eebccecf95c38e09c0"

# Accounting mode mapping
ACCOUNTING_MODE = {
    "Cash": "Cash",
    "Check": "Check",
    "Transfer": "Transfer",
    "BankCard": "BankCard",
    "Balance": "Balance",
    "Online": "BankCard",  # Mollie/Stripe = BankCard; PayPal = Paypal (override via pay_account_type)
}

# PayPal gateway overrides accounting_mode
PAYPAL_ACCOUNTING = "Paypal"


def _get_mcp_url() -> str:
    return os.environ.get("FOCUSINGPRO_MCP_URL", _DEFAULT_MCP_URL)


def _get_rpc_url() -> str:
    return os.environ.get("FOCUSINGPRO_RPC_URL", _DEFAULT_RPC_URL)


class FocusingProMCPError(Exception):
    pass


class FocusingProMCPAdapter:
    """
    Async context manager for FocusingPro payment writeback.

    Usage:
        async with FocusingProMCPAdapter(namespace="ilci-william") as fp:
            result = await fp.writeback_payment(
                inscription_code="CDUEG202506222590",
                amount=99.0,
                transaction_id="tr_mollie_xxx",
                paid_at="2026-04-23",
                gateway="mollie",
            )
    """

    def __init__(self, namespace: str):
        self.namespace = namespace
        self.mcp_url = _get_mcp_url()
        self.rpc_url = _get_rpc_url()

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
        self._rpc_counter = 0

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
    # MCP HTTP+SSE session management
    # ------------------------------------------------------------------

    async def _initialize_session(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "clawshow-mcp-server", "version": "3.0"},
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

        logger.debug("FocusingPro MCP session: %s", self._session_id)

        try:
            await self._client.post(
                self.mcp_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                headers={"mcp-session-id": self._session_id},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # MCP HTTP+SSE tool call (for read operations)
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict:
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

        logger.info("FocusingPro MCP call: tool=%s ns=%s", tool_name, self.namespace)

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
    # Raw JSON-RPC (for write operations — backfill mode)
    # ------------------------------------------------------------------

    async def _raw_execute_action(
        self, table_code: str, action_code: str, item: Dict[str, Any]
    ) -> str:
        """
        Call executeTableAction via raw JSON-RPC.
        Returns the created record's MyRangeKey (e.g. "PC2026042300006572").
        """
        if not self._client:
            raise RuntimeError("Use FocusingProMCPAdapter as async context manager")

        self._rpc_counter += 1
        payload = {
            "jsonrpc": "2.0",
            "method": "executeTableAction",
            "params": [{
                "tableCode": table_code,
                "actionCode": action_code,
                "item": item,
            }],
            "id": self._rpc_counter,
        }

        logger.info("FocusingPro RPC executeTableAction: table=%s action=%s", table_code, action_code)

        try:
            resp = await self._client.post(
                self.rpc_url,
                json=payload,
                headers={
                    "Authorization": self.token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("FocusingPro RPC HTTP error: status=%d body=%s",
                         exc.response.status_code, exc.response.text[:300])
            raise FocusingProMCPError(
                f"RPC HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except Exception as exc:
            raise FocusingProMCPError(str(exc)) from exc

        try:
            data = resp.json()
        except Exception:
            raise FocusingProMCPError(f"RPC response not JSON: {resp.text[:200]}")

        if "error" in data:
            err = data["error"]
            raise FocusingProMCPError(
                f"RPC error {err.get('code')}: {err.get('message')}"
            )

        result = data.get("result")
        if not result:
            raise FocusingProMCPError(f"RPC returned no result: {data}")

        return str(result)

    # ------------------------------------------------------------------
    # Step 1: Find inscription
    # ------------------------------------------------------------------

    async def find_inscription(self, inscription_code: str) -> Optional[Dict]:
        """
        Find student inscription by code via inscription_get MCP tool.
        Falls back to inscription_query if inscription_get returns empty.
        """
        # Primary: inscription_get (exact lookup by ID)
        try:
            result = await self.call_tool("inscription_get", {
                "params": {"inscription_id": inscription_code},
            })
            record = result.get("item") or result.get("record") or result.get("data")
            if not record and result.get("tableInfo"):
                # Some responses embed the record fields at top level alongside tableInfo
                # Check if MyRangeKey (or readable "code") is present
                if result.get("code") or result.get("MyRangeKey"):
                    record = result
            if record:
                code = record.get("code") or record.get("MyRangeKey", "")
                if str(code) == str(inscription_code):
                    logger.info("find_inscription: inscription_get found code=%s", inscription_code)
                    return record
        except FocusingProMCPError as exc:
            logger.debug("find_inscription: inscription_get failed (%s), trying query", exc)

        # Fallback: inscription_query with code filter + explicit match validation
        modules_to_try = [self.module]
        alt_module = "language" if self.module != "language" else "enseignement_superieur"
        modules_to_try.append(alt_module)

        for module in modules_to_try:
            result = await self.call_tool("inscription_query", {
                "module": module,
                "params": {
                    "inscription_code": inscription_code,
                    "page_size": 10,
                    "page": 1,
                },
            })
            items = (
                result.get("items")
                or result.get("records")
                or result.get("data")
                or []
            )
            for item in items:
                item_code = item.get("code") or item.get("MyRangeKey", "")
                if str(item_code) == str(inscription_code):
                    logger.info("find_inscription: found code=%s in module=%s", inscription_code, module)
                    return item

        logger.warning("find_inscription: no record for code=%s", inscription_code)
        return None

    # ------------------------------------------------------------------
    # Step 2: Backfill payment (single-step, replaces register+confirm)
    # ------------------------------------------------------------------

    def _build_collect_item(
        self,
        inscription: Dict,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
        mode: str = "Online",
    ) -> Dict[str, Any]:
        """
        Build the CP table item for backfill mode.

        Verified working with stand3 (PC2026042300006572 created 2026-04-23).
        Is Refund is boolean false (not string).
        Money/Unrecognized are numbers.
        """
        inscription_code = (
            inscription.get("code")
            or inscription.get("MyRangeKey")
        )
        programme_price = inscription.get("prix_du_programme") or inscription.get("b330bb0c7646418ab1cef17a3d7e8f50", "")
        futu_id = inscription.get("futu_show_id") or inscription.get("f54c22f765ea4731b39b8dcc6b52ad56", "")
        ecole = inscription.get("ecole") or inscription.get("29cfad6750384b629042a650dd895449", "")
        hash_code = inscription.get("hash_code") or inscription.get("33aaec1e0358450eba9bf8c0e700ec1f", "")

        # Reference order = hash_code + _ + inscription_code
        reference_order = f"{hash_code}_{inscription_code}" if hash_code else inscription_code

        abstract = (
            f"Inscription: {inscription_code};"
            f"Prix du programme: {programme_price}EUR;"
            f"ClawShow:{transaction_id}"
        )

        # Normalize date to "YYYY-MM-DD HH:MM:SS"
        if paid_at and len(paid_at) == 10:
            operate_ts = f"{paid_at} 17:00:00"
        else:
            operate_ts = paid_at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        is_immediate = mode in IMMEDIATE_MODES

        # PayAccountType: use gateway name (Mollie/Stripe/Paypal/etc.)
        pay_account_type = gateway.capitalize() if gateway else ""

        # accounting_mode: Online → BankCard (except PayPal)
        if mode == "Online" and gateway.lower() == "paypal":
            acc_mode = PAYPAL_ACCOUNTING
        else:
            acc_mode = ACCOUNTING_MODE.get(mode, "BankCard")

        item: Dict[str, Any] = {
            "47f072ada8574181922f05d96e67ca5b": abstract,           # Abstract
            "MyName": futu_id,                                       # Dealer FutuID (student)
            "f15bc3e80c474846aaa1c7fc00b21ffb": "Collect",           # Type
            "42df37fb599b4148940f5d7d423580c1": mode,               # Mode
            "759801ffb2b04c7cb9a3b9e8f282a306": False,              # Is Refund (boolean)
            "b71c4dc006d04876a6a4a33abf5407dd": ecole,               # Catalog (Ecole)
            "435b9a60f1fa467795efa120b891ea3e": amount,             # Money (number)
            "14eefa9ccaaf4ffca7913af80ef1095d": 0,                   # Unrecognized (0 = immediate)
            "c61be2426b0c45d28fe80c240835db27": "Confirmed" if is_immediate else "NewCreate",
            "9e887b6fa35341aeb33eae00bf69cfd4": operate_ts,          # Operate Date
            "00b3add61e7047dc9a476eb8e0fa23d9": reference_order,    # Reference Order
            "234fba95c08746f8b9a8cb97fd7f8af8": "",                  # Reference Tag
            "e79e468975414dfaad4955f2fc7786ef": "http://testURL",    # Order URL
            "9d9a8ce897394a52adc2b83d4d4ac436": f"_{ES_MODULE}_payOrCollectCallback",
            "570eaa2e15684c36850bae9c6e84f538": ES_MODULE,           # App
        }

        if is_immediate:
            item["9b4d1f07941b4170a97660e672f798ad"] = operate_ts   # ConfirmDate
        else:
            item["14eefa9ccaaf4ffca7913af80ef1095d"] = amount        # Unrecognized = amount (in transit)

        if mode == "Online":
            item["f8876e4a952942f8a11a6c6c84562f40"] = pay_account_type  # PayAccountType
            item["70b55780aec247838d804a020ef94392"] = acc_mode          # accounting_mode

        return item

    async def _backfill_payment(
        self,
        inscription: Dict,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
        mode: str = "Online",
    ) -> str:
        """
        Backfill: build CP item and call CP table action directly.
        Returns the created CP record ID (e.g. "PC2026042300006572").
        """
        item = self._build_collect_item(
            inscription=inscription,
            amount=amount,
            transaction_id=transaction_id,
            paid_at=paid_at,
            gateway=gateway,
            mode=mode,
        )
        action_code = COLLECT_ACTIONS[mode]
        return await self._raw_execute_action(COLLECT_PAY_TABLE, action_code, item)

    # ------------------------------------------------------------------
    # Idempotency check
    # ------------------------------------------------------------------

    async def verify_payment_exists(self, transaction_id: str) -> bool:
        """
        Check if this transaction_id is already recorded in FocusingPro.
        Returns False on error so the writeback proceeds.
        """
        try:
            result = await self.call_tool("collect_pay_query", {
                "params": {
                    "abstract": transaction_id,
                    "page_size": 5,
                    "page": 1,
                },
            })
            records = result.get("records") or result.get("data") or result.get("items") or []
            exists = any(
                transaction_id in str(r.get("abstract", ""))
                or transaction_id in str(r.get("note", ""))
                or transaction_id in str(r.get("external_reference", ""))
                for r in records
            )
            if exists:
                logger.info("verify_payment_exists: %s already in FocusingPro", transaction_id)
            return exists
        except FocusingProMCPError as exc:
            logger.warning("verify_payment_exists fallback (error: %s) — assuming not exists", exc)
            return False

    # ------------------------------------------------------------------
    # Combined writeback (single-step backfill)
    # ------------------------------------------------------------------

    async def writeback_payment(
        self,
        inscription_code: str,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
        mode: str = "Online",
    ) -> Dict:
        """
        Full payment writeback using backfill mode.

        Flow: find inscription → build CP item → executeTableAction (single step)

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
            # Step 1: find inscription
            inscription = await self.find_inscription(inscription_code)
            if not inscription:
                result["error"] = f"Inscription not found: {inscription_code}"
                return result
            result["steps_completed"].append("find")

            # Step 2: backfill (single call)
            cp_id = await self._backfill_payment(
                inscription=inscription,
                amount=amount,
                transaction_id=transaction_id,
                paid_at=paid_at,
                gateway=gateway,
                mode=mode,
            )
            result["steps_completed"].append("backfill")
            result["focusingpro_record_id"] = cp_id
            result["success"] = True

            logger.info("writeback_payment: success ns=%s inscription=%s amount=%.2f tx=%s cp=%s",
                        self.namespace, inscription_code, amount, transaction_id, cp_id)

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

    # ------------------------------------------------------------------
    # Legacy compatibility (kept for callers still using 3-step API)
    # ------------------------------------------------------------------

    async def register_payment(
        self,
        inscription_id: str,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
    ) -> str:
        """Deprecated: use writeback_payment() instead."""
        raise FocusingProMCPError(
            "register_payment is deprecated. Use writeback_payment() with backfill mode."
        )

    async def confirm_payment(self, collect_pay_id: str) -> Dict:
        """Deprecated: backfill mode creates Confirmed records in one step."""
        raise FocusingProMCPError(
            "confirm_payment is deprecated. Use writeback_payment() with backfill mode."
        )


def _step_name(n: int) -> str:
    return {1: "find_inscription", 2: "backfill_payment"}.get(n, "unknown")
