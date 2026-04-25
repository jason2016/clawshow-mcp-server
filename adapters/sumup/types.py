"""SumUp 类型定义"""

from typing import TypedDict, Literal, Optional, List
from dataclasses import dataclass, field

SumUpMode = Literal["mock", "sandbox", "live"]
PaymentMode = Literal["online", "in_person_solo", "in_person_caisse", "cash"]


class CheckoutResult(TypedDict):
    checkout_id: str
    hosted_checkout_url: Optional[str]
    status: str
    is_mock: bool


class ReaderCheckoutResult(TypedDict):
    checkout_id: str
    reader_id: str
    status: str
    is_mock: bool


class ExternalSaleResult(TypedDict):
    sale_id: str
    external_id: str
    status: str
    is_mock: bool


@dataclass
class SumUpCheckoutOptions:
    amount: float          # euros (SumUp uses decimal euros)
    currency: str
    checkout_reference: str
    pay_to_email: str
    return_url: str


@dataclass
class ReaderCheckoutOptions:
    reader_id: str
    amount: float          # euros
    currency: str
    description: str
    return_url: str


@dataclass
class ExternalSaleOptions:
    outlet_id: str
    items: List[dict]      # [{"name": str, "quantity": int, "unit_price": float}]
    total_amount: float    # euros
    currency: str
    external_reference: str
