"""
Mollie inbound webhook handler.
Mollie POSTs { id: "tr_xxx" } to our endpoint.
We fetch the payment, update installment status in DB.
"""
from __future__ import annotations

import logging
from typing import Dict

from adapters.mollie.customer import _get_client
from storage.billing_db import BillingDB

logger = logging.getLogger(__name__)


def handle_mollie_webhook(payment_id: str) -> Dict:
    """
    Fetch payment from Mollie, detect mode, update installment in DB.
    Returns structured result for logging.
    """
    # Detect mode from payment ID prefix (tr_ = both modes; use test key first)
    for mode in ("test", "live"):
        try:
            mollie = _get_client(mode)
            payment = mollie.payments.get(payment_id)
            break
        except Exception:
            payment = None
            continue

    if payment is None:
        logger.error("handle_mollie_webhook: could not fetch %s", payment_id)
        return {"payment_id": payment_id, "status": "error", "error": "payment not found"}

    subscription_id = getattr(payment, "subscription_id", None)
    customer_id = getattr(payment, "customer_id", None)
    status = payment.status

    logger.info(
        "Mollie webhook: payment=%s status=%s subscription=%s",
        payment_id, status, subscription_id,
    )

    # Update installment in DB if we can match by gateway_payment_id or subscription
    if subscription_id:
        try:
            db = BillingDB()
            db.init_tables()
            db.update_installment_by_gateway_payment(
                gateway_payment_id=payment_id,
                subscription_id=subscription_id,
                status=_map_status(status),
            )
        except Exception as exc:
            logger.warning("DB update failed for payment %s: %s", payment_id, exc)

    return {
        "payment_id": payment_id,
        "status": status,
        "subscription_id": subscription_id,
        "customer_id": customer_id,
    }


def _map_status(mollie_status: str) -> str:
    return {
        "paid": "charged",
        "failed": "failed",
        "canceled": "failed",
        "expired": "failed",
        "pending": "scheduled",
        "open": "scheduled",
    }.get(mollie_status, "scheduled")
