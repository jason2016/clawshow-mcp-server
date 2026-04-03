# Deployment

## Server

| Item | Value |
|------|-------|
| Host | stand9.focusingpro.com |
| IP | 51.77.201.82 |
| OS | Ubuntu 24.04 |
| Provider | OVH (Strasbourg) |
| SSH | `ssh root@51.77.201.82` |
| Code path | `/opt/clawshow-mcp-server` |
| Python | 3.12 (venv at `/opt/clawshow-mcp-server/venv`) |
| Domain | mcp.clawshow.ai → Nginx reverse proxy → localhost:8000 |

## Standard Deploy

```bash
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git pull origin main && systemctl restart clawshow-mcp"
```

If new pip dependencies were added:

```bash
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git pull origin main && source venv/bin/activate && pip install -r requirements.txt && systemctl restart clawshow-mcp"
```

## Verify

```bash
# Service status
ssh root@51.77.201.82 "systemctl status clawshow-mcp --no-pager | head -8"

# Recent logs
ssh root@51.77.201.82 "journalctl -u clawshow-mcp --since '5 minutes ago' --no-pager | tail -20"

# Stats endpoint
curl https://mcp.clawshow.ai/stats
```

## Environment Variables (.env)

Located at `/opt/clawshow-mcp-server/.env` (never committed to git):

```
GITHUB_TOKEN=ghp_xxx          # GitHub PAT — repo + pages scopes
STRIPE_SECRET_KEY=sk_test_xxx  # Stripe secret key
STRIPE_WEBHOOK_SECRET=whsec_xxx # Stripe webhook signing secret
RESEND_API_KEY=re_xxx          # Resend email API key
```

See `.env.example` in repo for template.

## systemd Service

File: `/etc/systemd/system/clawshow-mcp.service`

```ini
[Unit]
Description=ClawShow MCP Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/clawshow-mcp-server
ExecStart=/opt/clawshow-mcp-server/venv/bin/python server.py
Restart=on-failure
RestartSec=5
Environment=PATH=/opt/clawshow-mcp-server/venv/bin:/usr/local/bin:/usr/bin

[Install]
WantedBy=multi-user.target
```

Commands:
```bash
systemctl start clawshow-mcp
systemctl stop clawshow-mcp
systemctl restart clawshow-mcp
systemctl status clawshow-mcp
journalctl -u clawshow-mcp -f    # live logs
```

## Nginx

Config: `/etc/nginx/sites-enabled/mcp.clawshow.ai`

Reverse proxy `mcp.clawshow.ai` → `localhost:8000`. SSL via Let's Encrypt.

Key routes:
- `/sse` → MCP SSE stream
- `/messages/` → MCP message endpoint
- `/stats` → JSON stats
- `/webhook/stripe` → Stripe webhook
- `/reports/` → PDF files

## SSL Certificate

Let's Encrypt via certbot, auto-renew:
```bash
certbot renew --dry-run    # test
certbot renew              # actual
```

## Rollback

```bash
# Find previous commit
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git log --oneline -5"

# Rollback to specific commit
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git checkout <commit-hash> && systemctl restart clawshow-mcp"

# Return to latest
ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git checkout main && git pull origin main && systemctl restart clawshow-mcp"
```
