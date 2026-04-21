"""
Retry logic for failed installment charges.

Retry schedule: 24h, 48h, 72h (linear, max 3 retries).
Classification:
  client_fault     → retry (e.g. insufficient funds)
  client_permanent → no retry (card expired, mandate revoked)
  gateway_fault    → retry (Mollie 5xx)
  unknown          → retry (be optimistic)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from storage.billing_db import BillingDB
from engines.billing_engine.scheduler import schedule_installment_retry

logger = logging.getLogger(__name__)

RETRY_HOURS = [24, 48, 72]
NO_RETRY_CLASSIFICATIONS = {"client_permanent"}


def should_retry(classification: str, retry_count: int, max_retries: int = 3) -> bool:
    if classification in NO_RETRY_CLASSIFICATIONS:
        return False
    return retry_count < max_retries


def schedule_retry(
    installment_id: int,
    namespace: str,
    retry_count: int,
) -> datetime | None:
    """Schedule next retry. Returns retry_at or None if max reached."""
    if retry_count >= len(RETRY_HOURS):
        return None
    hours = RETRY_HOURS[retry_count]
    retry_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    schedule_installment_retry(installment_id, namespace, retry_at)
    return retry_at


async def execute_retry(installment_id: int, namespace: str) -> None:
    """Called by scheduler at retry time — attempt charge via Mollie."""
    db = BillingDB()
    db.init_tables()

    installment = db.get_installment(installment_id)
    if not installment or installment["status"] not in ("failed", "retry_processing"):
        logger.info("Retry skipped: installment %d not retryable", installment_id)
        return

    plan = db.get_plan(installment["plan_id"], namespace)
    if not plan:
        logger.error("Retry: plan not found for installment %d", installment_id)
        return

    new_count = (installment.get("retry_count") or 0) + 1
    db.update_installment(installment_id,
                          status="retry_processing",
                          retry_count=new_count,
                          last_retry_at=_now())

    try:
        from adapters.mollie.subscription import retry_failed_payment
        result = await retry_failed_payment(
            customer_id=plan["gateway_customer_id"],
            subscription_id=plan["gateway_plan_id"],
            amount=installment["amount"],
            currency=plan["currency"],
            description=f"Retry {new_count} — {plan.get('description', '')}",
            namespace=namespace,
            installment_id=installment_id,
            mode=plan.get("gateway_mode", "test"),
        )
        # Payment is now "open" in Mollie; webhook will update status when it settles
        db.update_installment(installment_id,
                              gateway_payment_id=result["payment_id"],
                              next_retry_at=None)
        logger.info("Retry payment created: installment=%d payment=%s", installment_id, result["payment_id"])
    except Exception as exc:
        logger.error("Retry failed for installment %d: %s", installment_id, exc)
        if new_count >= 3:
            db.update_installment(installment_id,
                                  status="failed",
                                  failure_reason=f"Final retry failed: {exc}")
        else:
            db.update_installment(installment_id, status="failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
