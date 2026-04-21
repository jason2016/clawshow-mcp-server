"""
APScheduler singleton for ClawShow Billing.

Uses BackgroundScheduler (thread-based) — works outside async context.
Async retry functions are run via asyncio.run() in the background thread.

Used for:
  ✅ Scheduling retries of failed charges (24h/48h/72h delay)
  ✅ Retrying failed outbound webhooks
  ❌ NOT for triggering first charges (Mollie does that)
  ❌ NOT for regular subscription schedule (Mollie does that)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
    return _scheduler


def start_scheduler() -> None:
    s = get_scheduler()
    if not s.running:
        s.start()
        logger.info("BillingScheduler started")


def schedule_installment_retry(
    installment_id: int,
    namespace: str,
    retry_at: datetime,
) -> None:
    s = get_scheduler()
    job_id = f"retry_installment_{installment_id}"
    s.add_job(
        func=_run_installment_retry,
        trigger=DateTrigger(run_date=retry_at),
        args=[installment_id, namespace],
        id=job_id,
        replace_existing=True,
    )
    logger.info("Retry scheduled: installment=%d at=%s", installment_id, retry_at)


def cancel_installment_retry(installment_id: int) -> None:
    try:
        get_scheduler().remove_job(f"retry_installment_{installment_id}")
    except Exception:
        pass


def _run_installment_retry(installment_id: int, namespace: str) -> None:
    """Executed by BackgroundScheduler (thread context) at retry time."""
    from engines.billing_engine.retry_manager import execute_retry
    asyncio.run(execute_retry(installment_id=installment_id, namespace=namespace))
