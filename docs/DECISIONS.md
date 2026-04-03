# Key Decisions

Append-only. Record before you build.

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-03-31 | MCP Server as the core product, not a standalone API | Standard protocol, Claude native integration, zero-config for users |
| 2026-03-31 | Zero Human Intervention as inviolable principle | If user needs manual steps after tool call, the tool is broken |
| 2026-03-31 | GitHub Pages for page deployment (not Vercel/Netlify) | Free, no API key for public repos, reliable |
| 2026-04-01 | Static HTML mode for business_page (not React) | 60s deploy vs 3min, no Actions dependency, simpler |
| 2026-04-02 | 6 engines is final — no new engines | Covers all SMB needs. Workflows orchestrate, not replace. |
| 2026-04-02 | 7th engine (automate_accounting) deferred to Phase 4 | Too complex for MVP, requires accounting domain knowledge |
| 2026-04-02 | Development priority: orders → inventory → report | Orders are revenue-critical, inventory supports orders, reports are read-only |
| 2026-04-02 | Florent pricing: €600 setup + €250/mo + €39/mo hosting | First paying client, price reflects real value not market rate |
| 2026-04-02 | Florent is seed client, core value is commercial validation | Proving the model works matters more than revenue |
| 2026-04-02 | Namespace isolation for all stateful data | Each client's data in separate directories, no cross-contamination |
| 2026-04-02 | Stripe keys managed globally (future: per-namespace) | MVP simplification, Florent is only paying client |
| 2026-04-02 | Page engine has built-in GEO (llms.txt + JSON-LD Schema) | Every generated page is AI-discoverable by default |
| 2026-04-02 | Notification engine: email only for now (Resend) | SMS/WhatsApp via Twilio planned but not yet needed |
| 2026-04-02 | Payment engine must include Stripe Webhook | Auto payment recording is the core differentiator vs manual links |
| 2026-04-02 | Workflows are orchestration layer, not a 7th engine | They call existing engines in sequence, no new external integration |
| 2026-04-02 | FocusingPro invisible to AI | AI knows ClawShow only. Backend persistence is an implementation detail |
| 2026-04-02 | Team roles: CEO (Jason) decides customers, CTO (Claude.ai Opus) does strategy/architecture, Engineer (Claude Code Sonnet) writes code/deploys/debugs/SSH | Clear separation prevents role confusion |
| 2026-04-02 | Tool descriptions must be English | Global AI discoverability. French examples in triggers are fine. |
| 2026-04-02 | Reports served via /reports/{ns}/{file} HTTP endpoint | Simple, no CDN needed, PDF files are small |
| 2026-04-02 | reportlab for PDF generation (not weasyprint) | Pure Python, no system dependencies, works on any VPS |
