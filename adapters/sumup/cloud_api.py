"""SumUp Cloud API — Solo Printer 远程控制"""

from .client import SumUpClient
from .types import ReaderCheckoutOptions, ReaderCheckoutResult


def pair_reader(client: SumUpClient, pairing_code: str) -> str:
    """配对 Solo 设备，返回 reader_id。"""
    response = client.post("/me/readers", {"pairing_code": pairing_code})
    return response["id"]


def create_checkout_on_reader(
    client: SumUpClient,
    options: ReaderCheckoutOptions
) -> ReaderCheckoutResult:
    """让 Solo TPE 弹出金额，等客户贴卡。"""
    response = client.post(
        f"/merchants/me/readers/{options.reader_id}/checkout",
        {
            "total_amount": {
                "value": options.amount,
                "currency": options.currency.upper(),
            },
            "description": options.description,
            "return_url": options.return_url,
        }
    )

    return {
        "checkout_id": response["id"],
        "reader_id": options.reader_id,
        "status": response["status"],
        "is_mock": client.is_mock,
    }
