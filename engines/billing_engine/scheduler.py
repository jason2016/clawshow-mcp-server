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


def start_unsigned_contract_cleanup() -> None:
    """
    Schedule a daily job to auto-cancel plans stuck in pending_signature for 30+ days.
    Runs at 02:00 UTC every day.
    """
    from apscheduler.triggers.cron import CronTrigger
    s = get_scheduler()
    job_id = "cleanup_unsigned_contracts"
    # Remove if already exists (prevents duplicate on restart)
    try:
        s.remove_job(job_id)
    except Exception:
        pass
    s.add_job(
        func=_run_unsigned_contract_cleanup,
        trigger=CronTrigger(hour=2, minute=0),
        id=job_id,
        replace_existing=True,
    )
    logger.info("Unsigned contract cleanup scheduled (daily at 02:00 UTC)")


def _run_installment_retry(installment_id: int, namespace: str) -> None:
    """Executed by BackgroundScheduler (thread context) at retry time."""
    from engines.billing_engine.retry_manager import execute_retry
    asyncio.run(execute_retry(installment_id=installment_id, namespace=namespace))


def _run_unsigned_contract_cleanup() -> None:
    """
    Cancel plans stuck in pending_signature for > 30 days.
    Runs daily. Logs but never raises.
    """
    try:
        from datetime import datetime, timezone, timedelta
        from storage.billing_db import get_conn

        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cancelled = []
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT plan_id, namespace FROM billing_plans
                   WHERE status = 'pending_signature'
                   AND created_at < ?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE billing_plans SET status='cancelled', updated_at=? WHERE plan_id=? AND namespace=?",
                    (datetime.now(timezone.utc).isoformat(), row["plan_id"], row["namespace"]),
                )
                conn.execute(
                    "UPDATE billing_installments SET status='cancelled' WHERE plan_id=? AND status='scheduled'",
                    (row["plan_id"],),
                )
                cancelled.append(row["plan_id"])
        if cancelled:
            logger.info("Unsigned contract cleanup: cancelled %d plans: %s", len(cancelled), cancelled)
    except Exception as exc:
        logger.error("Unsigned contract cleanup error: %s", exc)
