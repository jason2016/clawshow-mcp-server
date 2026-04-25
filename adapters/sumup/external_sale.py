"""SumUp External Sale API — Caisse 双屏推送

真实使用需要邮件 SumUp 申请 Vendor-Id:
  pos.support.uk.ie@sumup.com
Mock 模式无需申请，直接模拟响应。
"""

from .client import SumUpClient
from .types import ExternalSaleOptions, ExternalSaleResult


def create_external_sale(
    client: SumUpClient,
    options: ExternalSaleOptions
) -> ExternalSaleResult:
    """推送订单到 Caisse 收银台。"""
    lines = [
        {
            "name": item["name"],
            "quantity": item["quantity"],
            "unit_price": {
                "value": item.get("unit_price", 0),
                "currency": options.currency.upper(),
            },
        }
        for item in options.items
    ]

    response = client.post(
        "/external-sale/v0/sales",
        {
            "outlet_id": options.outlet_id,
            "lines": lines,
            "total": {
                "value": options.total_amount,
                "currency": options.currency.upper(),
            },
            "external_id": options.external_reference,
        }
    )

    return {
        "sale_id": response["id"],
        "external_id": options.external_reference,
        "status": response["status"],
        "is_mock": client.is_mock,
    }
