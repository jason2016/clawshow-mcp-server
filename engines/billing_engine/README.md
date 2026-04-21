# ClawShow Billing Engine

**Status**: ✅ MVP Complete (Week 4, 2026-04-21)

## Overview

Core business logic for billing. Orchestrates the full payment lifecycle from plan creation to final charge.

```
Tool (create_billing_plan / cancel_billing_plan)
  ↓
BillingOrchestrator        ← single entry point
  ├── schedule_calculator  ← date + amount math
  ├── esign_integration    ← contract flow (optional)
  ├── webhook_sender       ← outbound events
  ├── retry_manager        ← failed charge recovery
  ├── scheduler            ← APScheduler jobs
  └── adapters.*           ← Mollie / Stripe
```

## Components

### orchestrator.py

Main coordinator. All Tools call this. Never call adapters directly.

Key methods:
- `create_plan()` — Creates customer, mandate, (deferred) subscription, fires eSign
- `get_status()` — Returns plan + installment progress
- `cancel_plan()` — Cancels gateway subscription + DB + outbound webhook
- `activate_subscription_for_plan()` — Called by esign callback

**Contract deferred flow**: when `contract_required=True`, subscription is NOT created at `create_plan` time. It is created in `esign_integration._create_deferred_subscription()` after the signature webhook arrives.

### schedule_calculator.py

Calculates installment schedule. Implements Preview vs Reality pattern:
- Fixed plans: generates N rows
- Infinite (`installments=-1`): generates 12 preview rows; rolling schedule adds more after each charge

### webhook_sender.py

Outbound webhook to external platform (FocusingPro, webhook.site, etc.):
- Standard payload v1: `{clawshow_version, event, timestamp, plan_id, order_id, namespace, data}`
- 3x retry with 2s / 5s / 15s delays (async version)
- 1x fire-and-forget (sync version, for orchestrator.create_plan)
- All attempts logged to `billing_webhook_logs`

### scheduler.py

APScheduler (BackgroundScheduler — thread-based, works without event loop at startup).

Used for:
- ✅ Retry scheduling (24h / 48h / 72h after failed charge)
- ✅ Daily cleanup of plans stuck in `pending_signature` for 30+ days
- ❌ NOT for first charges (Mollie/Stripe handles that)

### retry_manager.py

Failed charge retry:
- Max 3 retries per installment
- Delays: 24h → 48h → 72h
- Classification-aware: `client_permanent` errors skip retry

### esign_integration.py

Coordinates with the eSign engine:
1. `send_contract_for_signing()` — POSTs to `/esign/create`
2. `handle_esign_callback()` — Called from `POST /webhooks/esign`
3. `_activate_plan_after_signature()` — Creates deferred subscription + activates plan
4. `_cancel_plan_after_failed_signature()` — Cancels plan on decline/expiry

## Events

Standard v1 events (see `webhook_sender.py` EVENTS set):

```
plan_created
plan_activated
installment_scheduled
installment_charged_success
installment_charged_failed
installment_retry_scheduled
installment_retry_failed_final
plan_completed
plan_cancelled
```

## Database

SQLite at `/opt/clawshow-mcp-server/data/billing.db`.
Schema: `migrations/001_billing_initial.sql`.
All queries are namespace-isolated.

Key tables:
- `billing_plans` — one row per plan
- `billing_installments` — N rows per plan
- `billing_commissions` — one row per successful charge
- `billing_webhook_logs` — all outbound webhook attempts
