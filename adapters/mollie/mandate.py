"""
Mollie mandate adapter.
Mandates authorise recurring charges. Week 1: stub — Mollie creates mandates
automatically on first subscription charge in test mode.
"""
from __future__ import annotations

from typing import Dict

from adapters.mollie.customer import _get_client


def list_mandates(customer_id: str, mode: str = "test") -> list[Dict]:
    mollie = _get_client(mode)
    mandates = mollie.customer_mandates.with_parent_id(customer_id).list()
    return [{"mandate_id": m.id, "status": m.status, "method": m.method} for m in mandates]


def get_mandate(customer_id: str, mandate_id: str, mode: str = "test") -> Dict:
    mollie = _get_client(mode)
    m = mollie.customer_mandates.with_parent_id(customer_id).get(mandate_id)
    return {"mandate_id": m.id, "status": m.status, "method": m.method}
