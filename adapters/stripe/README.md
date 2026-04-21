# Stripe Adapter

**Status**: ✅ MVP Complete (Week 4, 2026-04-21)

## Overview

Integrates ClawShow Billing with Stripe.
Currently used for IESIG sandbox (TEST mode only in Phase 1).

## Files

| File | Purpose |
|------|---------|
| `customer.py` | Create Stripe customers |
| `subscription.py` | Create / cancel subscriptions, retry payments |
| `webhook_handler.py` | Handle inbound Stripe billing webhook events |

## Authentication

```
TEST:  STRIPE_API_KEY_IESIG_TEST=sk_test_xxx
TEST:  STRIPE_WEBHOOK_SECRET_IESIG_TEST=whsec_xxx
LIVE:  STRIPE_API_KEY_IESIG_LIVE=sk_live_xxx    (Phase 2)
LIVE:  STRIPE_WEBHOOK_SECRET_IESIG_LIVE=...     (Phase 2)
```

Mode is determined by `core.config.get_gateway_mode(namespace, "stripe")`.

## Key Difference vs Mollie

| Aspect | Mollie | Stripe |
|--------|--------|--------|
| Subscription creation | 1 call (amount + interval) | 3 calls (Product → Price → Subscription) |
| Webhook payload | `{"id": "tr_xxx"}` then fetch | Full event object in body |
| Webhook security | IP allowlist | Signature (`Stripe-Signature` header) |
| Mandate | SEPA, created explicitly | PaymentMethod, attached to customer |
| Infinite sub | `times` absent from params | No `cancel_at` param |

## 3-Step Subscription Creation

```python
# 1. Product (represents the service)
product = stripe.Product.create(name="ClawShow plan", metadata={...})

# 2. Price (amount + interval, linked to product)
price = stripe.Price.create(
    unit_amount=99_00,  # cents
    currency="eur",
    recurring={"interval": "month"},
    product=product.id,
)

# 3. Subscription (links customer to price)
subscription = stripe.Subscription.create(
    customer=customer_id,
    items=[{"price": price.id}],
)
```

## Webhook Signature Verification

**Always verify.** Without it, anyone can call `/webhooks/stripe`.

```python
event = stripe.Webhook.construct_event(
    payload_bytes, stripe_signature_header, webhook_secret
)
```

## Handled Events (from 18 configured)

| Event | Action |
|-------|--------|
| `invoice.payment_succeeded` | Mark installment charged + commission |
| `invoice.payment_failed` | Mark failed + schedule retry |
| `customer.subscription.deleted` | Mark plan cancelled |
| All others | Acknowledged, ignored |

**Idempotency**: duplicate payment events for the same `payment_id` are silently skipped.

## Methods

| Function | Description |
|----------|-------------|
| `create_stripe_customer` | POST /v1/customers |
| `create_stripe_subscription` | Product + Price + Subscription (3 calls) |
| `cancel_stripe_subscription` | DELETE /v1/subscriptions/{id} |
| `retry_stripe_payment` | Create invoice + item + finalize + pay |
| `handle_stripe_billing_webhook` | Main webhook dispatcher (async) |

## Architecture Rule

Adapters contain **zero business logic**.
All business decisions live in `engines/billing_engine/orchestrator.py`.
