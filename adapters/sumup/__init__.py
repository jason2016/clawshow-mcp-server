"""SumUp Adapter — 统一入口"""

from .client import SumUpClient
from .checkout import create_hosted_checkout, get_checkout_status
from .cloud_api import pair_reader, create_checkout_on_reader
from .external_sale import create_external_sale
from .webhook import handle_sumup_webhook, verify_sumup_signature
from .types import (
    SumUpMode,
    PaymentMode,
    SumUpCheckoutOptions,
    ReaderCheckoutOptions,
    ExternalSaleOptions,
    CheckoutResult,
    ReaderCheckoutResult,
    ExternalSaleResult,
)

__all__ = [
    "SumUpClient",
    "create_hosted_checkout",
    "get_checkout_status",
    "pair_reader",
    "create_checkout_on_reader",
    "create_external_sale",
    "handle_sumup_webhook",
    "verify_sumup_signature",
    "SumUpMode",
    "PaymentMode",
    "SumUpCheckoutOptions",
    "ReaderCheckoutOptions",
    "ExternalSaleOptions",
    "CheckoutResult",
    "ReaderCheckoutResult",
    "ExternalSaleResult",
]
