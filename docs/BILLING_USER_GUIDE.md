# ClawShow Billing — User Guide

**Version**: 1.0 (MVP, 2026-04-21)

---

## Quick Start (5 minutes)

**Prerequisites:**
- ClawShow namespace configured
- Mollie TEST API key in `.env`

### Create your first billing plan

```python
# In Claude Desktop with ClawShow MCP:

create_billing_plan(
    namespace="my-school",
    customer_email="student@example.com",
    customer_name="Jean Dupont",
    total_amount=6000.00,
    currency="EUR",
    installments=6,
    frequency="monthly",
    start_date="2026-06-01",
    gateway="mollie",
    description="Frais formation 2026",
)
```

Response:
```json
{
  "success": true,
  "plan_id": "plan_abc123",
  "status": "active",
  "gateway_plan_id": "sub_xxx",
  "per_installment_amount": 1000.0,
  "schedule": [
    {"installment_number": 1, "scheduled_date": "2026-06-01", "amount": 1000.0},
    {"installment_number": 2, "scheduled_date": "2026-07-01", "amount": 1000.0},
    {"installment_number": 3, "scheduled_date": "2026-08-01", "amount": 1000.0}
  ]
}
```

Mollie handles all subsequent monthly charges automatically.

---

## Use Case 1: SaaS Monthly Subscription

Scenario: charging a SaaS customer €99/month indefinitely.

```python
create_billing_plan(
    namespace="my-saas",
    customer_email="ceo@company.com",
    customer_name="Sophie Renard",
    total_amount=99.00,          # per-month amount
    currency="EUR",
    installments=-1,              # -1 = infinite, no end date
    frequency="monthly",
    start_date="2026-05-01",
    gateway="mollie",
    description="ClawShow Pro — monthly",
    external_webhook_url="https://your-crm.com/webhooks",
    external_order_id="CRM-CUSTOMER-42",
)
```

**What happens:**
- Mollie subscription created (infinite)
- First charge on 2026-05-01
- Subsequent charges monthly, automatically
- Your CRM receives `installment_charged_success` event each month

---

## Use Case 2: Student Tuition with Contract (ILCI model)

Scenario: €12,000 tuition, 10 monthly installments, contract must be signed first.

```python
create_billing_plan(
    namespace="ilci",
    customer_email="marie.dupont@gmail.com",
    customer_name="Marie Dupont",
    total_amount=12000.00,
    currency="EUR",
    installments=10,
    frequency="monthly",
    start_date="2026-09-01",
    gateway="mollie",

    # Contract
    contract_pdf_url="https://mcp.clawshow.ai/esign/esign_2026_0001/preview.pdf",
    contract_required_before_charge=True,
    signers=[
        {"email": "marie.dupont@gmail.com", "name": "Marie Dupont", "role": "student"},
        {"email": "direction@ilci.fr", "name": "ILCI Direction", "role": "school"},
    ],

    # External sync with FocusingPro
    external_platform_name="focusingpro",
    external_webhook_url="https://mcp.focusingpro.com/webhooks/clawshow",
    external_order_id="ILCI-2026-001",
    external_auth_token="Bearer your-token",

    description="Frais scolarité 2026-2027 - Marie Dupont",
)
```

**What happens:**
1. Mollie customer + mandate created
2. Plan saved with `status=pending_signature`
3. eSign request sent to both signers (Marie + ILCI Direction)
4. **No Mollie subscription yet**
5. After both sign → `/webhooks/esign` callback → Mollie subscription created → `status=active`
6. First charge on 2026-09-01

---

## Use Case 3: One-time Payment

```python
create_billing_plan(
    namespace="neige-rouge",
    customer_email="client@restaurant.fr",
    customer_name="Pierre Durand",
    total_amount=150.00,
    currency="EUR",
    installments=1,
    frequency="one_time",
    gateway="mollie",
    description="Réservation table 8 personnes",
)
# Returns: {status="pending_charge"}
# Manual charge trigger required (Phase 2 feature)
```

---

## Feature Reference

### create_billing_plan Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespace` | str | required | Your ClawShow namespace |
| `customer_email` | str | required | Customer email |
| `customer_name` | str | required | Customer full name |
| `total_amount` | float | required | Total amount (not per installment) |
| `currency` | str | `"EUR"` | ISO currency code |
| `installments` | int | `1` | Number of payments; `-1` = infinite |
| `frequency` | str | `"monthly"` | `monthly` / `quarterly` / `weekly` / `one_time` |
| `start_date` | str | today | First charge date `YYYY-MM-DD` |
| `gateway` | str | `"mollie"` | `mollie` or `stripe` |
| `contract_pdf_url` | str | `""` | PDF URL to send for eSign |
| `contract_required_before_charge` | bool | `False` | Hold subscription until signed |
| `signers` | list | `[]` | `[{email, name, role}]` |
| `external_webhook_url` | str | `""` | Your webhook endpoint for events |
| `external_order_id` | str | `""` | Your internal order reference |
| `external_auth_token` | str | `""` | Bearer token for your webhook |
| `description` | str | `""` | Human-readable plan description |
| `customer_phone` | str | `""` | Customer phone (optional) |

### get_billing_status Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `namespace` | str | Your namespace |
| `plan_id` | str | Plan ID from create_billing_plan |

Returns installment counts (paid / failed / pending), next scheduled charge.

### cancel_billing_plan Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `namespace` | str | Your namespace |
| `plan_id` | str | Plan ID to cancel |
| `reason` | str | Cancellation reason (logged + forwarded) |

**Phase 1 limitation**: no automatic refund. Cancellation only stops future charges.

---

## Integration Guide

### Receiving Events (external_webhook_url)

Set up an endpoint on your system to receive ClawShow events:

**Payload v1 format:**
```json
{
  "clawshow_version": "1.0",
  "event": "installment_charged_success",
  "timestamp": "2026-05-01T10:00:00+00:00",
  "plan_id": "plan_abc123",
  "order_id": "YOUR-ORDER-001",
  "namespace": "my-school",
  "data": {
    "installment_number": 1,
    "amount": 1000.0,
    "charged_at": "2026-05-01T10:00:00+00:00"
  }
}
```

**Python example (Flask):**
```python
@app.route('/webhooks/clawshow', methods=['POST'])
def clawshow_webhook():
    payload = request.json
    event = payload['event']
    plan_id = payload['plan_id']
    order_id = payload['order_id']

    if event == 'installment_charged_success':
        mark_installment_paid(order_id, payload['data']['installment_number'])
    elif event == 'plan_cancelled':
        cancel_enrollment(order_id)
    elif event == 'plan_completed':
        mark_fully_paid(order_id)

    return jsonify({"ok": True}), 200
```

**PHP example:**
```php
$payload = json_decode(file_get_contents('php://input'), true);
$event = $payload['event'];
if ($event === 'installment_charged_success') {
    mark_paid($payload['order_id']);
}
http_response_code(200);
echo json_encode(['ok' => true]);
```

### eSign Callback

When `contract_required_before_charge=True`, ClawShow sends an eSign request.
After signing, ClawShow calls `POST /webhooks/esign` internally.

The signed PDF URL is available at:
`https://mcp.clawshow.ai/esign/{document_id}/signed.pdf`

---

## Troubleshooting

### "Gateway error creating customer"

Mollie API key not configured or wrong mode. Check:
```bash
grep MOLLIE_API_KEY /opt/clawshow-mcp-server/.env
```

### "No suitable mandates found for customer"

Test mode: mandate was not created. This is auto-handled in Week 2+.
Check `gateway_mandate_id` in the plan response.

### Plan stuck in pending_signature

1. Check eSign document status: `GET /esign/{document_id}/status`
2. Signers may not have received the email (check spam)
3. If 30+ days: plan auto-cancelled by daily cleanup job

### Webhook not received

1. Check `billing_webhook_logs` table:
   ```sql
   SELECT * FROM billing_webhook_logs WHERE plan_id='plan_xxx' ORDER BY created_at DESC;
   ```
2. Verify `external_webhook_url` is publicly accessible (not localhost)
3. ClawShow retries 3x (2s → 5s → 15s delays) then gives up

---

## Pricing

| Model | Rate |
|-------|------|
| ClawShow commission | 0.5% of each successful charge |
| William/ILCI special | 0.25% |
| Failed charges | No commission |
| Cancellations | No commission |

See `https://clawshow.ai/pricing` for current rates.
