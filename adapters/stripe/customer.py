"""
Stripe customer adapter — Week 3.
IESIG sandbox (TEST mode only in Phase 1).
"""
from __future__ import annotations

import os
from typing import Dict


def _get_stripe(mode: str = "test"):
    import stripe
    if mode == "test":
        key = os.environ.get("STRIPE_API_KEY_IESIG_TEST", "")
    else:
        key = os.environ.get("STRIPE_API_KEY_IESIG_LIVE", "")
    if not key:
        raise RuntimeError(f"STRIPE_API_KEY_IESIG_{mode.upper()} not configured")
    stripe.api_key = key
    return stripe


def create_stripe_customer(
    email: str,
    name: str,
    phone: str = "",
    metadata: dict | None = None,
    mode: str = "test",
) -> Dict:
    """Create a Stripe customer. Returns {customer_id, mode}."""
    stripe = _get_stripe(mode)
    params = {
        "email": email,
        "name": name,
        "metadata": metadata or {},
    }
    if phone:
        params["phone"] = phone
    customer = stripe.Customer.create(**params)
    return {"customer_id": customer.id, "mode": mode}
