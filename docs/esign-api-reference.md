# ClawShow eSign API Reference

**Base URL:** `https://esign-api.clawshow.ai`  
**Current version:** v1 (2026-04-18)  
**Compliance:** AES — Advanced Electronic Signature, eIDAS Article 26

---

## Quick Start — First signing in 5 minutes

### Step 1 — Create an account and get your API key

1. Sign up at [app.clawshow.ai](https://app.clawshow.ai)
2. Go to **Settings → API Keys** → click **+ Create new key**
3. Copy your API key (shown once — save it now)
4. Note your **Namespace** from **Settings → Account**

### Step 2 — Create a signing request

```bash
curl -X POST https://esign-api.clawshow.ai/esign/create \
  -H "Authorization: Bearer YOUR_CLAWSHOW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "your-namespace",
    "signer_name": "Jane Doe",
    "signer_email": "jane@example.com",
    "template": "enrollment_contract",
    "fields": {
      "student_name": "Jane Doe",
      "program": "Bachelor of Computer Science"
    },
    "reference_id": "YOUR_INTERNAL_REF_001",
    "callback_url": "https://your-server.com/webhooks/esign"
  }'
```

**Response:**

```json
{
  "success": true,
  "document_id": "esign_2026_0042",
  "signing_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042?token=...",
  "pdf_preview_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/preview.pdf",
  "total_pages": 3,
  "status": "student_signing",
  "signer_email": "jane@example.com",
  "signers": [
    {
      "role": "student",
      "name": "Jane Doe",
      "email": "jane@example.com",
      "order": 1,
      "status": "pending",
      "signing_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042?token=..."
    }
  ]
}
```

### Step 3 — Signer receives email, opens the signing page, signs

The signer clicks the link in their email. The signing page handles OTP verification, document review, initials per page, and final signature. No additional integration needed on your side.

### Step 4 — Your server receives the webhook

```json
{
  "event": "signer.signed",
  "provider": "clawshow_esign",
  "document_id": "esign_2026_0042",
  "reference_id": "YOUR_INTERNAL_REF_001",
  "namespace": "your-namespace",
  "status": "student_signed",
  "signer": {
    "role": "student",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "signed_at": "2026-04-18T10:30:00+00:00",
    "ip": "82.65.12.34"
  },
  "signed_pdf_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/signed.pdf",
  "audit_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/audit"
}
```

### Step 5 — Download the signed PDF

```bash
curl https://esign-api.clawshow.ai/esign/esign_2026_0042/signed.pdf \
  -o signed-contract.pdf
```

---

## Overview

ClawShow eSign is a REST API for sending, signing, and archiving electronic documents. It is designed for B2B SaaS integration: you send a request from your backend, the signer receives an email with a secure link, signs on a web page, and your server receives a webhook. The entire signing flow — OTP double-authentication, document review, per-page initials, digital signature embedding, audit log — happens inside ClawShow.

### What ClawShow handles

- PDF generation from templates or your uploaded PDF
- Secure per-document signing link with one-time token
- OTP email verification before allowing the signer to proceed
- Per-page paraphes (initials) + final handwritten signature capture
- PAdES digital signature embedding via pyHanko
- Complete audit trail: IP addresses, timestamps, user actions
- Webhook notification to your server on completion

### What you handle

- Calling `POST /esign/create` from your backend
- Receiving and processing the webhook
- Storing the `document_id` for later status queries

### Compliance

ClawShow eSign meets **eIDAS Article 26** (Advanced Electronic Signature):

| Requirement | Implementation |
|---|---|
| Linked to signatory | Per-document UUID token + OTP email verification |
| Capable of identifying signatory | Email verified, name captured |
| Data integrity | pyHanko PAdES digital signature; any modification invalidates signature |
| Under sole control of signatory | Link is single-use, OTP is time-limited |
| Audit trail | IP, timestamp, User-Agent logged for every action |

**QES (Qualified Electronic Signature)** with QTSP integration is on the roadmap for use cases requiring the highest legal level.

---

## Authentication

All requests to `POST /esign/create` and document management endpoints must include your API key.

### API Key

```http
Authorization: Bearer <YOUR_CLAWSHOW_API_KEY>
```

**Obtaining a key:**
1. Log in to [app.clawshow.ai/settings](https://app.clawshow.ai/settings)
2. Under **API Keys**, click **+ Create new key**
3. Give it a name (e.g., "Production", "Staging")
4. The full key is shown **once** — copy it immediately

**Properties:**
- Format: `sk_live_` prefix followed by 43 random characters
- Stored as SHA-256 hash — the raw key is never retrievable after creation
- Bound to your namespace
- Revocable from the Dashboard at any time

### Namespace

Every API request requires a `namespace` parameter in the request body. The namespace is your data isolation unit — all documents created in a namespace are accessible only within that namespace.

Your namespace is visible in **Dashboard → Settings → Account**.

```json
{
  "namespace": "your-namespace",
  ...
}
```

You can also pass it as a header:

```http
X-Namespace: your-namespace
```

---

## Core Concepts

| Concept | Definition |
|---|---|
| **Namespace** | Data isolation unit. All documents, signers, and audit logs belong to a namespace. One organization can use one or more namespaces. |
| **Document** | A single signing request. Contains one PDF, one signer (current version), and a full audit trail. Identified by `document_id` (e.g., `esign_2026_0042`). |
| **Template** | A named contract layout ClawShow uses to generate the PDF. Templates define the document structure and where fields are placed. Managed internally. |
| **Signer** | The person who must sign the document. Receives a signing link by email. Authenticated via OTP before they can sign. |
| **Signing URL** | A one-time secure URL sent to the signer. Contains a UUID token that is invalidated once used. |
| **Callback URL** | Your server endpoint. ClawShow POSTs a JSON payload here when the document is signed or declined. |
| **Reference ID** | Your own identifier for the document (e.g., your internal order ID). Returned verbatim in webhooks to help you correlate events. |
| **Audit Log** | A tamper-evident log of every action on a document: created, email sent, opened, signed, declined, webhook fired. |

---

## Document Status

```
student_signing  →  completed
     ↓
  declined
```

| Status | Meaning |
|---|---|
| `student_signing` | Document created, awaiting signer action |
| `completed` | Signer has signed; signed PDF is available |
| `declined` | Signer explicitly refused to sign |

> **Note:** Expiration-based status transitions (auto-expiring unsigned documents after `expiration_days`) are enforced at query time — the stored status remains `student_signing` until explicitly checked.

---

## API Endpoints

### POST /esign/create

Create a signing request and (optionally) send the signing email to the signer.

#### Request

```http
POST /esign/create
Authorization: Bearer <YOUR_CLAWSHOW_API_KEY>
Content-Type: application/json
```

**Body parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `namespace` | string | Yes | — | Your namespace |
| `signer_name` | string | Yes* | — | Full name of the signer |
| `signer_email` | string | Yes* | — | Email address of the signer |
| `template` | string | No | `enrollment_contract` | Template to use for PDF generation |
| `fields` | object | No | `{}` | Template field values (key-value pairs filled into the document) |
| `reference_id` | string | No | `""` | Your internal identifier — returned verbatim in webhooks |
| `callback_url` | string | No | `""` | Your server endpoint to receive webhook notifications |
| `language` | string | No | `fr` | Signing page language. Currently: `fr` (French), `en` (English) |
| `expiration_days` | integer | No | `30` | Days until the signing link expires |
| `reminder_frequency` | string | No | `EVERY_THIRD_DAY` | Email reminder cadence. Options: `EVERY_DAY`, `EVERY_THIRD_DAY`, `WEEKLY`, `NEVER` |
| `send_email` | boolean | No | `true` | Set to `false` to create the document without sending the email (you handle delivery) |
| `file_url` | string | No | — | URL of an existing PDF to use instead of generating from a template |
| `signers` | array | No | — | Alternative to `signer_name`/`signer_email`. See multi-signer format below. |
| `signature_fields` | array | No | — | Override signature placement. See signature fields format below. |

> *`signer_name` and `signer_email` are required unless you use the `signers` array format.

**Using an external PDF (`file_url`):**

Instead of generating from a template, you can provide a URL to an existing PDF. ClawShow downloads it, converts to images for display, and applies the signature overlay on completion.

```json
{
  "namespace": "your-namespace",
  "file_url": "https://your-server.com/contracts/contract-001.pdf",
  "signers": [
    { "name": "Jane Doe", "email": "jane@example.com", "role": "student", "order": 1 }
  ],
  "reference_id": "contract-001",
  "callback_url": "https://your-server.com/webhooks/esign"
}
```

**Fields object example:**

```json
{
  "fields": {
    "student_name": "Jane Doe",
    "program": "Bachelor of Computer Science",
    "start_date": "September 2026",
    "tuition_fee": "€12,500"
  }
}
```

Fields are inserted into the template. Available fields depend on the template. Contact support for template documentation.

#### Response — 201 Created

```json
{
  "success": true,
  "document_id": "esign_2026_0042",
  "signing_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042?token=uuid4",
  "pdf_preview_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/preview.pdf",
  "total_pages": 3,
  "status": "student_signing",
  "signer_email": "jane@example.com",
  "signers": [
    {
      "role": "student",
      "name": "Jane Doe",
      "email": "jane@example.com",
      "order": 1,
      "status": "pending",
      "signing_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042?token=..."
    }
  ]
}
```

#### Errors

| HTTP | Code | Description |
|---|---|---|
| 400 | `Invalid JSON` | Request body is not valid JSON |
| 400 | `signer_name is required` | Missing signer name (native format) |
| 400 | `signers array is required with file_url` | `file_url` requires the `signers` array |
| 400 | `Failed to download file_url: ...` | ClawShow could not fetch your PDF URL |
| 402 | `Envelope quota exceeded` | Your namespace has used all envelopes for this billing period |
| 500 | `PDF generation failed: ...` | Template rendering error |

#### Code examples

**Python:**

```python
import requests

response = requests.post(
    "https://esign-api.clawshow.ai/esign/create",
    headers={
        "Authorization": "Bearer YOUR_CLAWSHOW_API_KEY",
        "Content-Type": "application/json",
    },
    json={
        "namespace": "your-namespace",
        "signer_name": "Jane Doe",
        "signer_email": "jane@example.com",
        "template": "enrollment_contract",
        "fields": {
            "student_name": "Jane Doe",
            "program": "Bachelor of Computer Science",
        },
        "reference_id": "order-001",
        "callback_url": "https://your-server.com/webhooks/esign",
    },
)

data = response.json()
document_id = data["document_id"]
signing_url = data["signing_url"]
```

**Node.js:**

```javascript
const response = await fetch("https://esign-api.clawshow.ai/esign/create", {
  method: "POST",
  headers: {
    Authorization: "Bearer YOUR_CLAWSHOW_API_KEY",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    namespace: "your-namespace",
    signer_name: "Jane Doe",
    signer_email: "jane@example.com",
    template: "enrollment_contract",
    fields: {
      student_name: "Jane Doe",
      program: "Bachelor of Computer Science",
    },
    reference_id: "order-001",
    callback_url: "https://your-server.com/webhooks/esign",
  }),
});

const { document_id, signing_url } = await response.json();
```

**PHP:**

```php
$response = \Httpful\Request::post("https://esign-api.clawshow.ai/esign/create")
    ->addHeader("Authorization", "Bearer YOUR_CLAWSHOW_API_KEY")
    ->contentType("application/json")
    ->body(json_encode([
        "namespace"    => "your-namespace",
        "signer_name"  => "Jane Doe",
        "signer_email" => "jane@example.com",
        "template"     => "enrollment_contract",
        "fields"       => ["student_name" => "Jane Doe"],
        "reference_id" => "order-001",
        "callback_url" => "https://your-server.com/webhooks/esign",
    ]))
    ->send();

$data = $response->body;
$document_id = $data->document_id;
```

---

### GET /esign/{document_id}/status

Return the current status of a document, including per-signer status and links.

#### Request

```http
GET /esign/esign_2026_0042/status
```

No authentication required. The `document_id` acts as a resource identifier.

#### Response — 200 OK

```json
{
  "document_id": "esign_2026_0042",
  "status": "completed",
  "signer_name": "Jane Doe",
  "signer_email": "jane@example.com",
  "total_pages": 3,
  "signed_at": "2026-04-18T10:30:00.123456+00:00",
  "completed_at": "2026-04-18T10:30:01.000000+00:00",
  "city": "Paris",
  "reference_id": "order-001",
  "signed_pdf_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/signed.pdf",
  "created_at": "2026-04-18T09:00:00.000000+00:00",
  "signers": [
    {
      "role": "student",
      "name": "Jane Doe",
      "email": "jane@example.com",
      "status": "signed",
      "signed_at": "2026-04-18T10:30:00.123456+00:00",
      "signing_order": 1
    }
  ],
  "audit_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/audit"
}
```

| Field | Description |
|---|---|
| `status` | `student_signing`, `completed`, or `declined` |
| `signed_pdf_url` | Present only when `status == "completed"` |
| `city` | City entered by the signer at signing time |
| `signers` | Array of signer records with individual status |

#### Errors

| HTTP | Description |
|---|---|
| 404 | Document not found |

---

### GET /esign/{document_id}/audit

Return the complete audit trail for a document as a JSON array.

#### Request

```http
GET /esign/esign_2026_0042/audit
```

#### Response — 200 OK

```json
{
  "document_id": "esign_2026_0042",
  "events": [
    {
      "action": "created",
      "detail": {
        "signer_name": "Jane Doe",
        "signer_email": "jane@example.com",
        "total_pages": 3,
        "signers_count": 1
      },
      "ip": null,
      "at": "2026-04-18T09:00:00.000000"
    },
    {
      "action": "signed",
      "detail": {
        "city": "Paris",
        "pages_paraphed": [1, 2, 3],
        "signer_ip": "82.65.12.34"
      },
      "ip": "82.65.12.34",
      "at": "2026-04-18T10:30:00.000000"
    },
    {
      "action": "webhook_sent",
      "detail": {
        "event": "signer.signed",
        "attempt": 1,
        "status": 200
      },
      "ip": null,
      "at": "2026-04-18T10:30:01.000000"
    }
  ]
}
```

**Audit event types:**

| Action | Triggered when |
|---|---|
| `created` | Document was created via API |
| `signed` | Signer submitted their signature |
| `declined` | Signer refused to sign |
| `webhook_sent` | Webhook delivered successfully to callback URL |
| `webhook_failed` | Webhook delivery received non-200 response |
| `webhook_error` | Network error reaching callback URL |

---

### GET /esign/{document_id}/preview.pdf

Download the unsigned PDF.

```http
GET /esign/esign_2026_0042/preview.pdf
```

Returns the original PDF as `application/pdf`. Use this to show users a preview of what they will be signing.

---

### GET /esign/{document_id}/signed.pdf

Download the signed PDF (with embedded digital signature and paraphes).

```http
GET /esign/esign_2026_0042/signed.pdf
```

Returns `application/pdf` with `Content-Disposition: attachment`. Only available when the document `status` is `completed`. Returns 404 otherwise.

---

### GET /esign-documents/recent

List recent documents for a namespace. Used for dashboard activity feed.

#### Request

```http
GET /esign-documents/recent?namespace=your-namespace&limit=10
Authorization: Bearer <YOUR_CLAWSHOW_API_KEY>
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `namespace` | string | — | Required. Your namespace. |
| `limit` | integer | `10` | Max 50 documents. |

#### Response — 200 OK

```json
[
  {
    "id": "esign_2026_0042",
    "reference_id": "order-001",
    "signer_name": "Jane Doe",
    "signer_email": "jane@example.com",
    "status": "completed",
    "created_at": "2026-04-18T09:00:00",
    "signed_at": "2026-04-18T10:30:00",
    "completed_at": "2026-04-18T10:30:01"
  }
]
```

---

## Webhooks

ClawShow sends an HTTP POST to your webhook URL when a document is signed or declined.

### Configuration

There are two ways to configure a webhook URL — use whichever fits your integration:

#### Option A — Namespace-level (recommended)

Configure once in **Dashboard → Settings → Webhook**. All documents in your namespace will fire to this URL automatically. No changes needed per API call.

```
Dashboard: https://app.clawshow.ai/settings
Section: Webhook
Input: https://your-server.com/webhooks/esign
```

You can also set it via API:

```http
PATCH /webhooks/config
Authorization: Bearer <YOUR_CLAWSHOW_API_KEY>
Content-Type: application/json

{
  "namespace": "your-namespace",
  "webhook_url": "https://your-server.com/webhooks/esign"
}
```

Response:
```json
{
  "namespace": "your-namespace",
  "webhook_url": "https://your-server.com/webhooks/esign",
  "updated": true
}
```

To read the current config:
```http
GET /webhooks/config?namespace=your-namespace
Authorization: Bearer <YOUR_CLAWSHOW_API_KEY>
```

To remove it, send an empty string:
```json
{ "namespace": "your-namespace", "webhook_url": "" }
```

#### Option B — Per-document override

Pass `callback_url` in the `POST /esign/create` body. This takes priority over the namespace-level URL for that specific document.

**Priority:** per-document `callback_url` → namespace-level `webhook_url`

### Events

| Event | Trigger |
|---|---|
| `signer.signed` | Signer completed signing |
| `signer.declined` | Signer explicitly refused |

### Payload format

```json
{
  "event": "signer.signed",
  "provider": "clawshow_esign",
  "document_id": "esign_2026_0042",
  "reference_id": "YOUR_INTERNAL_REF",
  "namespace": "your-namespace",
  "status": "student_signed",
  "signer": {
    "role": "student",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "signed_at": "2026-04-18T10:30:00+00:00",
    "ip": "82.65.12.34"
  },
  "signed_pdf_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/signed.pdf",
  "audit_url": "https://esign-api.clawshow.ai/esign/esign_2026_0042/audit"
}
```

**Headers sent with every webhook request:**

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `X-ClawShow-Event` | Event name (e.g., `signer.signed`) |
| `X-ClawShow-Document-Id` | Document ID |
| `X-ClawShow-Timestamp` | ISO 8601 timestamp of the event |

### Retry policy

If your server returns a non-2xx response or times out, ClawShow retries:

- Attempt 1: immediate
- Attempt 2: after 1 second
- Attempt 3: after 2 seconds

After 3 failed attempts, the event is recorded in the audit log as `webhook_error` but no further retries occur. You can poll `/esign/{id}/status` at any time to recover the current state.

> **Roadmap 🔜** — Extended retry schedule (1min, 5min, 30min, 2h, 12h), Dashboard manual replay, and HMAC request signing are planned.

### Receiving webhooks

Your endpoint must:
- Return HTTP 2xx within 10 seconds
- Handle duplicate delivery (use `document_id` + `event` as idempotency key)
- Not depend on `reference_id` being set (it's optional on create)

**Minimal Python (Flask) receiver:**

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/webhooks/esign", methods=["POST"])
def esign_webhook():
    data = request.get_json()
    document_id = data["document_id"]
    event = data["event"]

    if event == "signer.signed":
        # Update your database
        update_contract_status(
            ref=data.get("reference_id"),
            status="signed",
            pdf_url=data["signed_pdf_url"],
        )

    return jsonify({"received": True}), 200
```

**Node.js (Express) receiver:**

```javascript
app.post("/webhooks/esign", express.json(), (req, res) => {
  const { event, document_id, reference_id, signed_pdf_url } = req.body;

  if (event === "signer.signed") {
    db.contracts.update(
      { internalId: reference_id },
      { status: "signed", pdfUrl: signed_pdf_url }
    );
  }

  res.json({ received: true });
});
```

### Testing webhooks locally

Use [ngrok](https://ngrok.com) or [webhook.site](https://webhook.site) to expose a local endpoint during development:

```bash
ngrok http 3000
# Copy the https://xxx.ngrok.io URL and use it as callback_url
```

---

## Data Models

### Document

| Field | Type | Description |
|---|---|---|
| `id` | string | Document ID (e.g., `esign_2026_0042`) |
| `namespace` | string | Namespace this document belongs to |
| `status` | string | `student_signing`, `completed`, `declined` |
| `signer_name` | string | Full name of the signer |
| `signer_email` | string | Email of the signer |
| `reference_id` | string | Your internal reference (optional) |
| `template` | string | Template used, or `external` for file_url uploads |
| `total_pages` | integer | Number of pages in the document |
| `signing_url` | string | Signing page URL sent to the signer |
| `original_pdf_path` | string | Internal path to the unsigned PDF |
| `signed_pdf_path` | string | Internal path to the signed PDF (present when completed) |
| `language` | string | Signing page language |
| `expiration_days` | integer | Days before link expires |
| `callback_url` | string | Webhook endpoint |
| `city` | string | City entered by signer (present when completed) |
| `created_at` | ISO 8601 | Creation timestamp |
| `signed_at` | ISO 8601 | Signature timestamp |
| `completed_at` | ISO 8601 | Completion timestamp |

### Signer

| Field | Type | Description |
|---|---|---|
| `role` | string | e.g., `student`, `guardian`, `witness` |
| `name` | string | Signer's full name |
| `email` | string | Signer's email address |
| `status` | string | `pending`, `signed`, `declined` |
| `signing_order` | integer | Order in multi-signer workflows |
| `signed_at` | ISO 8601 | When they signed (null if pending) |

### Audit Event

| Field | Type | Description |
|---|---|---|
| `action` | string | Event type (see audit event types above) |
| `detail` | object | Contextual data for the event |
| `ip` | string | IP address (null for system events) |
| `at` | ISO 8601 | Timestamp |

---

## Error Handling

All error responses follow this format:

```json
{
  "error": "Human-readable error message"
}
```

For quota errors, an additional field is included:

```json
{
  "error": "Envelope quota exceeded",
  "quota_exceeded": true
}
```

**HTTP status codes:**

| Code | Meaning |
|---|---|
| 200 | OK |
| 201 | Created (document successfully created) |
| 400 | Bad request — invalid parameters |
| 401 | Unauthorized — missing or invalid API key |
| 402 | Payment required — quota exceeded |
| 404 | Not found — document or resource does not exist |
| 409 | Conflict — document already in terminal state |
| 500 | Internal server error |

---

## Quota and Rate Limits

Every namespace has a monthly envelope quota determined by your subscription tier:

| Tier | Monthly envelopes | Overage |
|---|---|---|
| Free | 5 | Hard block |
| Starter | 30 | €0.50 / extra envelope |
| Pro | 150 | €0.35 / extra envelope |
| Business | Unlimited | — |
| Enterprise | Unlimited | — |

When you exceed your quota (Free and Starter with `hard_block`), `POST /esign/create` returns HTTP 402:

```json
{
  "error": "Envelope quota exceeded",
  "quota_exceeded": true
}
```

Starter and above tiers allow overage at the per-envelope rate shown above.

**View current usage:** Dashboard → Billing, or via `GET /subscriptions/current` (authenticated session required).

> **Roadmap 🔜** — `X-RateLimit-*` response headers and per-minute rate limiting are planned.

---

## Security and Compliance

### Data storage

- PDFs stored on server filesystem, isolated by namespace directory
- Database: SQLite with WAL mode
- Signing tokens: UUID v4, single-use

### Transport

- All endpoints served over HTTPS (TLS)
- Signing pages enforce HTTPS to protect OTP and signature data

### Audit trail

Every document has an immutable audit log recording:
- Document creation (signer name, email, page count)
- Signing event (city, pages initialled, IP address)
- Decline event (reason, IP address)
- Webhook delivery attempts (status codes)

The audit log is available at `GET /esign/{id}/audit` and embedded in the signed PDF metadata.

### OTP authentication

Before a signer can access the signing page, they must verify their email address via a one-time code sent to `signer_email`. This satisfies the eIDAS Article 26 requirement for "capable of identifying the signatory."

### Digital signature

Completed documents are signed using **pyHanko** (PAdES standard). The embedded signature covers the entire PDF content. Any modification to the PDF after signing invalidates the signature, detectable in any PDF reader.

---

## Development Guide

### Getting started checklist

- [ ] Sign up at [app.clawshow.ai](https://app.clawshow.ai)
- [ ] Create an API key in **Settings → API Keys**
- [ ] Note your namespace in **Settings → Account**
- [ ] Run the Quick Start cURL above — verify you receive a `document_id`
- [ ] Check your email (the `signer_email` you used) for the signing invitation
- [ ] Click through the signing flow
- [ ] Call `GET /esign/{document_id}/status` — verify `status: "completed"`
- [ ] Download `GET /esign/{document_id}/signed.pdf`

### Webhook development

For local development, use one of these tools to receive webhooks:

**ngrok** (recommended — keeps the same URL across sessions with a paid plan):

```bash
ngrok http 3000
# Use the HTTPS URL as callback_url
```

**webhook.site** (zero setup, inspect payloads in browser):

1. Go to [webhook.site](https://webhook.site)
2. Copy your unique URL
3. Use it as `callback_url` in your create request
4. Trigger a signing and watch the payload arrive in real time

### Error handling best practices

1. **Always store `document_id`** immediately after creation — it's your key to all status and PDF endpoints
2. **Use `reference_id`** to correlate ClawShow documents with your internal records
3. **Handle 402 before sending** — check dashboard quota before peak usage periods
4. **Implement idempotency** — use `document_id + event` as dedup key for webhook processing
5. **Poll as fallback** — if a webhook is not received within your expected window, poll `GET /esign/{id}/status`

---

## Roadmap

The following features are planned but not yet available:

| Feature | Description |
|---|---|
| 🔜 Multi-signer sequential flow | Require signatures in a defined order before document is complete |
| 🔜 Extended webhook retries | 5-attempt retry schedule with exponential backoff |
| 🔜 HMAC webhook signatures | `X-ClawShow-Signature` header for request verification |
| 🔜 Webhook management API | `GET/POST/DELETE /webhooks` endpoints |
| 🔜 `DELETE /esign/{id}` | Void / retract unsigned documents |
| 🔜 `POST /esign/{id}/resend` | Resend the signing email |
| 🔜 `POST /esign/{id}/extend` | Extend document expiration |
| 🔜 OpenAPI 3.0 spec | Machine-readable spec at `/openapi.json` |
| 🔜 Python/Node.js/PHP SDK | Official client libraries |
| 🔜 QES integration | Qualified Electronic Signature via QTSP |
| 🔜 Status page | `https://status.clawshow.ai` |

---

## FAQ

**How do I get an API key?**  
Log in to [app.clawshow.ai/settings](https://app.clawshow.ai/settings) and click **+ Create new key** under API Keys.

**What happens if I don't include `callback_url`?**  
The document is created and the email is sent, but ClawShow will not notify your server when the signer completes. You can still poll `GET /esign/{id}/status` to check completion.

**How long is the signing link valid?**  
Default 30 days, configurable via `expiration_days` on create.

**What is the maximum PDF size?**  
10 MB.

**Can I customize the signing page language?**  
Yes — set `language: "fr"` (French) or `language: "en"` (English) on create.

**Can multiple people sign the same document?**  
Single signer is fully supported. Sequential multi-signer (e.g., student + guardian) is on the roadmap.

**How do I confirm the email was sent?**  
Query `GET /esign/{id}/audit` and look for the email delivery event, or subscribe to the `signer.signed` webhook (email delivery is a precondition to signing).

**What if the webhook fails to deliver?**  
ClawShow retries 3 times with exponential backoff (1s, 2s gaps). After 3 failures, the event is logged in the audit trail. Use `GET /esign/{id}/status` as a fallback.

**Are signed documents stored permanently?**  
Signed documents (status `completed`) are retained as long as your account is active. Unsigned documents expired beyond `expiration_days` are subject to cleanup after 90 days.

**What compliance level is this?**  
AES — Advanced Electronic Signature, eIDAS Article 26. Suitable for most commercial contracts in the EU. QES (Qualified) is on the roadmap for use cases requiring higher legal standing.

---

## Support

| Channel | Address |
|---|---|
| Technical support | support@clawshow.ai |
| Dashboard | [app.clawshow.ai](https://app.clawshow.ai) |
| Status page | 🔜 status.clawshow.ai |

---

## Changelog

### v1.10.2 — 2026-04-18
- Namespace-level webhook configuration (Dashboard Settings + PATCH /webhooks/config)
- Per-document callback_url now falls back to namespace webhook if not set

### v1.10.1 — 2026-04-18
- Pen cursor restored in signature pad
- Undo button added to draw tab in setup modal

### v1.0.0 — 2026-04-09
- Initial public API release
- AES compliance (eIDAS Article 26)
- Template: `enrollment_contract`
- OTP email verification
- Webhook callback on signing
- Audit trail endpoint
- Signed PDF download
