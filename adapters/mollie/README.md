# Mollie Adapter

**Status**: ✅ MVP Complete (Week 4, 2026-04-21)

## Overview

Integrates ClawShow Billing with the Mollie payment platform.
Used for SEPA Direct Debit recurring subscriptions (primary gateway for France).

## Files

| File | Purpose |
|------|---------|
| `customer.py` | Create / get Mollie customers |
| `mandate.py` | Create test SEPA mandates (TEST mode) |
| `subscription.py` | Create / cancel subscriptions, retry payments |
| `webhook_handler.py` | Handle inbound Mollie payment webhooks |

## Authentication

```
TEST:  MOLLIE_API_KEY_TEST=test_xxx   ← all namespaces (Phase 1)
LIVE:  MOLLIE_API_KEY_LIVE=live_xxx  ← after Mollie LIVE approval
```

Mode is determined by `core.config.get_gateway_mode(namespace, "mollie")`.

## Key Behaviors

### Webhook per subscription (2026 API change)

Mollie Dashboard no longer supports a profile-level default webhook.
`webhookUrl` is passed **per subscription** at creation time:

```python
create_mollie_subscription(
    ...
    webhook_url="https://mcp.clawshow.ai/webhooks/mollie",
)
```

### Mandate required for SEPA

Before creating a recurring subscription, a mandate must exist.
In TEST mode: `create_test_mandate()` creates a fake SEPA mandate.
In LIVE mode: mandate is created by the customer's first SEPA payment.

### times=None for infinite subscriptions

Pass `times=None` (do not include the key) to Mollie for infinite subscriptions.
Passing `times=0` or `times=12` would cap the subscription.

```python
# Correct: infinite
params = {"amount": ..., "interval": "1 month", ...}
# Do NOT add: params["times"] = ...

# Correct: fixed 10 payments
params["times"] = 10
```

## Webhook Handler Flow

```
POST /webhooks/mollie  {"id": "tr_xxx"}
  ↓
fetch payment from Mollie API (full details)
  ↓
find plan by subscriptionId
  ↓
  paid   → charged installment + commission + outbound webhook + rolling schedule
  failed → failed installment + retry schedule + outbound webhook
  other  → no-op
```

**Idempotency**: if the same `payment_id` is already in status `charged`, the webhook is silently skipped.

## Methods

| Function | Description |
|----------|-------------|
| `create_mollie_customer` | POST /v2/customers |
| `create_test_mandate` | POST /v2/customers/{id}/mandates (test mode) |
| `create_mollie_subscription` | POST /v2/customers/{id}/subscriptions |
| `get_mollie_subscription` | GET /v2/customers/{id}/subscriptions/{sub_id} |
| `cancel_mollie_subscription` | DELETE /v2/customers/{id}/subscriptions/{sub_id} |
| `retry_failed_payment` | POST /v2/customers/{id}/payments (sequenceType=recurring) |
| `handle_mollie_webhook` | Main webhook dispatcher (async) |

## Architecture Rule

Adapters contain **zero business logic**.
All business decisions live in `engines/billing_engine/orchestrator.py`.
