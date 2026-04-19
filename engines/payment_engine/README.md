# Payment Engine

**Status**: 🚧 Bootstrap Phase (2026-04-19)

## Purpose

Orchestrates subscription/installment billing across multiple payment gateways.
Used by `tools/billing/` Tools.

## Architecture

```
Tool Layer (tools/billing/)
    ↓
Engine Layer (here)
    ├── adapters/        - Per-gateway adapters
    ├── scheduler.py     - Daily recurring charge runner
    ├── models.py        - BillingPlan, BillingEvent data models
    └── success_detector - Pay-for-outcome logic (Pricing v2.5)
```

## Gateways

| Gateway | Status | Use Case |
|---------|--------|----------|
| Stripe | `.placeholder` | William ILCI Sandbox → Production |
| Mollie | `.placeholder` | FR/EU subscriptions (Live pending 4/21) |
| Stancer | `.placeholder` | FR one-off + recurring |
| GoCardless | `.placeholder` | SEPA Direct Debit (Phase 3) |
