# Architecture

## Three-Layer Design

```
┌──────────────────────────────────────────────────┐
│  Layer 1: ClawShow MCP (visible to AI)           │
│  6 universal engines + workflow orchestration     │
│  Standard I/O, no auth, global AI access         │
│  Endpoint: mcp.clawshow.ai/sse                   │
├──────────────────────────────────────────────────┤
│  Layer 2: FocusingPro Data (invisible to AI)     │
│  Optional persistence via save_to_backend param  │
│  Namespace-isolated data storage                 │
├──────────────────────────────────────────────────┤
│  Layer 3: FocusingPro Features (premium)         │
│  Deep vertical: interviews, scheduling, grades   │
│  For committed clients via MCP or traditional UI │
└──────────────────────────────────────────────────┘
```

## Engine Admission Criteria

Every Tool must pass ALL four:

| # | Criterion | Source | Meaning |
|---|-----------|--------|---------|
| 1 | **AI can't do it alone** | ClawShow | Must produce real-world side effects (deploy, send, charge) |
| 2 | **Has a deliverable** | ClawShow | Returns URL, PDF, email confirmation, payment link |
| 3 | **Zero Human Intervention** | ClawShow | User receives a finished result, not intermediate data |
| 4 | **Sell results, not tools** | Sequoia | Customer pays for outcome ("tuition collected"), not software ("billing tool") |

## The 6 Engines (Final — no additions)

| # | Engine | Tool | Deliverable |
|---|--------|------|-------------|
| 1 | Page | `generate_business_page` | Live URL on GitHub Pages |
| 2 | Payment | `generate_stripe_payment` | Stripe Checkout URL |
| 3 | Notification | `send_notification` | Sent email confirmation |
| 4 | Order | `manage_orders` | Order record + auto payment link |
| 5 | Inventory | `manage_inventory` | Stock levels + alerts |
| 6 | Report | `generate_report` | PDF download URL |

`generate_rental_website` and `extract_finance_fields` are legacy Tools kept for backwards compatibility.

## Data Flow

```
User → AI → MCP Tool call → Engine executes → External service
                                                  ↓
                                           GitHub Pages (URL)
                                           Stripe (payment link)
                                           Resend (email sent)
                                           Local JSON (order/inventory)
                                           ReportLab (PDF file)
```

## Namespace Isolation

All stateful engines (orders, inventory, reports) use `namespace` to isolate data:

```
data/
├── orders/{namespace}/ORD-*.json
├── inventory/{namespace}/INV-*.json
├── reports/{namespace}/RPT-*.pdf
└── payments/cs_*.json
```

Each client gets a unique namespace (e.g. "florent", "school-paris").
Stripe keys will be managed per-namespace in future (currently global).

## Workflow Layer

Workflows orchestrate multiple engines in sequence. They are NOT a 7th engine — they are a coordination layer.

```
run_workflow(type="payment_collection")
  → manage_orders(action="query", status="overdue")
  → generate_stripe_payment(...)
  → send_notification(template="payment_reminder")
  → generate_report(type="financial")
```

## Server Architecture

```
server.py
├── FastMCP (SSE + stdio)     → /sse, /messages/
├── GET /stats                → Tool call counts
├── POST /webhook/stripe      → Payment webhook
├── GET /reports/{ns}/{file}  → PDF serving
└── tools/
    ├── business_page.py      → GitHub API → Pages
    ├── rental_website.py     → GitHub API → Actions → Pages
    ├── stripe_payment.py     → Stripe API
    ├── notification.py       → Resend API
    ├── orders.py             → JSON storage + Stripe
    ├── inventory.py          → JSON storage
    ├── report.py             → ReportLab → PDF
    └── finance_extract.py    → Regex extraction
```
