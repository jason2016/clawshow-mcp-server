"""
Tool: send_esign_request
-------------------------
Self-hosted electronic signature engine. Zero cost per signature.
No third-party e-sign service required.

Flow:
  1. Generate document_id (esign_YYYY_NNNN)
  2. Read HTML template, replace {{variable}} placeholders
  3. Render HTML -> PDF via weasyprint
  4. Save rendered HTML + PDF to /opt/clawshow-data/esign/{namespace}/
  5. Create DB record with signing_url = https://mcp.clawshow.ai/esign/{doc_id}
  6. Email signing link to signer (optional)
  7. Return signing_url + document_id

Signing page: GET /esign/{doc_id}  -- served by server.py
Signature submission: POST /esign/{doc_id}/sign

Env required: SMTP_HOST, SMTP_USER, SMTP_PASS (for email)
"""

from __future__ import annotations

import base64
import os
import json
import re
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import db

ESIGN_DATA_DIR = Path("/opt/clawshow-data/esign")
TEMPLATES_DIR = ESIGN_DATA_DIR / "templates"
BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai")

# ---------------------------------------------------------------------------
# Fallback contract templates (used when no HTML file found)
# ---------------------------------------------------------------------------

_FALLBACK_TEMPLATES: dict = {
    "rental_agreement": {
        "title": "CONTRAT DE LOCATION",
        "body": lambda f, sn: (
            f"<h3>Bailleur : {f.get('landlord_name', '')}</h3>"
            f"<h3>Locataire : {f.get('tenant_name', sn)}</h3>"
            f"<p><strong>Bien loue :</strong> {f.get('property_address', '')}</p>"
            f"<p><strong>Loyer mensuel :</strong> {f.get('rent_amount', '')} EUR</p>"
            f"<p><strong>Periode :</strong> du {f.get('start_date', '')} au {f.get('end_date', '')}</p>"
            f"<p>{f.get('terms', '')}</p>"
        ),
    },
    "service_agreement": {
        "title": "CONTRAT DE PRESTATION",
        "body": lambda f, sn: (
            f"<h3>Prestataire : {f.get('provider_name', '')}</h3>"
            f"<h3>Client : {f.get('client_name', sn)}</h3>"
            f"<p><strong>Prestation :</strong> {f.get('service_description', '')}</p>"
            f"<p><strong>Honoraires :</strong> {f.get('fee', '')} EUR</p>"
            f"<p><strong>Date de debut :</strong> {f.get('start_date', '')}</p>"
            f"<p>{f.get('terms', '')}</p>"
        ),
    },
    "custom": {
        "title": "DOCUMENT",
        "body": lambda f, sn: "".join(
            f"<p><strong>{k.replace('_', ' ').title()} :</strong> {v}</p>"
            for k, v in f.items() if k != "title"
        ),
    },
}

# ---------------------------------------------------------------------------
# Document ID generation
# ---------------------------------------------------------------------------

def _next_doc_id(namespace: str) -> str:
    """Generate esign_YYYY_NNNN style ID."""
    year = datetime.now(timezone.utc).year
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM esign_documents WHERE namespace = ? AND id LIKE ?",
            (namespace, f"esign_{year}_%"),
        ).fetchone()
        n = (row["cnt"] or 0) + 1
    return f"esign_{year}_{n:04d}"


# ---------------------------------------------------------------------------
# HTML template rendering
# ---------------------------------------------------------------------------

def _render_template(template: str, doc_id: str, signer_name: str, fields: dict) -> str:
    """Load HTML template and substitute {{variable}} placeholders. Returns rendered HTML string."""
    html_path = TEMPLATES_DIR / f"{template}.html"

    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
        all_vars = {"document_id": doc_id, **fields}
        for key, value in all_vars.items():
            html = html.replace("{{" + key + "}}", str(value))
        # Remove any remaining unreplaced placeholders
        html = re.sub(r"\{\{[^}]+\}\}", "", html)
        return html

    # Fallback: generate simple HTML
    tmpl = _FALLBACK_TEMPLATES.get(template, _FALLBACK_TEMPLATES["custom"])
    title = fields.get("title", tmpl["title"])
    body_html = tmpl["body"](fields, signer_name)
    return f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<style>
body {{ font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 40px; color: #333; }}
h1 {{ text-align: center; text-transform: uppercase; }}
p {{ line-height: 1.6; }}
.sig-block {{ margin-top: 60px; }}
</style></head>
<body>
<h1>{title}</h1>
{body_html}
<div class="sig-block">
  <p>Fait a : <span id="city-placeholder">___________________</span></p>
  <p>Le : <span id="date-placeholder">___________________</span></p>
  <p>Mention manuscrite lu et approuve :</p>
  <div id="lu-approuve-placeholder" style="height:50px;border-bottom:1px solid #000;width:260px;"></div>
  <p style="margin-top:16px;">Signature de {signer_name} :</p>
  <div id="signature-placeholder" style="height:80px;border-bottom:1px solid #000;width:260px;"></div>
</div>
<div style="margin-top:30px;font-size:9pt;color:#999;text-align:center;border-top:1px solid #eee;padding-top:8px;">
  Document ID : {doc_id} | Signe electroniquement via ClawShow eSign | mcp.clawshow.ai
</div>
</body></html>"""


def _html_to_pdf(html: str, out_path: str) -> None:
    """Render HTML string to PDF using weasyprint."""
    from weasyprint import HTML
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(out_path)


def _generate_pdf(doc_id: str, namespace: str, template: str, signer_name: str, fields: dict) -> tuple:
    """
    Render template and generate PDF.
    Returns (html_path, pdf_path).
    """
    out_dir = ESIGN_DATA_DIR / namespace
    out_dir.mkdir(parents=True, exist_ok=True)

    rendered_html = _render_template(template, doc_id, signer_name, fields)

    html_path = str(out_dir / f"{doc_id}.html")
    pdf_path = str(out_dir / f"{doc_id}.pdf")

    Path(html_path).write_text(rendered_html, encoding="utf-8")
    _html_to_pdf(rendered_html, pdf_path)

    return html_path, pdf_path


# ---------------------------------------------------------------------------
# Signature embedding (re-render signed HTML via weasyprint)
# ---------------------------------------------------------------------------

def _embed_signature_in_pdf(
    rendered_html_path: str,
    signed_pdf: str,
    signature_png_bytes: bytes,
    signer_name: str,
    signed_at: str,
    signer_ip: str,
    lu_approuve_png_bytes: bytes = b"",
    city: str = "Paris",
) -> None:
    """
    Replace placeholder divs in the rendered HTML with actual signature images,
    fill city and date, then re-render to produce the signed PDF.
    """
    html = Path(rendered_html_path).read_text(encoding="utf-8")

    try:
        dt = datetime.fromisoformat(signed_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%d/%m/%Y a %H:%M UTC")
    except Exception:
        date_str = signed_at[:10]

    sig_b64 = base64.b64encode(signature_png_bytes).decode()

    # Replace city placeholder
    html = re.sub(
        r'<span id="city-placeholder">[^<]*</span>',
        f'<span id="city-placeholder">{city}</span>',
        html,
    )
    # Replace date placeholder
    html = re.sub(
        r'<span id="date-placeholder">[^<]*</span>',
        f'<span id="date-placeholder">{date_str}</span>',
        html,
    )
    # Replace lu-approuve placeholder div with image (if available)
    if lu_approuve_png_bytes:
        lu_b64 = base64.b64encode(lu_approuve_png_bytes).decode()
        html = re.sub(
            r'<div id="lu-approuve-placeholder"[^>]*>.*?</div>',
            (
                '<div id="lu-approuve-placeholder" style="width:260px;height:50px;">'
                f'<img src="data:image/png;base64,{lu_b64}" style="max-width:260px;max-height:50px;"/></div>'
            ),
            html,
            flags=re.DOTALL,
        )
    # Replace signature placeholder div with image
    html = re.sub(
        r'<div id="signature-placeholder"[^>]*>.*?</div>',
        (
            '<div id="signature-placeholder" style="width:260px;height:80px;">'
            f'<img src="data:image/png;base64,{sig_b64}" style="max-width:260px;max-height:80px;"/></div>'
        ),
        html,
        flags=re.DOTALL,
    )

    # Inject metadata footer
    meta = (
        '<div style="margin-top:16px;padding:8px;background:#f9f9f9;border:1px solid #ddd;'
        'font-size:9pt;color:#555;font-family:Arial,sans-serif;">'
        f'Signe electroniquement par : <strong>{signer_name}</strong> | '
        f'Date : {date_str} | IP : {signer_ip} | Ville : {city}'
        '</div>'
    )
    html = html.replace("</body>", meta + "\n</body>")

    _html_to_pdf(html, signed_pdf)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _send_signing_email(signer_name: str, signer_email: str, signing_url: str,
                         doc_id: str, language: str) -> None:
    try:
        host = os.getenv("SMTP_HOST", "")
        port = int(os.getenv("SMTP_PORT", "465"))
        user = os.getenv("SMTP_USER", "")
        pwd = os.getenv("SMTP_PASS", "")
        if not host or not user:
            return

        labels = {
            "fr": ("Document a signer", "Bonjour", "Vous avez recu un document a signer.", "Signer le document"),
            "en": ("Document to sign", "Hello", "You have received a document to sign.", "Sign document"),
            "zh": ("请签署文件", "您好", "您收到了一份需要签署的文件。", "签署文件"),
        }.get(language, ("Document to sign", "Hello", "You have received a document to sign.", "Sign document"))

        subject, greeting, body_text, cta = labels
        html = f"""
        <div style="max-width:520px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
          <div style="background:#1a1a2e;padding:24px;text-align:center;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">ClawShow eSign</h1>
          </div>
          <div style="background:white;padding:28px;border:1px solid #eee;border-top:none">
            <p>{greeting} {signer_name},</p>
            <p>{body_text}</p>
            <div style="text-align:center;margin:28px 0">
              <a href="{signing_url}"
                 style="background:#1a1a2e;color:white;padding:14px 32px;text-decoration:none;border-radius:8px;font-size:16px;font-weight:600">
                {cta}
              </a>
            </div>
            <p style="font-size:12px;color:#999">Document ID: {doc_id}</p>
          </div>
        </div>
        """
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"ClawShow eSign <{user}>"
        msg["To"] = signer_email
        msg.attach(MIMEText(html, "html", "utf-8"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
    except Exception:
        pass  # Email failure must not break the tool


# ---------------------------------------------------------------------------
# MCP Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def send_esign_request(
        template: str,
        signer_name: str,
        signer_email: str,
        fields: dict,
        namespace: str,
        reference_id: str = "",
        callback_url: str = "",
        send_email: bool = True,
        language: str = "fr",
    ) -> str:
        """
        Send an electronic signature request for any document: enrollment contracts,
        rental agreements, service agreements, NDAs, or custom documents. Generates a
        PDF from template with pre-filled data, creates a mobile-friendly signing page,
        and emails the signing link to the signer. After signing, the signed PDF is
        automatically stored and a webhook callback updates the source system.
        Input: template type, signer name, signer email, document fields, namespace.
        Output: signing page URL, document ID, status.
        Zero cost per signature -- no third-party e-signature service required.
        Fully self-hosted. Compliant with eIDAS and ESIGN Act.

        Args:
            template:     Document template -- "enrollment_contract", "rental_agreement",
                          "service_agreement", or "custom"
            signer_name:  Full name of the person who will sign
            signer_email: Email address of the signer (signing link sent here)
            fields:       Template variables dict, e.g. {school_name: "ILCI", tuition: "8500"}
            namespace:    Client namespace (e.g. "ilci", "florent")
            reference_id: Optional external ID, e.g. FocusingPro inscription_id
            callback_url: Optional webhook URL called when signing is complete
            send_email:   Whether to email the signing link (default True)
            language:     Signing page language -- "fr", "en", or "zh" (default "fr")

        Returns:
            JSON with signing_url, document_id, pdf_preview_url, status, signer_email, created_at.
        """
        record_call("send_esign_request", {"namespace": namespace, "template": template})

        doc_id = _next_doc_id(namespace)
        signing_url = f"{BASE_URL}/esign/{doc_id}"
        pdf_preview_url = f"{BASE_URL}/esign/{doc_id}/preview.pdf"

        try:
            html_path, pdf_path = _generate_pdf(doc_id, namespace, template, signer_name, fields)
        except Exception as e:
            return json.dumps({"success": False, "error": f"PDF generation failed: {e}"}, ensure_ascii=False)

        db.create_esign_document(
            doc_id=doc_id,
            namespace=namespace,
            template=template,
            signer_name=signer_name,
            signer_email=signer_email,
            fields=fields,
            signing_url=signing_url,
            original_pdf_path=pdf_path,
            rendered_html_path=html_path,
            reference_id=reference_id,
            callback_url=callback_url,
            language=language,
            send_email=send_email,
        )

        if send_email and signer_email:
            threading.Thread(
                target=_send_signing_email,
                args=(signer_name, signer_email, signing_url, doc_id, language),
                daemon=True,
            ).start()

        created_at = datetime.now(timezone.utc).isoformat()
        return json.dumps({
            "success": True,
            "document_id": doc_id,
            "signing_url": signing_url,
            "pdf_preview_url": pdf_preview_url,
            "status": "pending",
            "signer_email": signer_email,
            "created_at": created_at,
            "email_sent": send_email and bool(signer_email),
        }, ensure_ascii=False)
