# Changelog

All notable changes to ClawShow MCP Server are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.9.0] — 2026-04-09

### Added — External PDF Signing (FocusingPro / file_url mode)

- **`send_esign_request` MCP Tool expanded**: new parameters `file_url`, `signers` (JSON array), `signature_fields`, `expiration_days`, `reminder_frequency`
- **`file_url` mode**: pass an S3 pre-signed URL (or any HTTPS PDF) to sign externally-generated contracts without a ClawShow template
- **Multi-party signers array**: `[{role, name, email, order}]` — supports dual-party and custom signing workflows
- **S3 upload on completion**: signed PDF automatically uploaded to `{namespace}/esign/{document_id}-signed.pdf` in configured S3 bucket (boto3, AWS credentials from .env)
- **`s3_url` persisted** in `esign_documents` table and returned by `/esign/{id}/status`
- **FocusingPro webhook field mapping**: `document.completed` payload now includes `focusingpro_fields` dict with 4 GUIDs mapping to status string, signed_at, effective date (+1 day 09:00), and s3_url
- **`_generate_page_images()`** added to `tools/esign.py` (PyMuPDF 2x zoom PNG render per page)
- **`_generate_pdf()`** now returns `(html_path, pdf_path, total_pages)` 3-tuple
- **`db.create_esign_document()`** accepts `total_pages`, `signature_positions`, `initial_status`

### Fixed

- `db.py` migration block structure corrected for `s3_url` column addition

---

## [1.8.0] — 2026-04-09

### Added — eSign AES (Advanced Electronic Signature) Compliance

- **OTP email verification gate** before signing page: 6-digit code, 10-min expiry, 5-attempt lockout (30 min)
- **PDF digital signature** (pyHanko + SHA-256 + X.509 self-signed cert): tamper-detectable post-signing
- `esign_otp` table for OTP management (code, expiry, attempts, lock)
- `otp_verified_at` column on `esign_signers` for session persistence
- `/otp/send` and `/otp/verify` REST endpoints
- `/verify` endpoint: document integrity check (cert info + all signers + otp_verified flag)
- `/audit` endpoint: full audit log per document
- AES badge (`🔒 AES`) in signing page header
- AES compliance footer in signing page (eIDAS Art. 26 + SHA-256 notice + link to clawshow.ai/esign)
- AES details box in signature confirmation page (OTP ✓, SHA-256 ✓, timestamp)
- OTP page AES explanation text
- eSign AES compliance page at [clawshow.ai/esign](https://clawshow.ai/esign)

### Fixed

- Document ID UNIQUE constraint collision: replaced `COUNT(*)+1` with `max(existing_nums)+1`
- Tokenless signing URLs now 302-redirect to token-bearing URL (OTP gate was bypassable)
- `digitally_sign_pdf` 0-byte output bug: `ds_pdf` path used wrong `.replace()` anchor causing input=output file truncation

---

## [1.7.0] — 2026-04-09

### Added — eSign V2: Complete Electronic Signature Platform

#### Core signing engine
- Multi-page PDF signing: per-page paraphes (initials) + full signature block on last page
- Three signature input modes: **Draw** (smooth Bézier quadraticCurveTo), **Type** (styled font preview), **Upload** (image file)
- Real-time progress bar showing remaining required fields
- Pen/pencil SVG cursor on all signing canvases (replaces crosshair)
- City field + "lu et approuvé" handwritten confirmation on last page
- Legal checkboxes (conditions + email consent) before submission

#### Dual-party counter-signing flow
- **PATH B** — Student signs: saves paraphes + signature, sets status `student_signed`, emails school admin with counter-sign URL, fires `signer.signed` webhook
- **PATH A** — School admin counter-signs: loads student data from DB, generates final PDF with both parties' signatures overlaid, sets status `completed`, sends completion emails to both parties, fires `document.completed` webhook
- **PATH C** — Single-signer: immediate PDF generation + `document.completed` (backward compatible)
- School signing page shows student paraphes as read-only overlays; only school signature zone is active

#### Webhook system
- Events: `signer.signed`, `document.completed`, `document.expired`
- 3-attempt retry with exponential backoff (1s, 2s, 4s)
- Headers: `X-ClawShow-Event`, `X-ClawShow-Document-Id`, `X-ClawShow-Timestamp`
- All attempts logged to audit trail

#### Audit trail
- Every action recorded: `created`, `viewed`, `student_signed`, `school_signed`, `expired`, `reminder_sent`, `webhook_sent`, `webhook_failed`, `webhook_error`
- Stored with signer ID, IP address, city, timestamp

#### Document expiration
- `check_pending_reminders()` cron now handles `student_signed` status (not only `student_signing`)
- On expiry: updates status to `expired`, emails all pending signers, fires `document.expired` webhook

#### PDF overlay engine (`_overlay_signatures_pdf`)
- Paraphe (120×40pt min, actual 160×48pt) rendered on each page, right-aligned
- Final signature block (200×70pt min, actual 220×70pt) on last page with signer name + date footer
- School counter-signature block (200×70pt) on last page, left-aligned, with admin name + date footer
- Scale-aware rendering for non-A4 PDFs

#### Email notifications
- School notification email: French HTML template with counter-sign CTA button
- Completion email: sent to both student and school with signed PDF download link
- Expiration email: French/English localized

#### FocusingPro compatibility
- `inscription_send_foxit_esign` tool routes to `send_esign_request` as drop-in replacement
- Enrollment contract template with student/school dual-party support
- `role` field on signers: `student`, `school_admin`

### Changed
- `esign_submit_signature` fully rewritten as role-aware three-path handler
- `_render_signing_page` detects `school_admin` token and injects `school_mode: true` + `student_paraphes` config
- JS success handler differentiates `student_signed` (shows "awaiting school") vs `completed` (shows download button)
- `get_pending_esign_documents` now includes `student_signed` status for cron processing

---

## [1.6.0] — 2026-04-07

### Added
- `send_esign_request` tool — V1 electronic signature (single-signer, template-based)
- `generate_payment` — merged Stripe + Stancer + SumUp into single tool
- `verify_payment` — unified payment status check
- FocusingPro MCP server integration

### Changed
- Removed `stripe_payment.py` (merged into `generate_payment`)
- All tool descriptions rewritten for AI-first discovery

---

## [1.5.0] — 2026-03-15

### Added
- `send_notification` — email, SMS, WhatsApp with dunning templates
- `manage_bookings`, `manage_orders`, `manage_inventory` tools
- `generate_report` — PDF business reports
- `extract_finance_fields` — invoice/receipt field extraction

---

## [1.0.0] — 2026-02-01

### Added
- Initial release: `generate_business_page`, `generate_rental_website`
- MCP SSE server on `mcp.clawshow.ai/sse`
- Namespace isolation, auto-provisioning
