"""
Async outbound webhook sender for billing events.
Sends standardized v1 payload to external platforms (FocusingPro, etc.).
3x retry with exponential backoff. All attempts logged to billing_webhook_logs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Dict

import httpx

from storage.billing_db import BillingDB

logger = logging.getLogger(__name__)

EVENTS = {
    "plan_created",
    "plan_activated",
    "installment_scheduled",
    "installment_processing",
    "installment_charged_success",
    "installment_charged_failed",
    "installment_retry_scheduled",
    "installment_retry_failed_final",
    "plan_completed",
    "plan_cancelled",
    "plan_paused",
    "plan_resumed",
}

RETRY_DELAYS = [2, 5, 15]


async def send_external_webhook(
    webhook_url: str,
    auth_token: str | None,
    event_type: str,
    plan_id: str,
    order_id: str | None,
    namespace: str,
    payload: Dict,
    max_retries: int = 3,
) -> bool:
    """
    POST standardized v1 event to external platform.
    Returns True on success.
    """
    if event_type not in EVENTS:
        logger.error("Unknown event_type: %s", event_type)
        return False

    full_payload = {
        "clawshow_version": "1.0",
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "order_id": order_id,
        "namespace": namespace,
        "data": payload,
    }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ClawShow-Webhook/1.0",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    body = json.dumps(full_payload)
    db = BillingDB()
    db.init_tables()

    for attempt in range(max_retries):
        http_status = None
        response_body = ""
        succeeded = False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(webhook_url, content=body, headers=headers)
            http_status = resp.status_code
            response_body = resp.text[:1000]
            succeeded = 200 <= resp.status_code < 300
        except Exception as exc:
            response_body = f"Exception: {exc}"
            logger.warning("Outbound webhook attempt %d failed: %s", attempt + 1, exc)

        db.log_webhook({
            "plan_id": plan_id,
            "event_type": event_type,
            "webhook_url": webhook_url,
            "payload": body,
            "http_status": http_status,
            "response_body": response_body,
            "succeeded": succeeded,
        })

        if succeeded:
            return True

        if attempt < max_retries - 1:
            import asyncio
            await asyncio.sleep(RETRY_DELAYS[attempt])

    return False
