# Billing Engine

Business logic for ClawShow recurring/installment billing.

## Modules

| File | Purpose |
|------|---------|
| `orchestrator.py` | Main coordinator — creates plans, queries status |
| `schedule_calculator.py` | Calculates installment dates (monthly/quarterly/weekly) |
| `success_detector.py` | Determines if payment succeeded; classifies failures |
| `commission.py` | Commission rate lookup + calculation (Pay-for-Outcome) |
| `scheduler.py` | APScheduler integration — **Week 2** |
| `retry_manager.py` | Retry logic for failed installments — **Week 2** |
| `esign_integration.py` | Contract signing flow — **Week 3** |
| `webhook_sender.py` | (Deprecated — use `core/webhook_sender.py`) |

## Architecture (Principle 1)

```
Tool (create_billing_plan)
  ↓
BillingOrchestrator
  ├── schedule_calculator
  ├── core.webhook_sender
  └── adapters.mollie.*
```

Tools NEVER call adapters directly.

## Week 1 Scope

- ✅ create_plan (Mollie TEST, no scheduler, no eSign)
- ✅ get_status
- ❌ APScheduler (Week 2)
- ❌ eSign integration (Week 3)
- ❌ Stripe (Week 3)
