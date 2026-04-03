# Changelog

## v1.3.0 (2026-04-02)

**All 6 engines complete. Full Instant Backend platform ready.**

- Add `generate_report` — PDF reports via reportlab (financial, inventory, orders, enrollment, custom)
- Add `manage_inventory` — stock management (add/remove/adjust/query/alert)
- Add `manage_orders` — order CRUD with auto Stripe links + webhook auto-mark-paid
- Add `generate_business_page` — 5 page types (rental, enrollment, product, service, restaurant) with GEO
- Stripe webhook auto-marks orders as paid via order_id in metadata
- PDF serving via GET /reports/{namespace}/{filename}
- 8 Tools, 6 Engines total

## v1.1.0 (2026-04-02)

- Add `generate_business_page` with 5 page types + JSON-LD Schema + llms.txt GEO
- Static HTML deploy from main branch (~60s, no Actions needed)
- i18n support (en/fr/zh)
- 6 Tools total

## v1.0.0 (2026-04-02)

**Three engines online.**

- Add `generate_stripe_payment` — Stripe Checkout Session creation
- Add `send_notification` — email via Resend with 4 HTML templates
- Upgrade `generate_rental_website` with payment_url param (green Pay Now button)
- POST /webhook/stripe for payment event processing
- GET /stats endpoint with CORS

## milestone-zero-human-intervention (2026-03-31)

**First Skill verified end-to-end.**

- `generate_rental_website` deploys to GitHub Pages, returns live URL
- Property data in → live URL out, zero manual steps
- Git Tree API single-commit push
- GitHub Actions build + deploy to gh-pages

## step1-complete (2026-03-31)

**MCP Server skeleton.**

- FastMCP server with SSE + stdio transport
- `generate_rental_website` (HTML mode)
- `extract_finance_fields` (regex extraction)
- Usage tracking via data/usage_log.json
