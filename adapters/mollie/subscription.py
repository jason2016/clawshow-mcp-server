"""
Mollie subscription adapter.
Week 1: TEST mode only.
"""
from __future__ import annotations

import os
from typing import Dict

from mollie.api.client import Client
from adapters.mollie.customer import _get_client


def create_mollie_subscription(
    customer_id: str,
    amount: float,
    currency: str,
    interval: str,
    start_date: str,
    description: str,
    mode: str = "test",
    webhook_url: str = "",
) -> Dict:
    """
    Create a recurring subscription for a Mollie customer.
    Returns {"subscription_id", "status", "mode"}.
    """
    mollie = _get_client(mode)

    params: dict = {
        "amount": {"currency": currency.upper(), "value": f"{amount:.2f}"},
        "interval": interval,
        "startDate": start_date,
        "description": description,
    }
    if webhook_url:
        params["webhookUrl"] = webhook_url

    sub = mollie.customer_subscriptions.with_parent_id(customer_id).create(params)
    return {
        "subscription_id": sub.id,
        "status": sub.status,
        "mode": mode,
        "next_payment_date": getattr(sub, "next_payment_date", None),
    }


def get_mollie_subscription(customer_id: str, subscription_id: str, mode: str = "test") -> Dict:
    mollie = _get_client(mode)
    sub = mollie.customer_subscriptions.with_parent_id(customer_id).get(subscription_id)
    return {
        "subscription_id": sub.id,
        "status": sub.status,
        "amount": sub.amount,
        "interval": sub.interval,
        "start_date": sub.start_date,
    }


def cancel_mollie_subscription(customer_id: str, subscription_id: str, mode: str = "test") -> Dict:
    mollie = _get_client(mode)
    mollie.customer_subscriptions.with_parent_id(customer_id).delete(subscription_id)
    return {"subscription_id": subscription_id, "status": "canceled"}
