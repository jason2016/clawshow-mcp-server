"""
Mollie customer adapter.
Week 1: TEST mode only. MOLLIE_API_KEY_TEST required.
"""
from __future__ import annotations

import os
from typing import Dict

from mollie.api.client import Client


def _get_client(mode: str = "test") -> Client:
    if mode == "test":
        key = os.environ.get("MOLLIE_API_KEY_TEST")
        if not key:
            raise RuntimeError(
                "MOLLIE_API_KEY_TEST not set. "
                "Set it in .env before using the billing module."
            )
    elif mode == "live":
        key = os.environ.get("MOLLIE_API_KEY_LIVE")
        if not key:
            raise RuntimeError("MOLLIE_API_KEY_LIVE not set")
    else:
        raise ValueError(f"Unknown mode: {mode}")
    mollie = Client()
    mollie.set_api_key(key)
    return mollie


def create_mollie_customer(
    email: str,
    name: str,
    mode: str = "test",
    metadata: dict | None = None,
) -> Dict:
    """Create a Mollie customer. Returns {"customer_id", "mode", "email", "name"}."""
    mollie = _get_client(mode)
    customer = mollie.customers.create({
        "email": email,
        "name": name,
        "metadata": metadata or {},
    })
    return {
        "customer_id": customer.id,
        "mode": mode,
        "email": customer.email,
        "name": customer.name,
    }


def get_mollie_customer(customer_id: str, mode: str = "test") -> Dict:
    mollie = _get_client(mode)
    customer = mollie.customers.get(customer_id)
    return {
        "customer_id": customer.id,
        "email": customer.email,
        "name": customer.name,
    }
