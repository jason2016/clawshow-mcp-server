# ClawShow Billing Tools

**Status**: ✅ MVP Complete (Week 4, 2026-04-21)

## Overview

Three MCP Tools for subscription and installment billing:

| Tool | Description |
|------|-------------|
| `create_billing_plan` | Create a new recurring or installment billing plan |
| `get_billing_status` | Query plan status, installment progress, and next charge |
| `cancel_billing_plan` | Cancel an active plan (no refund) |

## Architecture

```
Tool → BillingOrchestrator → Adapter (Mollie / Stripe)
                           → BillingDB (SQLite, namespace-isolated)
                           → eSign Integration (when contract_required=True)
                           → Webhook Sender (outbound events to external platform)
```

Tools **never** call adapters directly. All business logic is in `engines/billing_engine/`.

## Usage Examples

### Subscription (infinite monthly)

```python
create_billing_plan(
    namespace="my-saas",
    customer_email="alice@example.com",
    customer_name="Alice Martin",
    total_amount=99.00,
    currency="EUR",
    installments=-1,         # -1 = infinite
    frequency="monthly",
    start_date="2026-05-01",
    gateway="mollie",
)
# Returns: {plan_id, status="active", gateway_plan_id, ...}
```

### Installment with contract (10 monthly + eSign)

```python
create_billing_plan(
    namespace="ilci",
    customer_email="student@school.fr",
    customer_name="Marie Dupont",
    total_amount=12000.00,
    currency="EUR",
    installments=10,
    frequency="monthly",
    start_date="2026-09-01",
    gateway="mollie",
    contract_pdf_url="https://mcp.clawshow.ai/esign/doc_id/preview.pdf",
    contract_required_before_charge=True,
    signers=[
        {"email": "student@school.fr", "name": "Marie Dupont", "role": "signer"},
        {"email": "direction@school.fr", "name": "ILCI Direction", "role": "signer"},
    ],
    external_webhook_url="https://your-platform/webhooks",
    external_order_id="ORDER-2026-001",
)
# Returns: {status="pending_signature", contract_signing={signing_url, esign_request_id}}
# Mollie subscription is created AFTER /webhooks/esign callback with status=signed
```

### One-time payment

```python
create_billing_plan(
    namespace="my-shop",
    customer_email="bob@example.com",
    customer_name="Bob Smith",
    total_amount=250.00,
    currency="EUR",
    installments=1,
    frequency="one_time",
    gateway="mollie",
)
```

### Cancel a plan

```python
cancel_billing_plan(
    namespace="ilci",
    plan_id="plan_abc123",
    reason="Student withdrew",
)
# Returns: {success=True, status="cancelled"}
# All scheduled installments → cancelled
# Already charged installments → untouched (no refund)
```

## Plan Status Flow

```
create_plan
    ↓
[contract_required=False]   → active ──────────────→ completed
[contract_required=True]    → pending_signature ──→ active → completed
[frequency=one_time]        → pending_charge
                                   ↓
                            any status → cancelled
```

## External Webhooks (v1 payload)

When `external_webhook_url` is provided, ClawShow fires:

| Event | When |
|-------|------|
| `plan_created` | Plan saved to DB |
| `plan_activated` | Contract signed |
| `installment_charged_success` | Payment successful |
| `installment_charged_failed` | Payment failed |
| `plan_completed` | All installments paid |
| `plan_cancelled` | Plan cancelled |

## Gateway Support

| Gateway | Status | Use case |
|---------|--------|----------|
| Mollie | ✅ Primary | SEPA Direct Debit, recurring (FR) |
| Stripe | ✅ Secondary | IESIG sandbox |
| Stancer, SumUp | 🔜 Phase 2 | |

## Commission Model

ClawShow charges **only on successful payments** (Pay-for-Outcome):
- Default rate: 0.5%
- William/ILCI: 0.25% (configured per namespace)
- Commission logged per installment in `billing_commissions` table

## Related

- [engines/billing_engine/README.md](../../engines/billing_engine/README.md)
- [adapters/mollie/README.md](../../adapters/mollie/README.md)
- [adapters/stripe/README.md](../../adapters/stripe/README.md)
- [docs/BILLING_USER_GUIDE.md](../../docs/BILLING_USER_GUIDE.md)
