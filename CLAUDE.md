# ClawShow MCP Server

> Repo is the system of record. If it's not here, it doesn't exist.

## Quick Start

```
Server:    https://mcp.clawshow.ai/sse
Stack:     Python 3.12 + FastMCP + Starlette + Uvicorn
VPS:       root@51.77.201.82 (stand9, Ubuntu 24.04)
Deploy:    ssh root@51.77.201.82 "cd /opt/clawshow-mcp-server && git pull origin main && systemctl restart clawshow-mcp"
Test:      curl https://mcp.clawshow.ai/stats
```

## Current Tools (v1.3.0 — 8 Tools, 6 Engines)

| Tool | Engine | File |
|------|--------|------|
| `generate_business_page` | Page Engine | `tools/business_page.py` |
| `generate_rental_website` | Page Engine (React) | `tools/rental_website.py` |
| `generate_stripe_payment` | Payment Engine | `tools/stripe_payment.py` |
| `send_notification` | Notification Engine | `tools/notification.py` |
| `manage_orders` | Order Engine | `tools/orders.py` |
| `manage_inventory` | Inventory Engine | `tools/inventory.py` |
| `generate_report` | Report Engine | `tools/report.py` |
| `extract_finance_fields` | Finance Extraction | `tools/finance_extract.py` |

## Architecture

→ [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Three-layer design, engine admission criteria, data flow

## Strategy

→ [docs/STRATEGY.md](docs/STRATEGY.md) — "Instant Backend" positioning, 6 engines, 4 workflows, industry matrix, roadmap

## Key Decisions

→ [docs/DECISIONS.md](docs/DECISIONS.md) — All architectural and business decisions with dates and rationale

## Tool Development

→ [docs/TOOL_GUIDE.md](docs/TOOL_GUIDE.md) — How to build a new Tool: naming, structure, error handling, testing

## Deployment

→ [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — stand9 server, systemd, Nginx, SSL, env vars, rollback

## Changelog

→ [docs/CHANGELOG.md](docs/CHANGELOG.md) — Version history from step1-complete to v1.3.0

## Environment Variables

→ [.env.example](.env.example) — All required keys: GITHUB_TOKEN, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, RESEND_API_KEY

## HTTP Endpoints (beyond MCP)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/stats` | GET | Tool call counts JSON |
| `/webhook/stripe` | POST | Stripe payment webhook |
| `/reports/{ns}/{file}` | GET | Serve generated PDF reports |

## Team Workflow

| Role | Who | Scope |
|------|-----|-------|
| CEO | Jason | Customer decisions, pricing, priorities |
| CTO | Claude.ai (Opus) | Strategy, architecture, planning |
| Engineer | Claude Code (Sonnet) | Code, deploy, debug, SSH to stand9 |

## Workflow (Sprint Process)

### Feature Development
1. **Design:** CEO+CTO discuss in Claude.ai → output: decision in `docs/DECISIONS.md`
2. **Spec:** CTO writes Claude Code prompt → output: clear requirements
3. **Build:** Claude Code implements → output: code + tests
4. **Deploy:** Claude Code SSH to stand9 → output: service running
5. **Verify:** CEO tests in Claude.ai → output: confirmed working
6. **Tag:** Claude Code creates git tag → output: stable version

### Bug Fix
1. CEO reports issue with screenshot
2. CTO writes debug prompt for Claude Code
3. Claude Code SSH to stand9, diagnose, fix, restart
4. CEO re-tests

### Key Rule
Every decision from Claude.ai chat MUST be written to `docs/DECISIONS.md` before implementation. If it's not in the repo, it doesn't exist for the agent.

## Rules

- Every Tool must pass 4 admission criteria: AI can't do it alone + has deliverable + zero human intervention + sell results not tools (Sequoia)
- Tool descriptions MUST be in English (global AI discoverability)
- All data isolated by `namespace` parameter
- Don't add engines — 6 is final. Workflows orchestrate, not replace.
- `docs/DECISIONS.md` is append-only. Record before you build.
