# ClawShow — Instant Backend for SMBs

> AI-callable MCP tools for small businesses. No signup, no dashboard, just results.

**Endpoint:** `https://mcp.clawshow.ai/sse`  
**Version:** 1.7.0  
**Tools:** 11  
**Transport:** SSE (Remote)

## Quick Start

### Claude.ai
Settings → Integrations → Add URL: `https://mcp.clawshow.ai/sse`

### Claude Desktop / Cursor / Windsurf
```json
{
  "mcpServers": {
    "clawshow": {
      "url": "https://mcp.clawshow.ai/sse"
    }
  }
}
```

## Tools

### Page Generation

**`generate_business_page`** — Generate business pages and auto-deploy to GitHub Pages. Returns a live URL in 60 seconds.

**`generate_rental_website`** — Generate rental property websites with photos, pricing, calendar, and booking. Ideal for Airbnb-to-direct transition.

### Payments

**`generate_payment`** — Generate payment links via Stripe (global), Stancer (France), or SumUp (Europe). Supports Apple Pay, Google Pay, CB, Visa, Mastercard, SEPA.

**`verify_payment`** — Check payment status. Supports Stripe, Stancer, and SumUp.

### Notifications

**`send_notification`** — Send email, SMS, or WhatsApp notifications. Supports templates, batch sending, and 30/60/90 day dunning escalation.

### Electronic Signature

**`send_esign_request`** — Full electronic signature platform (V2). Multi-page document signing with per-page paraphes + final signature block. Dual-party flow: student signs first, school counter-signs automatically notified by email. Three input modes: Draw (Bézier pen), Type (styled font), Upload image. Real-time progress bar, mobile-friendly. Webhook callbacks on `signer.signed`, `document.completed`, `document.expired`. Full audit trail with IP, timestamp, city. FocusingPro compatible (`send_foxit_esign` drop-in). Zero cost per signature. Fully self-hosted. eIDAS compliant.

### Business Management

**`manage_bookings`** — Booking management for restaurants, hotels, salons, venues, rentals. Double-booking detection.

**`manage_orders`** — Order management with full lifecycle. Auto-creates from payment webhooks.

**`manage_inventory`** — Inventory tracking with low-stock alerts. Batch updates.

### Reporting & Finance

**`generate_report`** — Generate PDF business reports. Returns download URL.

**`extract_finance_fields`** — Extract structured data from invoice/receipt text.

## Supported Payment Providers

| Provider | Region | Status |
|----------|--------|--------|
| Stancer | France | ✅ Live |
| SumUp | Europe | ✅ Live |
| Stripe | Global | ✅ Live |
| Mollie | Europe | 🔜 Planned |

## Design Principles

- **Zero Human Intervention** — Every tool returns a directly usable result
- **AI-First** — Descriptions optimized for AI discovery. Standard JSON I/O.
- **No Signup Required** — First call auto-creates a namespace
- **Namespace Isolation** — Multi-tenant by default
- **Zero Cost Signatures** — Self-hosted e-sign, no per-document fees

## Use Cases

| Industry | Typical Workflow |
|----------|-----------------|
| **Schools** | `send_esign_request` (contracts) → `generate_payment` (tuition) → `send_notification` (dunning) |
| **Rental Properties** | `generate_rental_website` → `send_esign_request` (lease) → `generate_payment` (rent) |
| **Restaurants** | `manage_bookings` → `manage_orders` → `generate_payment` |
| **E-commerce** | `manage_orders` → `manage_inventory` → `generate_payment` → `generate_report` |
| **Freelancers** | `send_esign_request` (contract) → `generate_payment` (invoice) → `extract_finance_fields` |

## Architecture

```
┌─────────────────────────────────────────┐
│  ClawShow MCP Server (Public)           │
│  11 AI-callable tools, SSE transport    │
│  https://mcp.clawshow.ai/sse           │
├─────────────────────────────────────────┤
│  Data Persistence Layer (Optional)      │
│  Namespace-isolated, auto-provisioned   │
│  SQLite + optional cloud backend        │
└─────────────────────────────────────────┘
```

## Demo Mode

Call any tool without a namespace to use demo data:
- `manage_orders(action="query")` → sample orders
- `generate_payment(amount=10, currency="eur", provider="stancer", description="Demo", namespace="demo")` → real test payment link
- `send_esign_request(template="enrollment_contract", signer_name="Demo User", signer_email="demo@test.com", fields={}, namespace="demo")` → signing page URL

## License

MIT

---

Built by [ClawShow](https://clawshow.ai) · Instant Backend for Small Business
