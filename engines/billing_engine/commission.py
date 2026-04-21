"""
Commission calculation for ClawShow Billing.
Rates per Pricing v3 Final (2026-04-20).
Only charged on successful payment delivery (Pay-for-Outcome).
"""
from __future__ import annotations

# Commission rates by plan tier — keyed by namespace prefix or explicit tier
# Default: 0.5% (Team plan)
DEFAULT_RATE = 0.005

TIER_RATES = {
    "personal": 0.005,   # Personal plan: no Billing commission (eSign only), reuse 0.5% as default
    "team":     0.005,   # 0.5%
    "business": 0.003,   # 0.3%
    "enterprise": 0.002, # 0.2%
}


def calculate_commission(amount: float, namespace: str, tier: str = "team") -> dict:
    """
    Return {"rate": float, "amount": float} for a successful transaction.
    """
    rate = TIER_RATES.get(tier.lower(), DEFAULT_RATE)
    commission = round(amount * rate, 4)
    return {
        "rate": rate,
        "amount": commission,
        "namespace": namespace,
    }
