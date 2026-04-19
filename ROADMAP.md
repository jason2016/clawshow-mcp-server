# ClawShow MCP Server Roadmap

## Current State (2026-04-19)

- 11 Tools live in production
- Billing + e-Invoice in Bootstrap Phase
- Architecture: single MCP server

## Phase 0: Bootstrap (2026-04-19 ~ 2026-04-21)
- [x] Scaffolding for Billing + e-Invoice
- [x] Architecture decision: unified server
- [ ] MCP Tool specs (战略会议 4/21)

## Phase 1: Billing MVP (2026-04-22 ~ 2026-06-15)
- [ ] create_billing_plan with Stripe
- [ ] Scheduler (daily check)
- [ ] Success detection (Pricing v2.5)
- [ ] William ILCI Sandbox (2026-05-15)
- [ ] Production launch

## Phase 2: e-Invoice MVP (2026-04-22 ~ 2026-09-01)
- [ ] Richard Factur-X integration
- [ ] 2-3 PDP adapters
- [ ] create_einvoice MVP
- [ ] UHTECH test
- [ ] Production before 2026-09-01 (hard deadline)

## Phase 3: Multi-Gateway (2026-05-15 ~ 2026-06-30)
- [ ] Mollie Recurring
- [ ] Stancer scheduler
- [ ] GoCardless SEPA

## Phase 4: Scale (2026 Q3+)
- [ ] Customer acquisition
- [ ] Marketing site updates
- [ ] Commons hook activation (long-term)
