"""
Determine if a billing operation succeeded, and classify failures.
Pay-for-Outcome: commission only on confirmed success.
"""
from __future__ import annotations


# Mollie / Stripe terminal statuses
_GATEWAY_SUCCESS = {"paid", "succeeded", "active", "authorized"}
_GATEWAY_FAILED = {"failed", "expired", "canceled", "requires_payment_method"}
_GATEWAY_PENDING = {"open", "pending", "processing", "requires_action"}


def detect_success(gateway_status: str) -> bool:
    return gateway_status.lower() in _GATEWAY_SUCCESS


def classify_failure(gateway_status: str, error_code: str = "") -> str:
    """
    Returns one of: 'client' | 'gateway' | 'clawshow'
    Used to decide whether to charge commission and who bears retry cost.
    """
    code = (error_code or "").lower()

    # Client-side failures (card issues, insufficient funds)
    client_codes = {"insufficient_funds", "card_declined", "expired_card", "do_not_honor",
                    "incorrect_cvc", "invalid_account"}
    if any(c in code for c in client_codes):
        return "client"

    # Gateway failures (temporary outage)
    if gateway_status in {"failed", "expired"} and not code:
        return "gateway"

    # Default: assume gateway or unknown
    return "gateway"
