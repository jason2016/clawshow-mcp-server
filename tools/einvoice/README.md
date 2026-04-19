# e-Invoice Tools

**Status**: 🚧 Bootstrap Phase (2026-04-19)
**P0 Product per STRATEGY_DECISIONS v2.0 #30**

## Tools

- `create_einvoice` - Generate Factur-X format + route to PDP
- `receive_einvoices` - Aggregate incoming invoices across PDPs
- `get_einvoice_status` - Check delivery status
- `switch_pdp` - Migrate between PDPs
- `validate_einvoice` - Compliance check

## Strategic Context

**Window**: 2026-09-01 French mandatory e-invoice deadline
**Remaining**: ~4.5 months from 2026-04-19

## Strategic Positioning

"AI Layer above PDPs" (not becoming a PDP).
Client keeps existing accounting software.
ClawShow routes across Pennylane / Sage / Cegid /
facture.net / Chorus Pro.

## Success Criteria (Pricing v2.5)

e-Invoice "success" = PDP confirms receipt + deliverable status.

ClawShow charges €0.10/invoice only on successful delivery.
If ClawShow fails, no charge.
If PDP fails, no ClawShow charge (retry 3x, then stop).

## Related

- `engines/einvoice_engine/`
- Richard Factur-X code (2025 Pennylane sandbox tested)
- Revenue share with Richard: 25-30% per decision #30

## Roadmap

- [x] Bootstrap (2026-04-19)
- [ ] MCP Tool spec (2026-04-21)
- [ ] Richard Factur-X integration (2026-04-22 ~ 2026-05-01)
- [ ] 2 PDP adapters MVP (2026-05-01 ~ 2026-06-01)
- [ ] UHTECH test (2026-06-01 ~ 2026-07-01)
- [ ] Production launch BEFORE 2026-09-01 (强制日)
