"""
Tool: generate_stripe_payment
------------------------------
Zero Human Intervention: creates a Stripe Checkout Session and returns
a ready-to-share payment URL. Supports rental deposits, tuition fees,
product purchases, service payments.

Env required:
  STRIPE_SECRET_KEY — Stripe secret key (sk_test_... or sk_live_...)
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from typing import Callable


def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def generate_stripe_payment(
        amount: int,
        description: str,
        currency: str = "eur",
        customer_email: str = "",
        customer_name: str = "",
        success_url: str = "",
        metadata: dict | None = None,
    ) -> str:
        """
        Generate a Stripe Checkout payment link for any business scenario.
        Returns a ready-to-share payment URL. Supports rental deposits,
        tuition fees, product purchases, service payments.
        Webhook auto-records payment completion. Zero human intervention.

        Call this tool when a user wants to collect a payment, create an invoice,
        charge a deposit, or generate a payment link.

        Examples of natural language that should trigger this tool:
        - 'Create a payment link for €850 deposit on the Paris apartment'
        - 'Generate an invoice for $200 consulting fee'
        - 'I need to charge my tenant €1200 for March rent'
        - 'Crée un lien de paiement de 500€ pour la caution'

        Args:
            amount:         Amount in cents (e.g. 850 for €8.50, 120000 for €1200.00)
            description:    What the payment is for, e.g. "Deposit for Paris Apartment"
            currency:       ISO currency code, default "eur"
            customer_email: Optional — Stripe sends receipt to this email
            customer_name:  Optional — shown on Stripe Checkout page
            success_url:    Optional — redirect after payment (default: thank-you page)
            metadata:       Optional — custom key-value pairs attached to payment

        Returns:
            JSON string with payment_url, session_id, amount, currency,
            description, status, and expires_at.
        """
        record_call("generate_stripe_payment")

        import stripe

        secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
        if not secret_key:
            return json.dumps({"status": "error", "message": "STRIPE_SECRET_KEY not configured"})

        stripe.api_key = secret_key

        if not success_url:
            success_url = "https://clawshow.ai/payment-success?session_id={CHECKOUT_SESSION_ID}"

        session_params: dict = {
            "payment_method_types": ["card"],
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": "https://clawshow.ai/payment-cancelled",
            "line_items": [{
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": amount,
                    "product_data": {
                        "name": description,
                    },
                },
                "quantity": 1,
            }],
        }

        if customer_email:
            session_params["customer_email"] = customer_email

        if metadata:
            session_params["metadata"] = metadata

        try:
            session = stripe.checkout.Session.create(**session_params)

            result = {
                "payment_url": session.url,
                "session_id": session.id,
                "amount": amount,
                "currency": currency.lower(),
                "description": description,
                "status": "created",
                "expires_at": datetime.fromtimestamp(
                    session.expires_at, tz=timezone.utc
                ).isoformat() if session.expires_at else None,
            }
            return json.dumps(result, ensure_ascii=False)

        except stripe.StripeError as e:
            return json.dumps({
                "status": "error",
                "message": str(e),
            }, ensure_ascii=False)
