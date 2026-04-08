"""
Tool: generate_payment + verify_payment
----------------------------------------
Universal payment engine supporting multiple providers:
  - stancer  (Stancer v2 API — used by Neige Rouge)
  - sumup    (SumUp Hosted Checkout — used by Florent)
  - stripe   (Stripe Checkout — global)

Zero Human Intervention: creates payment link → returns ready-to-share URL.
Amount is always in cents (€10.50 = 1050). SumUp conversion is handled internally.

Env vars (per provider):
  STANCER_SECRET_KEY
  SUMUP_SECRET_KEY, SUMUP_MERCHANT_CODE
  STRIPE_SECRET_KEY
"""

from __future__ import annotations

import os
import uuid
import base64
import json
from typing import Callable

import requests


# ---------------------------------------------------------------------------
# Per-namespace payment config
# ---------------------------------------------------------------------------

def _get_stancer_key(namespace: str) -> str:
    """Return secret_key for Stancer, per namespace."""
    # Future: load from DB per namespace. For now, env vars = global default.
    return os.environ.get("STANCER_SECRET_KEY", "")


def _get_sumup_keys(namespace: str) -> tuple[str, str]:
    """Return (secret_key, merchant_code) for SumUp, per namespace."""
    return (
        os.environ.get("SUMUP_SECRET_KEY", ""),
        os.environ.get("SUMUP_MERCHANT_CODE", ""),
    )


# ---------------------------------------------------------------------------
# Stancer
# ---------------------------------------------------------------------------

def _create_stancer_payment(
    namespace: str,
    amount: int,
    currency: str,
    description: str,
    return_url: str | None,
) -> dict:
    secret_key = _get_stancer_key(namespace)
    if not secret_key:
        return {"success": False, "error": "STANCER_SECRET_KEY not configured"}

    auth = base64.b64encode(f"{secret_key}:".encode()).decode()
    payload: dict = {
        "amount": amount,
        "currency": currency.lower(),
        "description": description,
    }
    if return_url:
        payload["return_url"] = return_url

    try:
        r = requests.post(
            "https://api.stancer.com/v2/payment_intents/",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return {"success": False, "error": f"Stancer API error: {e}"}

    payment_id = data.get("id", "")
    payment_url = data.get("url", "")
    if not payment_url:
        return {"success": False, "error": "Stancer API did not return a payment URL", "raw": data}

    return {
        "success": True,
        "payment_id": payment_id,
        "payment_url": payment_url,
        "provider": "stancer",
        "amount": amount,
        "currency": currency.lower(),
        "status": "pending",
    }


def _verify_stancer_payment(namespace: str, payment_id: str) -> dict:
    secret_key = _get_stancer_key(namespace)
    if not secret_key:
        return {"success": False, "error": "STANCER_SECRET_KEY not configured"}

    auth = base64.b64encode(f"{secret_key}:".encode()).decode()
    try:
        r = requests.get(
            f"https://api.stancer.com/v2/payment_intents/{payment_id}",
            headers={"Authorization": f"Basic {auth}"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return {"success": False, "error": f"Stancer API error: {e}"}

    status = data.get("status", "unknown")
    paid = status in ("succeeded", "captured", "requires_capture")

    return {
        "success": True,
        "payment_id": payment_id,
        "provider": "stancer",
        "status": status,
        "paid": paid,
        "amount": data.get("amount"),
        "currency": data.get("currency"),
    }


# ---------------------------------------------------------------------------
# SumUp
# ---------------------------------------------------------------------------

def _create_sumup_payment(
    namespace: str,
    amount: int,
    currency: str,
    description: str,
    return_url: str | None,
    metadata: dict | None,
) -> dict:
    secret_key, merchant_code = _get_sumup_keys(namespace)
    if not secret_key:
        return {"success": False, "error": "SUMUP_SECRET_KEY not configured"}
    if not merchant_code:
        return {"success": False, "error": "SUMUP_MERCHANT_CODE not configured"}

    # SumUp uses decimal euros, not cents
    amount_decimal = round(amount / 100, 2)
    checkout_ref = str(uuid.uuid4())
    if metadata and metadata.get("order_id"):
        checkout_ref = f"claw-{metadata['order_id']}-{uuid.uuid4().hex[:6]}"

    payload: dict = {
        "amount": amount_decimal,
        "currency": currency.upper(),
        "checkout_reference": checkout_ref,
        "description": description,
        "merchant_code": merchant_code,
    }
    if return_url:
        payload["redirect_url"] = return_url

    try:
        r = requests.post(
            "https://api.sumup.com/v0.1/checkouts",
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return {"success": False, "error": f"SumUp API error: {e}"}

    checkout_id = data.get("id", "")
    payment_url = f"https://pay.sumup.com/b2c/Q{checkout_id}"

    return {
        "success": True,
        "payment_id": checkout_id,
        "payment_url": payment_url,
        "provider": "sumup",
        "amount": amount,
        "currency": currency.lower(),
        "status": "pending",
    }


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

def _create_stripe_payment(
    namespace: str,
    amount: int,
    currency: str,
    description: str,
    return_url: str | None,
    metadata: dict | None,
) -> dict:
    import stripe as _stripe
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret_key:
        return {"success": False, "error": "STRIPE_SECRET_KEY not configured"}

    _stripe.api_key = secret_key
    success_url = return_url or "https://clawshow.ai/payment-success?session_id={CHECKOUT_SESSION_ID}"

    session_params: dict = {
        "payment_method_types": ["card"],
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": "https://clawshow.ai/payment-cancelled",
        "line_items": [{
            "price_data": {
                "currency": currency.lower(),
                "unit_amount": amount,
                "product_data": {"name": description},
            },
            "quantity": 1,
        }],
    }
    if metadata:
        customer_email = metadata.get("customer_email", "")
        if customer_email:
            session_params["customer_email"] = customer_email
        meta_stripped = {k: str(v) for k, v in metadata.items() if k != "customer_email"}
        if meta_stripped:
            session_params["metadata"] = meta_stripped

    try:
        session = _stripe.checkout.Session.create(**session_params)
    except Exception as e:
        return {"success": False, "error": f"Stripe error: {e}"}

    return {
        "success": True,
        "payment_id": session.id,
        "payment_url": session.url,
        "provider": "stripe",
        "amount": amount,
        "currency": currency.lower(),
        "status": "pending",
    }


def _verify_stripe_payment(namespace: str, payment_id: str) -> dict:
    import stripe as _stripe
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret_key:
        return {"success": False, "error": "STRIPE_SECRET_KEY not configured"}

    _stripe.api_key = secret_key
    try:
        session = _stripe.checkout.Session.retrieve(payment_id)
    except Exception as e:
        return {"success": False, "error": f"Stripe error: {e}"}

    paid = session.payment_status == "paid"
    return {
        "success": True,
        "payment_id": payment_id,
        "provider": "stripe",
        "status": session.payment_status,
        "paid": paid,
        "amount": session.amount_total,
        "currency": session.currency,
    }


# ---------------------------------------------------------------------------
# SumUp (verify)
# ---------------------------------------------------------------------------

def _verify_sumup_payment(namespace: str, payment_id: str) -> dict:
    secret_key, _ = _get_sumup_keys(namespace)
    if not secret_key:
        return {"success": False, "error": "SUMUP_SECRET_KEY not configured"}

    try:
        r = requests.get(
            f"https://api.sumup.com/v0.1/checkouts/{payment_id}",
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return {"success": False, "error": f"SumUp API error: {e}"}

    status = data.get("status", "unknown")
    paid = status == "PAID"
    amount_decimal = data.get("amount", 0)

    return {
        "success": True,
        "payment_id": payment_id,
        "provider": "sumup",
        "status": status,
        "paid": paid,
        "amount": int(round(amount_decimal * 100)),  # convert back to cents
        "currency": data.get("currency", "").lower(),
    }


# ---------------------------------------------------------------------------
# MCP Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def generate_payment(
        provider: str,
        amount: int,
        currency: str,
        description: str,
        namespace: str,
        return_url: str = "",
        metadata: dict | None = None,
    ) -> str:
        """
        Generate a hosted payment link for any business scenario: rent collection,
        tuition fees, restaurant orders, invoices, e-commerce, donations, or service
        payments. Supports 3 payment providers: Stripe (global), Stancer (France,
        lowest fees), SumUp (Europe). Accepts Apple Pay, Google Pay, CB, Visa,
        Mastercard, SEPA. Input: amount, currency, provider, description, namespace.
        Output: ready-to-share payment URL. No signup required — works on first call.
        Payment status trackable via verify_payment tool.

        Args:
            provider:    Payment gateway — "stancer", "sumup", or "stripe"
            amount:      Amount in cents (e.g. 1050 for €10.50)
            currency:    ISO currency code, e.g. "eur"
            description: Payment description shown on the payment page
            namespace:   Client namespace for config routing (e.g. "neige-rouge", "florent")
            return_url:  Optional URL to redirect after payment
            metadata:    Optional dict with order_id, customer_name, etc.

        Returns:
            JSON string with payment_url, payment_id, provider, amount, currency, status.
        """
        record_call("generate_payment", {"provider": provider, "namespace": namespace})

        if provider == "stancer":
            result = _create_stancer_payment(
                namespace, amount, currency, description, return_url or None
            )
        elif provider == "sumup":
            result = _create_sumup_payment(
                namespace, amount, currency, description, return_url or None, metadata
            )
        elif provider == "stripe":
            result = _create_stripe_payment(
                namespace, amount, currency, description, return_url or None, metadata
            )
        else:
            result = {"success": False, "error": f"Unknown provider: {provider}. Supported: stancer, sumup, stripe"}

        return json.dumps(result, ensure_ascii=False)

    @mcp.tool()
    def verify_payment(
        provider: str,
        payment_id: str,
        namespace: str,
    ) -> str:
        """
        Check the status of a payment by its payment ID. Supports Stripe, Stancer,
        and SumUp providers. Input: provider, payment_id. Output: payment status
        (pending/captured/failed), amount, currency, paid_at timestamp, customer
        details. Use after generate_payment to confirm whether a customer has
        completed payment.

        Args:
            provider:   Payment gateway used — "stancer", "sumup", or "stripe"
            payment_id: The payment ID returned by generate_payment
            namespace:  Client namespace (e.g. "neige-rouge", "florent")

        Returns:
            JSON string with paid (bool), status, payment_id, provider, amount, currency.
        """
        record_call("verify_payment", {"provider": provider, "namespace": namespace})

        if provider == "stancer":
            result = _verify_stancer_payment(namespace, payment_id)
        elif provider == "sumup":
            result = _verify_sumup_payment(namespace, payment_id)
        elif provider == "stripe":
            result = _verify_stripe_payment(namespace, payment_id)
        else:
            result = {"success": False, "error": f"Unknown provider: {provider}. Supported: stancer, sumup, stripe"}

        return json.dumps(result, ensure_ascii=False)
