# Billing Tools

**Status**: 🚧 Bootstrap Phase (2026-04-19)
**P0 Product per STRATEGY_DECISIONS v2.0 #29**

## Tools

- `create_billing_plan` - Create subscription/installment plan
- `get_billing_status` - Check status of a plan
- `cancel_billing_plan` - Cancel plan or initiate refund

## Strategic Positioning

Cross-gateway AI-Native subscription & installment platform.
- Multi-gateway (Stripe / Mollie / Stancer / GoCardless)
- Built-in eSign integration
- AI-First API
- €29/mo + 0.3-0.5% transaction fee

## Success Criteria (Pricing v2.5)

Billing "success" = Payment gateway returns "succeeded" status.

ClawShow charges commission only on successful charges.
If ClawShow fails, no commission.
If 3rd party (Stripe etc.) fails, no ClawShow commission.
Third-party fees (if any) are client's responsibility.

## Related

- `engines/payment_engine/` - Backend orchestration
- `engines/payment_engine/adapters/` - Gateway adapters
- Manifesto v1.1 - AI-First 5 principles
- Pricing v2.5 - Pay-for-outcome model

## Roadmap

- [x] Bootstrap (2026-04-19)
- [ ] MCP Tool spec (2026-04-21 战略会议)
- [ ] Stripe adapter MVP (2026-04-22 ~ 2026-04-25)
- [ ] William ILCI Sandbox test (2026-05-01 ~ 2026-05-15)
- [ ] Mollie adapter (2026-05-15 ~ 2026-05-30)
- [ ] Production launch (2026-06-15)
