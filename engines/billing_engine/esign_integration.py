"""
Billing eSign Integration — Week 3.

Flow:
  1. create_plan(contract_required=True, contract_pdf_url=...) → status=pending_signature
  2. send_contract_for_signing() → calls POST /esign/create via internal HTTP
  3. Customer signs → /esign/{id}/sign fires webhook to /webhooks/esign
  4. handle_esign_callback(plan_id, status) → if signed: activate gateway subscription
     if declined/expired: cancel plan

This module is the bridge between the eSign engine and the billing engine.
No direct DB calls outside of BillingDB. No direct calls to Mollie/Stripe here — delegate to orchestrator.
"""
from __future__ import annotations

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai")


async def send_contract_for_signing(
    plan_id: str,
    namespace: str,
    contract_pdf_url: str,
    customer_email: str,
    customer_name: str,
    description: str = "",
) -> Dict:
    """
    Send contract PDF for eSign via the existing /esign/create endpoint.
    Updates plan DB with contract_esign_request_id.
    Returns {ok, esign_request_id, signing_url}.
    """
    import httpx
    from storage.billing_db import BillingDB

    db = BillingDB()

    payload = {
        "namespace": namespace,
        "document_url": contract_pdf_url,
        "signers": [
            {
                "name": customer_name,
                "email": customer_email,
                "role": "signer",
            }
        ],
        "description": description or f"Contract for plan {plan_id}",
        "callback_metadata": {"plan_id": plan_id, "namespace": namespace},
        "webhook_url": f"{MCP_BASE_URL}/webhooks/esign",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{MCP_BASE_URL}/esign/create", json=payload)
        if resp.status_code != 200:
            return {"ok": False, "error": f"esign/create returned {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "error": f"esign/create request failed: {exc}"}

    esign_id = data.get("document_id") or data.get("id") or data.get("esign_request_id")
    if not esign_id:
        return {"ok": False, "error": "esign/create response missing document_id"}

    db.update_plan_status(plan_id, namespace, "pending_signature",
                          contract_esign_request_id=esign_id)

    return {
        "ok": True,
        "esign_request_id": esign_id,
        "signing_url": data.get("signing_url", ""),
    }


async def handle_esign_callback(payload: dict) -> Dict:
    """
    Called from POST /webhooks/esign.
    payload must contain: {plan_id, namespace, status} where status in {signed, declined, expired}.
    Routes to activate or cancel.
    """
    plan_id = payload.get("plan_id") or payload.get("metadata", {}).get("plan_id", "")
    namespace = payload.get("namespace") or payload.get("metadata", {}).get("namespace", "")
    status = payload.get("status", "")

    if not plan_id:
        logger.warning("esign callback: missing plan_id in payload %s", payload)
        return {"ok": False, "error": "missing plan_id"}

    if status == "signed":
        return await _activate_plan_after_signature(plan_id, namespace)
    elif status in ("declined", "expired"):
        return await _cancel_plan_after_failed_signature(plan_id, namespace, reason=status)
    else:
        logger.info("esign callback: unhandled status=%s plan=%s", status, plan_id)
        return {"ok": True, "action": "ignored", "status": status}


async def _activate_plan_after_signature(plan_id: str, namespace: str) -> Dict:
    """
    Contract signed → activate the plan.
    For plans with gateway_plan_id already set: just flip status to active.
    For one_time plans: status → pending_charge.
    """
    from storage.billing_db import BillingDB

    db = BillingDB()

    # Find plan across all namespaces if namespace is empty
    if namespace:
        plan = db.get_plan(plan_id, namespace)
    else:
        plan = db.find_plan_by_id_any_namespace(plan_id)

    if not plan:
        logger.warning("esign activate: plan not found plan_id=%s ns=%s", plan_id, namespace)
        return {"ok": False, "error": f"Plan {plan_id} not found"}

    ns = plan["namespace"]

    if plan["status"] != "pending_signature":
        logger.info("esign activate: plan %s is already %s, skipping", plan_id, plan["status"])
        return {"ok": True, "action": "already_processed", "status": plan["status"]}

    if plan["frequency"] == "one_time":
        new_status = "pending_charge"
    else:
        new_status = "active"

    db.update_plan_status(plan_id, ns, new_status)
    logger.info("esign: plan %s activated → %s", plan_id, new_status)

    # Fire external webhook if configured
    if plan.get("external_webhook_url"):
        from engines.billing_engine.webhook_sender import send_external_webhook
        import asyncio
        asyncio.create_task(send_external_webhook(
            webhook_url=plan["external_webhook_url"],
            auth_token=plan.get("external_auth_token") or "",
            event_type="plan_activated",
            plan_id=plan_id,
            order_id=plan.get("external_order_id") or "",
            namespace=ns,
            payload={"status": new_status, "trigger": "contract_signed"},
        ))

    return {"ok": True, "action": "activated", "plan_id": plan_id, "new_status": new_status}


async def _cancel_plan_after_failed_signature(plan_id: str, namespace: str, reason: str = "declined") -> Dict:
    """Contract declined or expired → cancel the plan (no charge)."""
    from storage.billing_db import BillingDB

    db = BillingDB()

    if namespace:
        plan = db.get_plan(plan_id, namespace)
    else:
        plan = db.find_plan_by_id_any_namespace(plan_id)

    if not plan:
        return {"ok": False, "error": f"Plan {plan_id} not found"}

    ns = plan["namespace"]
    db.update_plan_status(plan_id, ns, "cancelled")
    db.cancel_pending_installments(plan_id)

    logger.info("esign: plan %s cancelled due to %s", plan_id, reason)

    if plan.get("external_webhook_url"):
        from engines.billing_engine.webhook_sender import send_external_webhook
        import asyncio
        asyncio.create_task(send_external_webhook(
            webhook_url=plan["external_webhook_url"],
            auth_token=plan.get("external_auth_token") or "",
            event_type="plan_cancelled",
            plan_id=plan_id,
            order_id=plan.get("external_order_id") or "",
            namespace=ns,
            payload={"reason": f"contract_{reason}"},
        ))

    return {"ok": True, "action": "cancelled", "plan_id": plan_id, "reason": reason}
