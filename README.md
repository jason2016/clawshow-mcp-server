# clawshow-mcp-server

ClawShow MCP Server — production endpoint: `mcp.clawshow.ai/sse`

Hosts AI-callable tools for the ClawShow discovery layer.

## Available Tools

| Tool | Description |
|------|-------------|
| `generate_rental_website` | Generate a complete static HTML rental website from property data |
| `extract_finance_fields` | Extract vendor, amount, currency, due_date, category from invoice text |

## Local Testing

### Option A — Claude Desktop (stdio, simplest)

Add to `~/AppData/Roaming/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "clawshow": {
      "command": "python",
      "args": ["C:\\Users\\luqia\\OneDrive\\Development\\ClawShow-Workspace\\repos\\clawshow-mcp-server\\server.py", "--stdio"]
    }
  }
}
```

Restart Claude Desktop → look for the hammer icon → ClawShow tools appear.

### Option B — SSE server (browser / remote client)

```bash
cd repos/clawshow-mcp-server
pip install -r requirements.txt
python server.py
# → http://localhost:8000/sse
```

## Production Deployment (Railway)

1. Push this repo to GitHub
2. New Railway project → Deploy from GitHub repo
3. Railway auto-detects `railway.toml` and runs `python server.py`
4. Add custom domain: `mcp.clawshow.ai` → Railway service URL
5. Done — Claude users add `https://mcp.clawshow.ai/sse` in Settings > Integrations

## File Structure

```
clawshow-mcp-server/
├── server.py              # MCP server entry point, tool registration
├── tools/
│   ├── rental_website.py  # generate_rental_website tool
│   └── finance_extract.py # extract_finance_fields tool
├── data/
│   └── usage_log.json     # call tracking (auto-created, gitignored)
├── requirements.txt
├── railway.toml
└── .env.example
```
