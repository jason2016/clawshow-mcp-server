"""
Mollie mandate adapter.
In TEST mode we create a SEPA direct debit test mandate so subscriptions can be created.
In LIVE mode, mandates are created automatically via a first checkout payment.
"""
from __future__ import annotations

import datetime
from typing import Dict

from adapters.mollie.customer import _get_client


def create_test_mandate(customer_id: str, consumer_name: str) -> Dict:
    """Create a test SEPA mandate so subscriptions can be created in TEST mode."""
    mollie = _get_client("test")
    customer = mollie.customers.get(customer_id)
    mandate = customer.mandates.create({
        "method": "directdebit",
        "consumerName": consumer_name or "Test Consumer",
        "consumerAccount": "NL55INGB0000000000",
        "consumerBic": "INGBNL2A",
        "signatureDate": datetime.date.today().isoformat(),
        "mandateReference": f"clawshow-test-{customer_id}",
    })
    return {"mandate_id": mandate.id, "status": mandate.status, "method": mandate.method}


def list_mandates(customer_id: str, mode: str = "test") -> list[Dict]:
    mollie = _get_client(mode)
    customer = mollie.customers.get(customer_id)
    return [{"mandate_id": m.id, "status": m.status, "method": m.method} for m in customer.mandates.list()]


def get_mandate(customer_id: str, mandate_id: str, mode: str = "test") -> Dict:
    mollie = _get_client(mode)
    customer = mollie.customers.get(customer_id)
    m = customer.mandates.get(mandate_id)
    return {"mandate_id": m.id, "status": m.status, "method": m.method}
