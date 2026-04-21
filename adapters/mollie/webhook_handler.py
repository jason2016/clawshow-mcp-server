"""
Mollie inbound webhook handler (async, Week 2).

Mollie POSTs {"id": "tr_xxx"} when payment status changes.
Flow:
  paid    → update installment "charged" + commission + external webhook + rolling schedule
  failed  → update installment "failed" + classify + schedule retry + external webhook
  pending → no-op
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict

from adapters.mollie.customer import _get_client
from storage.billing_db import BillingDB

logger = logging.getLogger(__name__)


async def handle_mollie_webhook(payment_id: str) -> Dict:
    """Main entry point. Called from POST /webhooks/mollie."""

    # Detect mode: try test first, then live
    payment = None
    used_mode = "test"
    for mode in ("test", "live"):
        try:
            mollie = _get_client(mode)
            payment = mollie.payments.get(payment_id)
            used_mode = mode
            break
        except Exception:
            continue

    if payment is None:
        logger.error("Could not fetch payment %s from Mollie", payment_id)
        return {"success": False, "error": f"payment {payment_id} not found"}

    subscription_id = payment._get_property("subscriptionId")
    customer_id = payment._get_property("customerId")
    status = payment.status

    logger.info("Mollie webhook: %s status=%s sub=%s", payment_id, status, subscription_id)

    if not subscription_id:
        # Not a subscription payment — log and return
        return {"success": True, "action_taken": "not_a_subscription_payment"}

    db = BillingDB()
    db.init_tables()

    # Find plan by subscription ID
    plan = _find_plan_by_subscription(db, subscription_id)
    if not plan:
        logger.warning("No plan found for subscription %s", subscription_id)
        return {"success": False, "error": f"no plan for subscription {subscription_id}"}

    namespace = plan["namespace"]

    # Find or match installment
    installment = _find_installment(db, plan["plan_id"], payment_id)

    if status == "paid":
        return await _handle_paid(db, plan, installment, payment_id, namespace)
    elif status == "failed":
        return await _handle_failed(db, plan, installment, payment_id, namespace, payment)
    else:
        return {"success": True, "action_taken": f"no_action_status_{status}"}


async def _handle_paid(db: BillingDB, plan: dict, installment: dict | None, payment_id: str, namespace: str) -> Dict:
    now = _now()

    # Idempotency: if already charged with this payment_id, skip
    if installment and installment.get("status") == "charged" and installment.get("gateway_payment_id") == payment_id:
        logger.info("Duplicate paid webhook for %s — skipping", payment_id)
        return {"success": True, "action_taken": "duplicate_skipped", "payment_id": payment_id}

    if installment:
        db.update_installment(installment["id"],
                              status="charged",
                              gateway_payment_id=payment_id,
                              charged_at=now)
        installment_id = installment["id"]
        installment_number = installment.get("installment_number", 1)
        amount = installment["amount"]
    else:
        # Payment arrived but no installment row — create one
        installment_id = None
        installment_number = db.count_charged_installments(plan["plan_id"]) + 1
        amount = plan["total_amount"] if plan["installments"] == -1 else plan["total_amount"]

    # Record commission (0.5% default)
    commission_amount = round(float(amount) * 0.005, 4)
    db.record_commission({
        "namespace": namespace,
        "plan_id": plan["plan_id"],
        "installment_id": installment_id,
        "transaction_amount": amount,
        "commission_rate": 0.005,
        "commission_amount": commission_amount,
    })

    # Rolling schedule for infinite subscriptions
    if plan["installments"] == -1:
        db.generate_next_preview_installment(plan["plan_id"])

    # Check plan completion for fixed installments
    if plan["installments"] > 0:
        charged = db.count_charged_installments(plan["plan_id"])
        if charged >= plan["installments"]:
            db.update_plan_status(plan["plan_id"], namespace, "completed")
            _fire_external_webhook(plan, namespace, "plan_completed", {
                "completed_at": now,
                "total_charged": plan["total_amount"],
            })
            return {"success": True, "action_taken": "plan_completed", "plan_id": plan["plan_id"]}

    # External webhook
    _fire_external_webhook(plan, namespace, "installment_charged_success", {
        "installment_number": installment_number,
        "amount": amount,
        "charged_at": now,
        "mollie_payment_id": payment_id,
    })

    return {
        "success": True,
        "action_taken": "payment_recorded_success",
        "installment_id": installment_id,
        "plan_id": plan["plan_id"],
        "commission_earned": commission_amount,
    }


async def _handle_failed(db: BillingDB, plan: dict, installment: dict | None, payment_id: str, namespace: str, payment) -> Dict:
    failure_reason = _get_failure_reason(payment)
    classification = _classify_failure(failure_reason)
    now = _now()

    retry_count = (installment.get("retry_count") or 0) if installment else 0

    if installment:
        db.update_installment(installment["id"],
                              status="failed",
                              gateway_payment_id=payment_id,
                              failure_reason=failure_reason,
                              failure_classification=classification)
        installment_id = installment["id"]
    else:
        installment_id = None

    # Schedule retry
    retry_at = None
    from engines.billing_engine.retry_manager import should_retry, schedule_retry
    if installment_id and should_retry(classification, retry_count, plan.get("max_retries", 3)):
        retry_at_dt = schedule_retry(installment_id, namespace, retry_count)
        if retry_at_dt:
            retry_at = retry_at_dt.isoformat()
            db.update_installment(installment_id, next_retry_at=retry_at)

    # External webhook
    _fire_external_webhook(plan, namespace, "installment_charged_failed", {
        "installment_id": installment_id,
        "failure_reason": failure_reason,
        "classification": classification,
        "retry_scheduled": retry_at,
        "mollie_payment_id": payment_id,
    })

    return {
        "success": True,
        "action_taken": "payment_failed_recorded",
        "installment_id": installment_id,
        "classification": classification,
        "retry_scheduled": retry_at,
    }


def _fire_external_webhook(plan: dict, namespace: str, event: str, data: dict) -> None:
    """Fire-and-forget: schedule as background task if external_webhook_url set."""
    if not plan.get("external_webhook_url"):
        return
    import asyncio
    from engines.billing_engine.webhook_sender import send_external_webhook
    asyncio.create_task(send_external_webhook(
        webhook_url=plan["external_webhook_url"],
        auth_token=plan.get("external_auth_token"),
        event_type=event,
        plan_id=plan["plan_id"],
        order_id=plan.get("external_order_id"),
        namespace=namespace,
        payload=data,
    ))


def _find_plan_by_subscription(db: BillingDB, subscription_id: str) -> dict | None:
    with db.get_conn_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM billing_plans WHERE gateway_plan_id = ?", (subscription_id,)
        ).fetchone()
    return dict(row) if row else None


def _find_installment(db: BillingDB, plan_id: str, payment_id: str) -> dict | None:
    # Try by payment_id first (retry payments)
    inst = db.get_installment_by_gateway_payment(payment_id)
    if inst:
        return inst
    # Fall back to earliest scheduled installment
    with db.get_conn_ctx() as conn:
        row = conn.execute(
            """SELECT * FROM billing_installments
               WHERE plan_id = ? AND status = 'scheduled'
               ORDER BY installment_number LIMIT 1""",
            (plan_id,),
        ).fetchone()
    return dict(row) if row else None


def _get_failure_reason(payment) -> str:
    try:
        details = payment._get_property("details") or {}
        return details.get("failureReason", "unknown")
    except Exception:
        return "unknown"


def _classify_failure(reason: str) -> str:
    r = (reason or "").lower()
    if any(x in r for x in ["card_expired", "card_blocked", "mandate_revoked", "account_closed", "do_not_honor"]):
        return "client_permanent"
    if any(x in r for x in ["insufficient_funds", "card_declined", "generic_decline"]):
        return "client_fault"
    if any(x in r for x in ["gateway", "processor", "timeout"]):
        return "gateway_fault"
    return "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
