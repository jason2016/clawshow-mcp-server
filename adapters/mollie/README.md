# Mollie Adapter

Handles all Mollie API calls for ClawShow Billing.

## Files

| File | Purpose |
|------|---------|
| `customer.py` | Create/get Mollie customers |
| `subscription.py` | Create/cancel recurring subscriptions |
| `mandate.py` | List/get mandates (auto-created by Mollie on first charge) |
| `webhook_handler.py` | Handle inbound Mollie payment webhooks (Week 2) |

## Auth

```
TEST:  MOLLIE_API_KEY_TEST=test_xxx   ← Week 1-4
LIVE:  MOLLIE_API_KEY_LIVE=live_xxx  ← After 2026-05-20 only
```

## Architecture Rule

Adapters contain ZERO business logic.
All business decisions live in `engines/billing_engine/orchestrator.py`.
