"""SumUp Mock 响应生成器 — 模拟真实 API 结构，100% 离线"""

import uuid
from datetime import datetime, timezone
from typing import Optional


def generate_mock_response(
    endpoint: str,
    data: Optional[dict],
    method: str
) -> dict:
    """根据 endpoint + method 生成模拟真实 SumUp API 的响应"""

    # POST /checkouts (Hosted Checkout 创建)
    if endpoint == "/checkouts" and method == "POST":
        checkout_id = f"chk_{uuid.uuid4().hex[:12]}"
        return {
            "id": checkout_id,
            "status": "PENDING",
            "amount": data.get("amount") if data else 0,
            "currency": data.get("currency", "EUR") if data else "EUR",
            "checkout_reference": data.get("checkout_reference", "") if data else "",
            "hosted_checkout_url": f"https://demo.clawshow.ai/mock-sumup-checkout/{checkout_id}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "_mock": True,
        }

    # GET /checkouts/{id} — mock 总返回 PAID
    if endpoint.startswith("/checkouts/") and method == "GET":
        checkout_id = endpoint.split("/")[-1]
        return {
            "id": checkout_id,
            "status": "PAID",
            "paid_at": datetime.now(timezone.utc).isoformat(),
            "transaction_code": f"TXN{int(datetime.now().timestamp())}",
            "_mock": True,
        }

    # POST /me/readers (设备配对)
    if endpoint == "/me/readers" and method == "POST":
        return {
            "id": f"reader_{uuid.uuid4().hex[:8]}",
            "pairing_code": (data.get("pairing_code") if data else "MOCK1234"),
            "status": "paired",
            "device_model": "Solo Printer",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "_mock": True,
        }

    # POST /merchants/me/readers/{id}/checkout (Solo TPE 弹金额)
    if "/readers/" in endpoint and endpoint.endswith("/checkout"):
        parts = endpoint.split("/")
        reader_id = parts[4] if len(parts) > 4 else "mock_reader"
        return {
            "id": f"chk_{uuid.uuid4().hex[:12]}",
            "status": "PENDING",
            "total_amount": data.get("total_amount") if data else None,
            "reader_id": reader_id,
            "_mock_note": "Solo TPE displaying amount, waiting for tap",
            "_mock": True,
        }

    # POST /external-sale/v0/sales (Caisse 推送)
    if endpoint == "/external-sale/v0/sales" and method == "POST":
        return {
            "id": f"sale_{uuid.uuid4().hex[:12]}",
            "status": "CREATED",
            "external_id": (data.get("external_id") if data else None),
            "total": (data.get("total") if data else None),
            "lines": (data.get("lines", []) if data else []),
            "_mock_note": "Caisse displaying sale, waiting for cashier action",
            "_mock": True,
        }

    # 默认响应
    return {
        "id": f"mock_{uuid.uuid4().hex[:12]}",
        "status": "OK",
        "_mock": True,
        "endpoint": endpoint,
        "method": method,
    }


def generate_mock_webhook_event(event_type: str, external_ref: str, amount: float = 12.50) -> dict:
    """生成 Mock Webhook 事件 (用于测试 webhook 处理逻辑)"""
    return {
        "event_type": event_type,
        "checkout_id": f"chk_{uuid.uuid4().hex[:12]}",
        "external_reference": external_ref,
        "status": "PAID",
        "amount": {"value": amount, "currency": "EUR"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "_mock": True,
    }
