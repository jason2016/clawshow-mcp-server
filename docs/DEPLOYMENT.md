# ClawShow Deployment Guide

**Updated: 2026-04-21 (Billing MVP Week 4)**

## Server: stand9 (OVH France)

| Item | Value |
|------|-------|
| Host | stand9.focusingpro.com |
| IP | 51.77.201.82 |
| OS | Ubuntu 24.04 |
| Provider | OVH (Strasbourg) |
| SSH | `ssh root@51.77.201.82` |
| Code path | `/opt/clawshow-mcp-server` |
| Python | 3.12 (`.venv` — PEP 668 compliant) |
| Domain | `mcp.clawshow.ai` → Nginx → localhost:8000 |
| eSign domain | `esign-api.clawshow.ai` → same backend |

## Service Management

```bash
systemctl start clawshow-mcp
systemctl stop clawshow-mcp
systemctl restart clawshow-mcp
systemctl status clawshow-mcp --no-pager
journalctl -u clawshow-mcp -f --no-pager        # live logs
journalctl -u clawshow-mcp --since "10 min ago" --no-pager  # recent
```

## Standard Deploy (most common)

```bash
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git pull origin main && systemctl restart clawshow-mcp && sleep 3 && systemctl status clawshow-mcp --no-pager | head -8"
```

## Deploy with New Dependencies

```bash
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git pull origin main && .venv/bin/pip install -r requirements.txt && systemctl restart clawshow-mcp"
```

## Virtual Environment

Ubuntu 24.04 enforces PEP 668 — system Python is protected.
Always use `.venv`:

```bash
cd /opt/clawshow-mcp-server
.venv/bin/python --version           # should be 3.12.x
.venv/bin/pip list | grep mollie     # check a package
.venv/bin/python server.py           # manual start (for debug)
```

## Environment Variables

File: `/opt/clawshow-mcp-server/.env` (never committed to git)

```
# General
MCP_BASE_URL=https://mcp.clawshow.ai
GITHUB_TOKEN=ghp_xxx
RESEND_API_KEY=re_xxx
AWS_ACCESS_KEY_ID=xxx
AWS_SECRET_ACCESS_KEY=xxx
S3_BUCKET_CLAWSHOW=xxx

# Billing — Mollie
MOLLIE_API_KEY_TEST=test_xxx
MOLLIE_API_KEY_LIVE=live_xxx       # after Mollie LIVE approval

# Billing — Stripe IESIG
STRIPE_API_KEY_IESIG_TEST=sk_test_xxx
STRIPE_WEBHOOK_SECRET_IESIG_TEST=whsec_xxx
STRIPE_API_KEY_IESIG_LIVE=sk_live_xxx    # Phase 2
STRIPE_WEBHOOK_SECRET_IESIG_LIVE=...     # Phase 2

# Billing — gateway mode overrides (optional)
# CLAWSHOW_ILCI_WILLIAM_MOLLIE_MODE=live  # enable live per namespace
```

## Database

SQLite at `/opt/clawshow-mcp-server/data/billing.db`.

```bash
# Quick check
ssh root@51.77.201.82 "sqlite3 /opt/clawshow-mcp-server/data/billing.db 'SELECT count(*), status FROM billing_plans GROUP BY status;'"

# eSign DB
sqlite3 /opt/clawshow-mcp-server/data/esign.db '.tables'
```

Backup: currently manual. Phase 2: daily S3 backup.

## Nginx

Config: `/etc/nginx/sites-enabled/mcp.clawshow.ai`

Reverse proxy `mcp.clawshow.ai` → `localhost:8000`. SSL via Let's Encrypt.

Key routes:
- `/sse` → MCP SSE stream
- `/stats` → JSON tool call counts
- `/webhooks/mollie` → Mollie billing webhook
- `/webhooks/stripe` → Stripe billing webhook (Week 3+)
- `/webhooks/esign` → eSign callback (Week 3+)
- `/webhook/stripe` → legacy Stripe checkout webhook
- `/esign/*` → eSign signing pages
- `/reports/` → PDF files

## SSL Certificate

```bash
certbot renew --dry-run    # test
certbot renew              # actual renewal
```

Auto-renew via cron. Check: `certbot certificates`.

## systemd Service

File: `/etc/systemd/system/clawshow-mcp.service`

```ini
[Unit]
Description=ClawShow MCP Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/clawshow-mcp-server
ExecStart=/opt/clawshow-mcp-server/.venv/bin/python server.py
Restart=on-failure
RestartSec=5
EnvironmentFile=/opt/clawshow-mcp-server/.env

[Install]
WantedBy=multi-user.target
```

## Verify

```bash
# Service running
ssh root@51.77.201.82 "systemctl status clawshow-mcp --no-pager | head -8"

# Stats
curl https://mcp.clawshow.ai/stats

# Webhook endpoints
curl -X POST https://mcp.clawshow.ai/webhooks/mollie -d '{"id":"test"}' -H 'Content-Type: application/json'
```

## Rollback

```bash
# Find previous stable commit
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git log --oneline -10"

# Rollback
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git checkout <commit-hash> && systemctl restart clawshow-mcp"

# Return to latest
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git checkout main && git pull origin main && systemctl restart clawshow-mcp"
```
