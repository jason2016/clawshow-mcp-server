"""
FocusingPro MCP Adapter — v4 (2026-04-23, V8 Online Supplement)

两层协议：
  1. MCP HTTP+SSE  (mcp.focusingpro.com/mcp)     → inscription 查询、CP 查询
  2. Raw JSON-RPC  (focusingpro.com:8443/.../mcp) → executeTableAction 补录写入

V8 补录模式 (v2 指南验证通过 — PC2026042300006583):
  使用真·Online Collect Action (647f2cbf...) + item + bundle (SupplementOnlinePayment=True)
  触发完整业务闭环：CP 记录 + PUBLICRECEIVELOG + inscription Montant déjà perçu 更新

Mollie 方案 β：PayType="Stripe", AcountID="Test Stripe", TransactionID="MOLLIE:tr_xxx"
  (FP PayType 枚举不认识 Mollie，通过 Stripe 代理接入 Reconciliation)

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
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_MCP_URL = "https://mcp.focusingpro.com/mcp"
_DEFAULT_RPC_URL = "https://focusingpro.com:8443/FocusingPro/UEGroup/mcp"

# ── Collect&Pay table ──────────────────────────────────────────────────────────
COLLECT_PAY_TABLE = "eeb7989fa4014a7590d0ba60aadb60c1"

# V8: true Online Collect Action (verified on stand3, produces PUBLICRECEIVELOG + callback)
ONLINE_COLLECT_ACTION = "647f2cbf7de643a38948ea9c428e853b"

# Other payment mode action codes (not used for Mollie backfill)
COLLECT_ACTIONS = {
    "Cash":     "de403ed135f2447487849a096f89572d",
    "Check":    "20c94cfb2a404bb48119efd65390bb47",
    "Balance":  "12de747762574e0abf3dcd345163024d",
    "Transfer": "aaa8d0a258fc4277b5e83887149f3312",
    "BankCard": "1c1f4b955f6149b7a84a895dd372a08c",
    "Online":   ONLINE_COLLECT_ACTION,  # V8: real Online action, NOT Cash alias
}

# ES (Enseignement Supérieur) module app ID — used in callback hook
ES_MODULE = "dd1f960651e945eebccecf95c38e09c0"

# Scheme β: Mollie is disguised as Stripe (FP PayType enum has no Mollie entry)
GATEWAY_PAYTYPE = {
    "mollie":  "Stripe",
    "stripe":  "Stripe",
    "paypal":  "Paypal",
}
GATEWAY_ACCOUNT_ID = {
    "mollie":  "Test Stripe",
    "stripe":  "Test Stripe",
    "paypal":  "Paypal",
}
GATEWAY_TX_PREFIX = {
    "mollie": "MOLLIE:",
    "stripe": "",
    "paypal": "PAYPAL:",
}


def _get_mcp_url() -> str:
    return os.environ.get("FOCUSINGPRO_MCP_URL", _DEFAULT_MCP_URL)


def _get_rpc_url() -> str:
    return os.environ.get("FOCUSINGPRO_RPC_URL", _DEFAULT_RPC_URL)


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class FocusingProMCPError(Exception):
    pass


class FocusingProMCPAdapter:
    """
    Async context manager for FocusingPro payment writeback (V8 mode).

    Usage:
        async with FocusingProMCPAdapter(namespace="ilci-william") as fp:
            result = await fp.writeback_payment(
                inscription_code="CDUEG202506222590",
                amount=10.0,
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

    # ── MCP HTTP+SSE session management ────────────────────────────────────────

    async def _initialize_session(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "clawshow-mcp-server", "version": "4.0"},
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

    # ── MCP HTTP+SSE tool call (read operations) ────────────────────────────────

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

    # ── Raw JSON-RPC (write operations — V8 backfill) ──────────────────────────

    async def _raw_execute_action(
        self,
        table_code: str,
        action_code: str,
        item: Dict[str, Any],
        bundle: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Call executeTableAction via raw JSON-RPC.

        V8: bundle is required for Online Collect Action to trigger
        PUBLICRECEIVELOG and inscription Montant callback.

        Returns the created record's MyRangeKey (e.g. "PC2026042300006583").
        """
        if not self._client:
            raise RuntimeError("Use FocusingProMCPAdapter as async context manager")

        self._rpc_counter += 1
        params_obj: Dict[str, Any] = {
            "tableCode": table_code,
            "actionCode": action_code,
            "item": item,
        }
        if bundle is not None:
            params_obj["bundle"] = bundle

        payload = {
            "jsonrpc": "2.0",
            "method": "executeTableAction",
            "params": [params_obj],
            "id": self._rpc_counter,
        }

        logger.info("FocusingPro RPC executeTableAction: table=%s action=%s has_bundle=%s",
                    table_code, action_code, bundle is not None)

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

    # ── Step 1: Find inscription ────────────────────────────────────────────────

    async def find_inscription(self, inscription_code: str) -> Optional[Dict]:
        """
        Find student inscription by code via inscription_get MCP tool.
        Falls back to inscription_query if inscription_get returns empty.
        """
        try:
            result = await self.call_tool("inscription_get", {
                "params": {"inscription_id": inscription_code},
            })
            items = result.get("items") or result.get("records") or result.get("data") or []
            for item in items:
                item_code = item.get("code") or item.get("MyRangeKey", "")
                if str(item_code) == str(inscription_code):
                    logger.info("find_inscription: inscription_get found code=%s", inscription_code)
                    return item
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

    # ── Step 2: Build V8 item + bundle ─────────────────────────────────────────

    def _build_v8_item(
        self,
        inscription: Dict,
        inscription_code: str,
        amount: float,
        transaction_id_prefixed: str,
        paid_at: str,
        pay_type: str,
        account_id: str,
        currency: str,
        module_app_id: str,
    ) -> Dict[str, Any]:
        """
        Construct V8 item for Online Collect Action.

        Rules (v2 指南 必做 ✅):
        - Amounts are numbers (not strings)
        - Booleans are booleans (not strings)
        - Do NOT put bundle-only envViables keys (Amount, Currency, PayType, AcountID,
          TransactionID, TransactionReference) in item — those belong only in bundle
        - Do NOT put afterAutoEditCallBackFunctionName in item (bundle-only)
        """
        futu_id = (
            inscription.get("futu_id")
            or inscription.get("f54c22f765ea4731b39b8dcc6b52ad56", "")
        )
        school = (
            inscription.get("school")
            or inscription.get("ecole")
            or inscription.get("29cfad6750384b629042a650dd895449", "")
        )
        programme_price = (
            inscription.get("programme_price")
            or inscription.get("prix_du_programme")
            or inscription.get("b330bb0c7646418ab1cef17a3d7e8f50", "")
        )
        # hash_code from inscription_get IS the full reference_order
        reference_order = (
            inscription.get("hash_code")
            or inscription.get("33aaec1e0358450eba9bf8c0e700ec1f", "")
            or inscription_code
        )
        speciality = inscription.get("speciality_name") or inscription.get("speciality") or ""
        level = inscription.get("level") or ""
        school_year = inscription.get("school_year") or inscription.get("annee_scolaire") or ""

        abstract = (
            f"Inscription: {inscription_code};"
            f"Prix du programme: {programme_price};"
            f"Gateway: {transaction_id_prefixed};"
            f"ExternalReconciliation:true"
        )

        # Normalize date to "YYYY-MM-DD HH:MM:SS"
        if paid_at and len(paid_at) >= 10:
            operate_ts = paid_at[:10] + " 19:00:00"
        else:
            operate_ts = _now_ts()

        callback_name = f"_{module_app_id}_payOrCollectCallback"

        return {
            # Abstract
            "47f072ada8574181922f05d96e67ca5b": abstract,
            # MyName (student FutuID)
            "MyName": futu_id,
            # Type = Collect
            "f15bc3e80c474846aaa1c7fc00b21ffb": "Collect",
            # Mode = Online
            "42df37fb599b4148940f5d7d423580c1": "Online",
            # Ecole (school catalog)
            "b71c4dc006d04876a6a4a33abf5407dd": school,
            # Amount (number, not string)
            "435b9a60f1fa467795efa120b891ea3e": amount,
            # Is Refund (boolean)
            "759801ffb2b04c7cb9a3b9e8f282a306": False,
            # Status = Confirmed
            "c61be2426b0c45d28fe80c240835db27": "Confirmed",
            # PayAccountType (e.g. "Stripe")
            "f8876e4a952942f8a11a6c6c84562f40": pay_type,
            # accounting_mode = BankCard
            "70b55780aec247838d804a020ef94392": "BankCard",
            # Reference Order (hash_code from inscription)
            "00b3add61e7047dc9a476eb8e0fa23d9": reference_order,
            # TransactionID (GuidKey version — in item this is OK as GuidKey)
            "ae24d48538c44686b2f50014959d8690": transaction_id_prefixed,
            # Currency (GuidKey)
            "5189d73a964e46a5add1e609fa0cb3a1": currency,
            # Operate Date
            "9e887b6fa35341aeb33eae00bf69cfd4": operate_ts,
            # Confirm Date
            "9b4d1f07941b4170a97660e672f798ad": operate_ts,
            # AcountID (Reconciliation account — GuidKey)
            "0ea820f452d8493ca349e6a5c73117e3": account_id,
            # callBackFunctionName — CRITICAL: without this inscription Montant never updates
            "9d9a8ce897394a52adc2b83d4d4ac436": callback_name,
            # App ID
            "570eaa2e15684c36850bae9c6e84f538": module_app_id,
            # Order URL
            "e79e468975414dfaad4955f2fc7786ef": "http://testURL",
            # Commercial marker
            "234fba95c08746f8b9a8cb97fd7f8af8": "商用标记",
            # Catalog 2 — Spécialité
            "71eaf6b0d77f4b1fb6de75a032947f17": speciality,
            # Catalog 3 — Niveau
            "75bdeb83dfdf4c4cb2169cc5c1b44b42": level,
            # Catalog 4 — Année scolaire
            "bf863acd949b4607b611949475c42723": school_year,
        }

    def _build_v8_bundle(
        self,
        inscription: Dict,
        inscription_code: str,
        amount: float,
        transaction_id_prefixed: str,
        paid_at: str,
        pay_type: str,
        account_id: str,
        currency: str,
        module_app_id: str,
        payer_account: str = "",
        payer_surname: str = "MOLLIE",
        payer_givenname: str = "PROXY",
    ) -> Dict[str, Any]:
        """
        Construct V8 bundle (envViables) for Online Collect Action.

        Rules:
        - Amounts are strings ("15", not 15)
        - Booleans are lowercase strings ("false", not False)
        - afterAutoEditCallBackFunctionName goes ONLY in bundle, not item
        - SupplementOnlinePayment = True (Python bool, not string) — Action branch depends on this
        """
        futu_id = (
            inscription.get("futu_id")
            or inscription.get("f54c22f765ea4731b39b8dcc6b52ad56", "")
        )
        school = (
            inscription.get("school")
            or inscription.get("ecole")
            or inscription.get("29cfad6750384b629042a650dd895449", "")
        )
        programme_price = (
            inscription.get("programme_price")
            or inscription.get("prix_du_programme")
            or inscription.get("b330bb0c7646418ab1cef17a3d7e8f50", "")
        )
        reference_order = (
            inscription.get("hash_code")
            or inscription.get("33aaec1e0358450eba9bf8c0e700ec1f", "")
            or inscription_code
        )
        speciality = inscription.get("speciality_name") or inscription.get("speciality") or ""
        level = inscription.get("level") or ""
        school_year = inscription.get("school_year") or inscription.get("annee_scolaire") or ""

        abstract = (
            f"Inscription: {inscription_code};"
            f"Prix du programme: {programme_price};"
            f"Gateway: {transaction_id_prefixed};"
            f"ExternalReconciliation:true"
        )

        callback_name = f"_{module_app_id}_payOrCollectCallback"
        after_callback_name = f"_{module_app_id}_triggerAfterAutoEditCallBack"

        return {
            # Business fields (string versions of item values)
            "f15bc3e80c474846aaa1c7fc00b21ffb": "Collect",
            "42df37fb599b4148940f5d7d423580c1": "Online",
            "435b9a60f1fa467795efa120b891ea3e": str(amount),          # string in bundle
            "47f072ada8574181922f05d96e67ca5b": abstract,
            "MyName": futu_id,
            "b71c4dc006d04876a6a4a33abf5407dd": school,
            "759801ffb2b04c7cb9a3b9e8f282a306": "false",              # string in bundle
            "00b3add61e7047dc9a476eb8e0fa23d9": reference_order,
            "234fba95c08746f8b9a8cb97fd7f8af8": "商用标记",
            "e79e468975414dfaad4955f2fc7786ef": "http://testURL",
            "9d9a8ce897394a52adc2b83d4d4ac436": callback_name,
            # afterAutoEditCallBackFunctionName — ONLY in bundle (never in item)
            "afterAutoEditCallBackFunctionName": after_callback_name,
            "570eaa2e15684c36850bae9c6e84f538": module_app_id,
            # Catalog fields
            "71eaf6b0d77f4b1fb6de75a032947f17": speciality,
            "75bdeb83dfdf4c4cb2169cc5c1b44b42": level,
            "bf863acd949b4607b611949475c42723": school_year,
            # envViables — these plain-string keys belong ONLY in bundle
            "Amount": str(amount),
            "Currency": currency,
            "PayType": pay_type,
            "AcountID": account_id,
            "TransactionReference": f"{transaction_id_prefixed}_ref",
            "TransactionID": transaction_id_prefixed,
            "PayerAccountID": "",
            "PayerAccount": payer_account,
            "PayerSurname": payer_surname,
            "PayerGivenname": payer_givenname,
            # Supplement Online Payment switch — MUST be True (bool) for callback to fire
            "SupplementOnlinePayment": True,
            "SupplementOnlineRefundToPayer": False,
        }

    # ── Step 3: Execute V8 backfill ─────────────────────────────────────────────

    async def _backfill_payment_v8(
        self,
        inscription: Dict,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
        currency: str = "EUR",
    ) -> str:
        """
        V8 backfill: build CP item + bundle and call Online Collect Action.
        Returns the created CP record ID (e.g. "PC2026042300006583").
        """
        gateway_lower = gateway.lower()
        inscription_code = (
            inscription.get("code")
            or inscription.get("MyRangeKey", "")
        )

        # Scheme β: prefix transaction ID with gateway name for Mollie
        prefix = GATEWAY_TX_PREFIX.get(gateway_lower, "")
        # Avoid double-prefixing if already prefixed
        if prefix and transaction_id.startswith(prefix):
            tx_prefixed = transaction_id
        else:
            tx_prefixed = f"{prefix}{transaction_id}" if prefix else transaction_id

        pay_type = GATEWAY_PAYTYPE.get(gateway_lower, "Stripe")
        account_id = GATEWAY_ACCOUNT_ID.get(gateway_lower, "Test Stripe")

        item = self._build_v8_item(
            inscription=inscription,
            inscription_code=inscription_code,
            amount=amount,
            transaction_id_prefixed=tx_prefixed,
            paid_at=paid_at,
            pay_type=pay_type,
            account_id=account_id,
            currency=currency,
            module_app_id=ES_MODULE,
        )

        bundle = self._build_v8_bundle(
            inscription=inscription,
            inscription_code=inscription_code,
            amount=amount,
            transaction_id_prefixed=tx_prefixed,
            paid_at=paid_at,
            pay_type=pay_type,
            account_id=account_id,
            currency=currency,
            module_app_id=ES_MODULE,
        )

        return await self._raw_execute_action(
            COLLECT_PAY_TABLE,
            ONLINE_COLLECT_ACTION,
            item,
            bundle,
        )

    # ── Idempotency check ──────────────────────────────────────────────────────

    async def verify_payment_exists(self, transaction_id: str) -> bool:
        """
        Check if this transaction_id is already recorded in FocusingPro.
        Returns False on error so the writeback proceeds.
        """
        # Check both plain and prefixed forms
        tx_variants = {transaction_id}
        for prefix in GATEWAY_TX_PREFIX.values():
            if prefix and not transaction_id.startswith(prefix):
                tx_variants.add(f"{prefix}{transaction_id}")

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
                any(v in str(r.get("abstract", "")) or v in str(r.get("note", ""))
                    for v in tx_variants)
                for r in records
            )
            if exists:
                logger.info("verify_payment_exists: %s already in FocusingPro", transaction_id)
            return exists
        except FocusingProMCPError as exc:
            logger.warning("verify_payment_exists fallback (error: %s) — assuming not exists", exc)
            return False

    # ── Combined writeback entry point ─────────────────────────────────────────

    async def writeback_payment(
        self,
        inscription_code: str,
        amount: float,
        transaction_id: str,
        paid_at: str,
        gateway: str,
        mode: str = "Online",
        currency: str = "EUR",
    ) -> Dict:
        """
        Full payment writeback using V8 Online Supplement mode.

        Flow: find inscription → build V8 item+bundle → executeTableAction

        Triggers: CP record + PUBLICRECEIVELOG + inscription Montant déjà perçu update

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

            # Step 2: V8 backfill (item + bundle, Online Collect Action)
            cp_id = await self._backfill_payment_v8(
                inscription=inscription,
                amount=amount,
                transaction_id=transaction_id,
                paid_at=paid_at,
                gateway=gateway,
                currency=currency,
            )
            result["steps_completed"].append("backfill_v8")
            result["focusingpro_record_id"] = cp_id
            result["success"] = True

            logger.info(
                "writeback_payment v8: success ns=%s inscription=%s amount=%.2f tx=%s cp=%s",
                self.namespace, inscription_code, amount, transaction_id, cp_id,
            )

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

    # ── Legacy stubs ────────────────────────────────────────────────────────────

    async def register_payment(self, *_, **__):
        raise FocusingProMCPError(
            "register_payment is deprecated. Use writeback_payment() instead."
        )

    async def confirm_payment(self, *_, **__):
        raise FocusingProMCPError(
            "confirm_payment is deprecated. Use writeback_payment() instead."
        )


def _step_name(n: int) -> str:
    return {1: "find_inscription", 2: "backfill_v8"}.get(n, "unknown")
