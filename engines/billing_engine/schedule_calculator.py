"""
Calculate installment payment dates for a billing plan.
Handles monthly, quarterly, weekly, and one_time frequencies.
"""
from __future__ import annotations

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


def calculate_schedule(
    start_date: date,
    installments: int,
    frequency: str,
    amount_per_installment: float,
) -> list[dict]:
    """
    Return list of installment dicts:
    [{"installment_number": 1, "scheduled_date": "YYYY-MM-DD", "amount": float, "status": "scheduled"}, ...]

    installments=-1 means infinite subscription — return first 12 for preview.
    """
    preview_count = installments if installments > 0 else 12

    schedule = []
    current = start_date

    for i in range(1, preview_count + 1):
        schedule.append({
            "installment_number": i,
            "scheduled_date": current.isoformat(),
            "amount": round(amount_per_installment, 2),
            "status": "scheduled",
        })
        current = _next_date(current, frequency)

    return schedule


def _next_date(d: date, frequency: str) -> date:
    if frequency == "monthly":
        return d + relativedelta(months=1)
    if frequency == "quarterly":
        return d + relativedelta(months=3)
    if frequency == "weekly":
        return d + timedelta(weeks=1)
    if frequency == "one_time":
        return d  # only one installment
    raise ValueError(f"Unknown frequency: {frequency}")


def installment_amount(total: float, installments: int) -> float:
    """Per-installment amount. For subscriptions (installments=-1) total IS the recurring amount."""
    if installments <= 0:
        return total
    return round(total / installments, 2)
