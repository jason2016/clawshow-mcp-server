"""SumUp Webhook 处理 + HMAC-SHA256 签名验证

Mock 模式下跳过签名校验但保留验证代码。
"""

import os
import hmac
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def verify_sumup_signature(payload: bytes, signature: str, secret: str) -> bool:
    """验证 SumUp Webhook HMAC-SHA256 签名。"""
    if not signature or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def handle_sumup_webhook(
    namespace: str,
    raw_payload: bytes,
    parsed_body: dict,
    signature: Optional[str],
    webhook_secret: str,
) -> dict:
    """
    处理 SumUp 入站 Webhook。

    流程:
      1. 签名验证 (mock 模式跳过)
      2. 二次状态确认 (mock 直接信任)
      3. 更新订单 payment_status
      4. 写 webhook_logs 审计记录

    返回 {"received": True, "mock": bool} 或 {"error": ..., "_status": 401}
    """
    sumup_mode = os.getenv("SUMUP_MODE", "mock")

    # 1. 签名验证
    if sumup_mode != "mock":
        if not verify_sumup_signature(raw_payload, signature or "", webhook_secret):
            logger.warning(f"[{namespace}] Invalid SumUp webhook signature")
            return {"error": "Invalid signature", "_status": 401}
    else:
        logger.info(f"[{namespace}] [MOCK] Webhook signature check skipped")

    event = parsed_body
    verified_status = event.get("status")

    # 2. 二次验证 (mock 直接信任 body 内容)
    if sumup_mode != "mock":
        from .client import SumUpClient
        from .checkout import get_checkout_status
        api_key = os.getenv("SUMUP_API_KEY", "")
        client = SumUpClient(api_key=api_key, mode=sumup_mode)
        try:
            checkout = get_checkout_status(client, event.get("checkout_id", ""))
            verified_status = checkout.get("status")
        except Exception as exc:
            logger.error(f"[{namespace}] Failed to verify checkout status: {exc}")

    # 3. 更新订单
    if verified_status == "PAID":
        ext_ref = event.get("external_reference", "")
        _mark_order_paid(namespace, ext_ref)

    # 4. 写日志
    _write_webhook_log(
        namespace=namespace,
        provider="sumup",
        event_type=event.get("event_type", "payment"),
        payload_str=raw_payload.decode("utf-8", errors="replace"),
        signature_valid=True,
        is_mock=(sumup_mode == "mock"),
    )

    return {"received": True, "mock": sumup_mode == "mock"}


def _mark_order_paid(namespace: str, external_reference: str) -> None:
    """
    order_id는 external_reference로 전달됩니다.
    external_reference 형식: str(integer order_id) 또는 "nr-{order_id}"
    """
    try:
        from db import update_dine_order_payment_status
        # Parse integer order_id from external_reference
        # Support formats: "42", "nr-42"
        ref = external_reference.lstrip("nr-").strip()
        order_id = int(ref)
        result = update_dine_order_payment_status(namespace, order_id, "paid")
        if result.get("success"):
            logger.info(f"[{namespace}] Order {order_id} marked paid via SumUp webhook")
        else:
            logger.warning(f"[{namespace}] Failed to mark order {order_id} paid: {result.get('error')}")
    except (ValueError, TypeError) as exc:
        logger.error(f"[{namespace}] Cannot parse order_id from external_reference='{external_reference}': {exc}")
    except Exception as exc:
        logger.error(f"[{namespace}] Error marking order paid: {exc}", exc_info=True)


def _write_webhook_log(
    namespace: str,
    provider: str,
    event_type: str,
    payload_str: str,
    signature_valid: bool,
    is_mock: bool,
) -> None:
    try:
        from db import write_webhook_log
        write_webhook_log(
            namespace=namespace,
            provider=provider,
            event_type=event_type,
            payload=payload_str,
            signature_valid=signature_valid,
            processed=True,
            is_mock=is_mock,
        )
    except Exception as exc:
        logger.error(f"Failed to write webhook log: {exc}")
