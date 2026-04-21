"""
Generic outbound webhook sender (ClawShow → external platform).
Standard payload v1. Retries 3×  with exponential backoff.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

CLAWSHOW_VERSION = "1.0"
MAX_RETRIES = 3
RETRY_DELAYS = [2, 5, 15]  # seconds


def send_webhook(
    webhook_url: str,
    event: str,
    plan_id: str,
    namespace: str,
    data: dict,
    auth_token: str = "",
    external_order_id: str = "",
) -> dict:
    """
    POST a standardised ClawShow event to an external webhook URL.
    Returns {"success": bool, "http_status": int, "attempts": int}.
    """
    payload = {
        "clawshow_version": CLAWSHOW_VERSION,
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan_id,
        "order_id": external_order_id,
        "namespace": namespace,
        "data": data,
    }
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"

    last_status = 0
    last_body = ""
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            resp = httpx.post(webhook_url, content=body, headers=headers, timeout=10)
            last_status = resp.status_code
            last_body = resp.text[:500]
            if resp.status_code < 300:
                logger.info("Webhook %s → %s sent (attempt %d)", event, webhook_url, attempt)
                return {"success": True, "http_status": last_status, "attempts": attempt}
            logger.warning("Webhook %s attempt %d → HTTP %d", event, attempt, last_status)
        except Exception as exc:
            logger.warning("Webhook %s attempt %d failed: %s", event, attempt, exc)
        if attempt < MAX_RETRIES:
            time.sleep(delay)

    logger.error("Webhook %s → %s failed after %d attempts", event, webhook_url, MAX_RETRIES)
    return {"success": False, "http_status": last_status, "attempts": MAX_RETRIES, "response": last_body}
