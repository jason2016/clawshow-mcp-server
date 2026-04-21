"""
Stripe billing webhook handler — Week 3.
Handles subscription lifecycle events from Stripe IESIG sandbox.

Relevant events (18 configured in dashboard):
  - invoice.payment_succeeded  → mark installment charged
  - invoice.payment_failed     → mark installment failed, schedule retry
  - customer.subscription.deleted → mark plan cancelled
"""
from __future__ import annotations

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)


def verify_stripe_signature(payload_bytes: bytes, sig_header: str, mode: str = "test") -> Dict:
    """
    Verify Stripe webhook signature and return parsed event dict.
    Raises ValueError on failure.
    """
    import stripe

    if mode == "test":
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET_IESIG_TEST", "")
    else:
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET_IESIG_LIVE", "")

    if not secret:
        raise ValueError(f"STRIPE_WEBHOOK_SECRET_IESIG_{mode.upper()} not configured")

    api_key = os.environ.get("STRIPE_API_KEY_IESIG_TEST" if mode == "test" else "STRIPE_API_KEY_IESIG_LIVE", "")
    if api_key:
        stripe.api_key = api_key

    event = stripe.Webhook.construct_event(payload_bytes, sig_header, secret)
    return dict(event)


async def handle_stripe_billing_webhook(payload_bytes: bytes, sig_header: str, mode: str = "test") -> Dict:
    """
    Entry point for POST /webhooks/stripe (billing namespace).
    Verifies signature, routes by event type.
    """
    try:
        event = verify_stripe_signature(payload_bytes, sig_header, mode)
    except Exception as exc:
        logger.warning("Stripe webhook signature failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    logger.info("Stripe billing webhook: type=%s id=%s", event_type, event.get("id"))

    if event_type == "invoice.payment_succeeded":
        return await _handle_invoice_paid(obj)
    elif event_type == "invoice.payment_failed":
        return await _handle_invoice_failed(obj)
    elif event_type == "customer.subscription.deleted":
        return await _handle_subscription_cancelled(obj)
    else:
        # Acknowledge but don't process
        return {"ok": True, "action": "ignored", "event_type": event_type}


async def _handle_invoice_paid(invoice: dict) -> Dict:
    """invoice.payment_succeeded — mark installment as charged."""
    from storage.billing_db import BillingDB
    from engines.billing_engine.webhook_sender import send_external_webhook

    db = BillingDB()
    subscription_id = invoice.get("subscription", "")
    invoice_id = invoice.get("id", "")
    payment_intent = invoice.get("payment_intent", "")

    if not subscription_id:
        return {"ok": True, "action": "skipped", "reason": "no subscription_id"}

    plan = _find_plan_by_subscription(db, subscription_id)
    if not plan:
        logger.warning("Stripe invoice paid: no plan found for sub=%s", subscription_id)
        return {"ok": True, "action": "plan_not_found", "subscription_id": subscription_id}

    plan_id = plan["plan_id"]
    namespace = plan["namespace"]
    payment_id = payment_intent or invoice_id

    # Find earliest scheduled installment
    installments = db.get_installments(plan_id)
    target = next((i for i in installments if i["status"] == "scheduled"), None)
    if target:
        db.update_installment(
            target["id"],
            status="charged",
            gateway_payment_id=payment_id,
            charged_at=_now(),
        )
        # Record commission
        commission_rate = 0.005
        commission_amount = round(target["amount"] * commission_rate, 4)
        db.record_commission({
            "namespace": namespace,
            "plan_id": plan_id,
            "installment_id": target["id"],
            "transaction_amount": target["amount"],
            "commission_rate": commission_rate,
            "commission_amount": commission_amount,
        })

        # Rolling schedule for infinite subs
        if plan.get("installments") == -1:
            db.generate_next_preview_installment(plan_id)

        # Check plan completion
        total = plan.get("installments", 0)
        charged = db.count_charged_installments(plan_id)
        if total != -1 and charged >= total:
            db.update_plan_status(plan_id, namespace, "completed")
            event_type = "plan_completed"
            event_data = {"installment_number": target["installment_number"], "charged": charged, "total": total}
        else:
            event_type = "installment_charged_success"
            event_data = {
                "installment_number": target["installment_number"],
                "amount": target["amount"],
                "payment_id": payment_id,
                "charged": charged,
            }

        if plan.get("external_webhook_url"):
            import asyncio
            asyncio.create_task(send_external_webhook(
                webhook_url=plan["external_webhook_url"],
                auth_token=plan.get("external_auth_token") or "",
                event_type=event_type,
                plan_id=plan_id,
                order_id=plan.get("external_order_id") or "",
                namespace=namespace,
                payload=event_data,
            ))

    return {"ok": True, "action": "charged", "plan_id": plan_id}


async def _handle_invoice_failed(invoice: dict) -> Dict:
    """invoice.payment_failed — mark installment failed, schedule retry."""
    from storage.billing_db import BillingDB
    from engines.billing_engine.retry_manager import schedule_retry
    from engines.billing_engine.webhook_sender import send_external_webhook

    db = BillingDB()
    subscription_id = invoice.get("subscription", "")
    invoice_id = invoice.get("id", "")
    payment_intent = invoice.get("payment_intent", "")

    if not subscription_id:
        return {"ok": True, "action": "skipped", "reason": "no subscription_id"}

    plan = _find_plan_by_subscription(db, subscription_id)
    if not plan:
        return {"ok": True, "action": "plan_not_found"}

    plan_id = plan["plan_id"]
    namespace = plan["namespace"]
    payment_id = payment_intent or invoice_id

    installments = db.get_installments(plan_id)
    target = next((i for i in installments if i["status"] == "scheduled"), None)
    if target:
        retry_count = (target.get("retry_count") or 0) + 1
        db.update_installment(
            target["id"],
            status="failed",
            gateway_payment_id=payment_id,
            retry_count=retry_count,
            last_error="stripe_invoice_payment_failed",
        )

        # Schedule retry if applicable
        schedule_retry(target["id"], namespace, retry_count)

        if plan.get("external_webhook_url"):
            import asyncio
            asyncio.create_task(send_external_webhook(
                webhook_url=plan["external_webhook_url"],
                auth_token=plan.get("external_auth_token") or "",
                event_type="installment_charged_failed",
                plan_id=plan_id,
                order_id=plan.get("external_order_id") or "",
                namespace=namespace,
                payload={"installment_number": target["installment_number"], "retry_count": retry_count},
            ))

    return {"ok": True, "action": "failed", "plan_id": plan_id}


async def _handle_subscription_cancelled(subscription: dict) -> Dict:
    """customer.subscription.deleted — mark plan cancelled."""
    from storage.billing_db import BillingDB

    db = BillingDB()
    subscription_id = subscription.get("id", "")
    plan = _find_plan_by_subscription(db, subscription_id)
    if not plan:
        return {"ok": True, "action": "plan_not_found"}

    plan_id = plan["plan_id"]
    namespace = plan["namespace"]

    if plan["status"] not in ("cancelled", "completed"):
        db.update_plan_status(plan_id, namespace, "cancelled")

    return {"ok": True, "action": "cancelled", "plan_id": plan_id}


def _find_plan_by_subscription(db, subscription_id: str) -> dict | None:
    from storage.billing_db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM billing_plans WHERE gateway_plan_id = ?",
            (subscription_id,),
        ).fetchone()
    return dict(row) if row else None


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
