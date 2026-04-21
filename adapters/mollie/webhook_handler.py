"""
Mollie inbound webhook handler.
Mollie POSTs payment IDs to our webhook endpoint.
We fetch the payment to get actual status.

Week 1: stub — full integration in Week 2 with APScheduler.
"""
from __future__ import annotations

import logging
from typing import Dict

from adapters.mollie.customer import _get_client

logger = logging.getLogger(__name__)


def handle_mollie_webhook(payment_id: str, mode: str = "test") -> Dict:
    """
    Fetch payment status from Mollie and return structured result.
    Called when Mollie POSTs to our webhook endpoint.
    """
    try:
        mollie = _get_client(mode)
        payment = mollie.payments.get(payment_id)
        return {
            "payment_id": payment.id,
            "status": payment.status,
            "amount": payment.amount,
            "subscription_id": getattr(payment, "subscription_id", None),
            "customer_id": getattr(payment, "customer_id", None),
            "metadata": getattr(payment, "metadata", {}),
        }
    except Exception as exc:
        logger.error("handle_mollie_webhook failed for %s: %s", payment_id, exc)
        return {"payment_id": payment_id, "status": "error", "error": str(exc)}
