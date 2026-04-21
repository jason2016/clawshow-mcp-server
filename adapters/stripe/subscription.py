"""
Stripe subscription adapter — Week 3.
Flow: Product → Price → Subscription (3 steps).
Phase 1: TEST mode only (IESIG sandbox).
"""
from __future__ import annotations

import logging
from typing import Dict

from adapters.stripe.customer import _get_stripe

logger = logging.getLogger(__name__)

_INTERVAL_MAP = {
    "monthly": ("month", 1),
    "quarterly": ("month", 3),
    "weekly": ("week", 1),
}


def create_stripe_subscription(
    customer_id: str,
    amount: float,
    currency: str,
    frequency: str,
    start_date: str,
    description: str,
    namespace: str,
    plan_id: str,
    installments: int,
    mode: str = "test",
    webhook_url: str = "",
) -> Dict:
    """
    Create a Stripe subscription via Product → Price → Subscription.
    installments=-1 means infinite. installments=N means cancel_at is computed externally.
    Returns {subscription_id, status, mode, product_id, price_id}.
    """
    stripe = _get_stripe(mode)

    # 1. Create Product
    product = stripe.Product.create(
        name=description or f"ClawShow {namespace} plan",
        metadata={"namespace": namespace, "plan_id": plan_id},
    )

    # 2. Create Price
    interval, interval_count = _INTERVAL_MAP.get(frequency, ("month", 1))
    price_params = {
        "unit_amount": round(amount * 100),  # cents
        "currency": currency.lower(),
        "recurring": {
            "interval": interval,
            "interval_count": interval_count,
        },
        "product": product.id,
        "metadata": {"namespace": namespace, "plan_id": plan_id},
    }
    price = stripe.Price.create(**price_params)

    # 3. Create Subscription
    sub_params: dict = {
        "customer": customer_id,
        "items": [{"price": price.id}],
        "description": description,
        "metadata": {"namespace": namespace, "plan_id": plan_id, "installments": str(installments)},
        "collection_method": "charge_automatically",
        "expand": ["latest_invoice.payment_intent"],
    }
    if webhook_url:
        # Stripe doesn't support per-subscription webhooks; webhook is global
        pass

    subscription = stripe.Subscription.create(**sub_params)

    return {
        "subscription_id": subscription.id,
        "status": subscription.status,
        "mode": mode,
        "product_id": product.id,
        "price_id": price.id,
    }


def cancel_stripe_subscription(
    subscription_id: str,
    mode: str = "test",
) -> Dict:
    """Cancel a Stripe subscription immediately."""
    stripe = _get_stripe(mode)
    sub = stripe.Subscription.cancel(subscription_id)
    return {"subscription_id": sub.id, "status": sub.status}


async def retry_stripe_payment(
    customer_id: str,
    subscription_id: str,
    amount: float,
    currency: str,
    description: str,
    namespace: str,
    installment_id: int,
    mode: str = "test",
) -> Dict:
    """
    Retry a failed Stripe payment by creating a one-off invoice.
    Returns {payment_id, status}.
    """
    stripe = _get_stripe(mode)
    invoice = stripe.Invoice.create(
        customer=customer_id,
        description=description,
        metadata={
            "namespace": namespace,
            "installment_id": installment_id,
            "subscription_id": subscription_id,
            "is_retry": "true",
        },
        auto_advance=True,
    )
    stripe.InvoiceItem.create(
        customer=customer_id,
        invoice=invoice.id,
        amount=round(amount * 100),
        currency=currency.lower(),
        description=description,
    )
    finalized = stripe.Invoice.finalize_invoice(invoice.id)
    paid = stripe.Invoice.pay(finalized.id)
    return {"payment_id": paid.payment_intent or paid.id, "status": paid.status}
