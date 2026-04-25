"""SumUp Hosted Checkout (在线支付)"""

from .client import SumUpClient
from .types import SumUpCheckoutOptions, CheckoutResult


def create_hosted_checkout(
    client: SumUpClient,
    options: SumUpCheckoutOptions
) -> CheckoutResult:
    """创建 SumUp Hosted Checkout，返回 hosted_checkout_url。"""
    response = client.post("/checkouts", {
        "amount": options.amount,
        "currency": options.currency.upper(),
        "checkout_reference": options.checkout_reference,
        "pay_to_email": options.pay_to_email,
        "return_url": options.return_url,
        "hosted_checkout": {"enabled": True},
    })

    return {
        "checkout_id": response["id"],
        "hosted_checkout_url": response.get("hosted_checkout_url"),
        "status": response["status"],
        "is_mock": client.is_mock,
    }


def get_checkout_status(client: SumUpClient, checkout_id: str) -> dict:
    """查询 checkout 状态。"""
    return client.get(f"/checkouts/{checkout_id}")
