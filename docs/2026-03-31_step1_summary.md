# Step 1 完成总结

**日期：** 2026-03-31
**里程碑：** MCP Server 骨架搭建完成，2 个 Tool 注册并本地验证通过

---

## 已完成内容

### 新建仓库：`clawshow-mcp-server`

| 文件 | 说明 |
|------|------|
| `server.py` | FastMCP 主入口，支持 `--stdio`（Claude Desktop）和 SSE（Railway）双模式 |
| `tools/rental_website.py` | `generate_rental_website` Tool |
| `tools/finance_extract.py` | `extract_finance_fields` Tool（从 clawshow-finance-skill 移植） |
| `data/` | 调用记录目录（usage_log.json，运行时自动生成） |
| `railway.toml` | Railway 部署配置，`python server.py` 启动 |
| `requirements.txt` | 唯一依赖：`mcp[cli]>=1.26.0` |

### 验证通过

- `tools/list` 正确返回 2 个 Tool
- `generate_rental_website` 输出正确 HTML，包含 Tailwind CDN
- `extract_finance_fields` 字段提取全部正确（vendor, amount, currency, due_date, category）
- SSE server 在 8000 端口正常启动（`python server.py`）
- stdio 模式可用于 Claude Desktop 本地测试（`python server.py --stdio`）

### 同步更新：CLAUDE.md

- 新增 **Section 2：Zero Human Intervention 核心原则**（不可违反）
- 仓库 Truth Table 更新，加入 `clawshow-mcp-server` 为新 P0
- 路径索引、功能边界速查全部更新

---

## 不合规 Skill 清单（Zero Human Intervention 原则）

### ❌ `generate_rental_website`

**当前返回：** HTML 字符串（完整 `<html>...</html>` 文档）

**问题：** 用户拿到 HTML 后还需要自己找地方部署，才能生成可访问 URL。有人工介入步骤。

**合规目标：** 自动调用 Vercel Deploy API，将 HTML 部署为静态站点，返回 `https://xxx.vercel.app`。用户拿到 URL 直接发给客户，零操作。

---

### ❌ `extract_finance_fields`

**当前返回：** `{"vendor": "...", "amount": 1620.0, "currency": "USD", "due_date": "...", "category_guess": "..."}` 原始 JSON

**问题：** 用户拿到原始字段后还需要自己写账目、做摘要或汇总报告。

**合规目标：** 返回完整可读的财务摘要文本，例如：

```
Invoice Summary
───────────────────────────────────
Vendor:   Acme Corp
Amount:   USD 1,620.00
Due:      2026-04-14
Category: Software
───────────────────────────────────
Action: Schedule payment before April 14.
```

进阶：直接写入 Google Sheets / 发邮件给会计 → 返回操作确认。

---

### ❌ `draft_reply_from_context`（email skill，仅文档，未实现）

**当前设计返回：** `draft_body`（草稿文本）

**问题：** 用户拿到草稿还需要自己复制粘贴并发送。

**合规目标：** 调用 Gmail API 直接发送邮件 → 返回发送确认（邮件 ID + 时间戳）。

---

## 下一步：generate_rental_website 返回 Vercel URL（Step 2）

### 目标

用户输入房产信息 → Tool 生成 HTML → 自动部署到 Vercel → 返回可访问 URL

### 技术路径

1. **Vercel Deploy API**：`POST https://api.vercel.com/v13/deployments`
   - 上传单个 `index.html` 文件作为静态部署
   - 需要 Vercel API Token（环境变量 `VERCEL_TOKEN`）
   - 返回 `url` 字段（例如 `clawshow-abc123.vercel.app`）
   - 无需 Vercel 账号中预先配置项目，每次 deploy 自动创建

2. **依赖**：只需 `httpx`（已在 mcp 依赖树中，或单独添加）

3. **环境变量**：Railway 中配置 `VERCEL_TOKEN`，本地测试用 `.env`

### 执行顺序

1. 用户提供 Vercel API Token
2. 在 `tools/rental_website.py` 中加入 `_deploy_to_vercel(html: str) -> str` 函数
3. `generate_rental_website` Tool 改为返回 URL 字符串
4. 本地测试：Claude Desktop 调用 → 拿到真实 URL → 浏览器打开验证
5. 发给 Florent 测试

### 前置条件

- [ ] Vercel 账号（免费即可）
- [ ] 生成 Vercel API Token（账号 Settings → Tokens）
- [ ] Token 配置到本地 `.env` 和 Railway 环境变量
