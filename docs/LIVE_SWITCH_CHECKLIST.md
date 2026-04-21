# Billing LIVE Switch Checklist

**Created: 2026-04-21**

This checklist must be completed before switching any namespace from TEST to LIVE mode.
Default is TEST. LIVE is opt-in per namespace via env var.

---

## How to Enable LIVE Mode

```bash
# On stand9, add to /opt/clawshow-mcp-server/.env:
CLAWSHOW_ILCI_WILLIAM_MOLLIE_MODE=live

# Restart
systemctl restart clawshow-mcp
```

Do NOT enable LIVE without completing this checklist.

---

## Mollie

- [ ] Mollie SEPA Direct Debit LIVE approval received (email confirmation from Mollie)
- [ ] `MOLLIE_API_KEY_LIVE` saved in stand9 `.env`
- [ ] Verified: `_get_client("live")` succeeds (no RuntimeError)
- [ ] Production webhook confirmed: `https://mcp.clawshow.ai/webhooks/mollie`
- [ ] Test transaction: create 1 TEST plan → verify it goes to Mollie LIVE dashboard
- [ ] Commission rate agreed in writing with client (per namespace)

## Stripe (if used for client)

- [ ] Stripe account fully verified (KYC passed)
- [ ] `STRIPE_API_KEY_IESIG_LIVE` and `STRIPE_WEBHOOK_SECRET_IESIG_LIVE` saved
- [ ] Stripe LIVE webhook configured in dashboard pointing to `/webhooks/stripe`
- [ ] Test with real card (small amount, refund manually)

## ClawShow Code

- [ ] `core/config.py` reviewed — default mode is "test" for all namespaces
- [ ] No hardcoded `mode = "live"` anywhere in adapters
- [ ] All logs include `mode=` indicator (confirm in journalctl)
- [ ] `get_gateway_mode(namespace, gateway)` returns correct value:
  ```bash
  ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && .venv/bin/python3 -c \"from core.config import get_gateway_mode; print(get_gateway_mode('ilci-william', 'mollie'))\""
  ```

## Customer Onboarding

- [ ] Customer (e.g. William) signed ClawShow service contract
- [ ] Commission rate agreed in writing (email/document)
- [ ] Sandbox test completed: full E2E in TEST mode with real scenario
- [ ] Customer trained: what they see in Mollie dashboard, what events to expect
- [ ] Support channel established (WhatsApp / email)

## Monitoring

- [ ] Logs checked at go-live: `journalctl -u clawshow-mcp -f`
- [ ] First LIVE charge monitored manually
- [ ] Commission table checked after first charge
- [ ] External webhook delivery confirmed (check webhook.site or client system)

## Legal & Compliance

- [ ] ClawShow business registration (SIREN/SIRET) active
- [ ] Data processing agreement (DPA) signed with client
- [ ] GDPR: customer data stored in France (OVH Strasbourg) ✅
- [ ] Terms of Service published at `clawshow.ai/terms`

---

## Post-LIVE Monitoring (First Week)

```bash
# Check plans daily
ssh root@51.77.201.82 "sqlite3 /opt/clawshow-mcp-server/data/billing.db 'SELECT status, COUNT(*) FROM billing_plans GROUP BY status;'"

# Check failed charges
ssh root@51.77.201.82 "sqlite3 /opt/clawshow-mcp-server/data/billing.db \"SELECT * FROM billing_installments WHERE status='failed' ORDER BY updated_at DESC LIMIT 10;\""

# Check commissions earned
ssh root@51.77.201.82 "sqlite3 /opt/clawshow-mcp-server/data/billing.db 'SELECT namespace, SUM(commission_amount) FROM billing_commissions GROUP BY namespace;'"

# Check webhook delivery rate
ssh root@51.77.201.82 "sqlite3 /opt/clawshow-mcp-server/data/billing.db 'SELECT succeeded, COUNT(*) FROM billing_webhook_logs GROUP BY succeeded;'"
```

---

## Rollback Plan

If LIVE mode causes issues:

```bash
# 1. Remove LIVE override env var
# Edit /opt/clawshow-mcp-server/.env — remove CLAWSHOW_xxx_MOLLIE_MODE=live

# 2. Restart
systemctl restart clawshow-mcp

# 3. Verify
# New plans will use TEST mode
# Existing LIVE plans: their Mollie subscription continues in LIVE (Mollie side)
# but ClawShow will use TEST key for API calls — this will fail
# → Cancel affected plans manually in Mollie dashboard
```

**Note**: switching back to TEST after LIVE plans exist requires manual cleanup.
Always test in sandbox before going LIVE.
