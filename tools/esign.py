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
    """Generate esign_YYYY_NNNN style ID, collision-safe."""
    year = datetime.now(timezone.utc).year
    prefix = f"esign_{year}_"
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM esign_documents WHERE id LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        existing_nums = set()
        for r in rows:
            try:
                existing_nums.add(int(r["id"][len(prefix):]))
            except ValueError:
                pass
        n = max(existing_nums, default=0) + 1
        candidate = f"{prefix}{n:04d}"
        existing_ids = {r["id"] for r in rows}
        while candidate in existing_ids:
            n += 1
            candidate = f"{prefix}{n:04d}"
    return candidate


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
  <p>Fait à : <span id="city-placeholder">___________________</span></p>
  <p>Le : <span id="date-placeholder">___________________</span></p>
  <p>Mention manuscrite lu et approuvé :</p>
  <div id="lu-approuve-placeholder" style="height:50px;border-bottom:1px solid #000;width:260px;"></div>
  <p style="margin-top:16px;">Signature de {signer_name} :</p>
  <div id="signature-placeholder" style="height:80px;border-bottom:1px solid #000;width:260px;"></div>
</div>
<div style="margin-top:30px;font-size:9pt;color:#999;text-align:center;border-top:1px solid #eee;padding-top:8px;">
  Document ID : {doc_id} | Signé électroniquement via ClawShow eSign | mcp.clawshow.ai
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

    try:
        import fitz as _fitz
        doc = _fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()
    except Exception:
        total_pages = 1

    return html_path, pdf_path, total_pages


def _generate_page_images(pdf_path: str, pages_dir: str) -> int:
    """Render each page of a PDF as a PNG. Returns total page count."""
    import fitz as _fitz
    import os as _os

    _os.makedirs(pages_dir, exist_ok=True)
    doc = _fitz.open(pdf_path)
    total = len(doc)
    for i, page in enumerate(doc, start=1):
        mat = _fitz.Matrix(2.0, 2.0)  # 2x zoom = ~144 dpi
        pix = page.get_pixmap(matrix=mat, alpha=False)
        pix.save(_os.path.join(pages_dir, f"page_{i}.png"))
    doc.close()
    return total


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
        from zoneinfo import ZoneInfo as _ZI
        _paris = dt.astimezone(_ZI("Europe/Paris"))
        _utc = dt.astimezone(__import__("datetime").timezone.utc)
        _tz = _paris.strftime("%Z")
        date_str = (
            f"{_paris.strftime('%d/%m/%Y')} à "
            f"{_paris.strftime('%H:%M')} {_tz} "
            f"({_utc.strftime('%H:%M')} UTC)"
        )
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
        f'Signé électroniquement par : <strong>{signer_name}</strong> | '
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
        if not signer_email:
            return
        labels = {
            "fr": ("Document à signer", "Bonjour", "Vous avez reçu un document à signer.", "Signer le document"),
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
        _send_html_email(signer_email, subject, html)
    except Exception:
        pass  # Email failure must not break the tool


# ---------------------------------------------------------------------------
# MCP Tool registration
# ---------------------------------------------------------------------------

def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def send_esign_request(
        namespace: str,
        template: str = "",
        signer_name: str = "",
        signer_email: str = "",
        fields: dict = None,
        reference_id: str = "",
        callback_url: str = "",
        send_email: bool = True,
        language: str = "fr",
        file_url: str = "",
        signers: str = "",
        signature_fields: str = "",
        expiration_days: int = 30,
        reminder_frequency: str = "EVERY_THIRD_DAY",
    ) -> str:
        """
        Send an electronic signature request. Supports two modes:
        1. ClawShow native: generate PDF from template with pre-filled fields.
        2. FocusingPro / external: pass a file_url (S3 PDF) + signers array.
        After signing, the signed PDF is stored and webhook callback updates the source system.
        AES-level (eIDAS Art.26): OTP email verification + SHA-256 digital signature.

        Args:
            namespace:          Client namespace (e.g. "ilci", "florent")
            template:           Document template -- "enrollment_contract", "rental_agreement",
                                "service_agreement", or "custom". Required if no file_url.
            signer_name:        Full name of the primary signer. Required if no signers array.
            signer_email:       Email of the primary signer. Required if no signers array.
            fields:             Template variables dict, e.g. {"school_name": "ILCI", "tuition": "8500"}.
                                Use for native template mode.
            reference_id:       Optional external ID, e.g. FocusingPro inscription_id.
            callback_url:       Optional webhook URL called when signing is complete.
            send_email:         Whether to email the signing link (default True).
            language:           Signing page language -- "fr", "en", or "zh" (default "fr").
            file_url:           URL of an existing PDF to sign (e.g. S3 pre-signed URL).
                                When provided, template/fields are ignored.
            signers:            JSON array string of signers for multi-party workflows.
                                Each item: {"role":"student","name":"...","email":"...","order":1}.
                                Order 1 signs first. If omitted, uses signer_name/signer_email.
            signature_fields:   JSON object string defining custom signature positions per role.
                                E.g. {"paraphe":{"x":390,"y":22,"w":160,"h":48}}.
            expiration_days:    Days until signing link expires (default 30).
            reminder_frequency: Reminder schedule -- "EVERY_THIRD_DAY", "WEEKLY", or "NONE".

        Returns:
            JSON with signing_url, document_id, pdf_preview_url, signers list, status.
        """
        import json as _json
        import requests as _req

        record_call("send_esign_request", {"namespace": namespace, "template": template or "external"})

        payload = {
            "namespace": namespace,
            "reference_id": reference_id,
            "callback_url": callback_url,
            "language": language,
            "send_email": send_email,
            "expiration_days": expiration_days,
            "reminder_frequency": reminder_frequency,
        }

        # File URL mode (FocusingPro / external PDF)
        if file_url:
            payload["file_url"] = file_url
            # Parse signers JSON string if provided
            if signers:
                try:
                    payload["signers"] = _json.loads(signers)
                except Exception:
                    return _json.dumps({"success": False, "error": "signers must be valid JSON array string"})
            else:
                if not signer_name or not signer_email:
                    return _json.dumps({"success": False, "error": "signer_name and signer_email are required when signers array is not provided"})
                payload["signers"] = [{"role": "student", "name": signer_name, "email": signer_email, "order": 1}]
            if signature_fields:
                try:
                    payload["signature_fields"] = _json.loads(signature_fields)
                except Exception:
                    pass  # ignore malformed; server uses defaults
        else:
            # Native template mode
            payload["template"] = template or "enrollment_contract"
            payload["fields"] = fields or {}
            if signers:
                try:
                    payload["signers"] = _json.loads(signers)
                except Exception:
                    return _json.dumps({"success": False, "error": "signers must be valid JSON array string"})
            else:
                payload["signer_name"] = signer_name
                payload["signer_email"] = signer_email

        try:
            resp = _req.post(f"{BASE_URL}/esign/create", json=payload, timeout=120)
            resp.raise_for_status()
            return resp.text
        except _req.exceptions.RequestException as exc:
            return _json.dumps({"success": False, "error": str(exc)})

# ---------------------------------------------------------------------------
# V2: Multi-page overlay + school counter-sign

_DEFAULT_SIG_POSITIONS = {
    "paraphe":   {"x": 390, "y": 22, "w": 160, "h": 48},
    "final_lu":  {"x": 90,  "y": 130, "w": 220, "h": 38},
    "final_sig": {"x": 90,  "y": 70,  "w": 220, "h": 70},
    "school_sig":{"x": 330, "y": 30,  "w": 200, "h": 70},
}


def _overlay_signatures_pdf(
    original_pdf: str,
    signed_pdf: str,
    paraphes: dict,
    final_sig: dict,
    sig_positions: dict = None,
    school_sig: dict = None,
) -> None:
    """
    Overlay paraphes on each page + full signature block on last page.
    paraphes: {page_num: bytes} (1-indexed)
    final_sig: {sig_bytes, lu_bytes, city, signer_name, signed_at, signer_ip}
    school_sig: optional {sig_bytes, city, signer_name, signed_at} for counter-sign
    """
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
    from PyPDF2 import PdfReader, PdfWriter
    import io as _io

    pos = sig_positions or _DEFAULT_SIG_POSITIONS
    reader = PdfReader(original_pdf)
    writer = PdfWriter()
    total = len(reader.pages)

    for page_num in range(1, total + 1):
        page = reader.pages[page_num - 1]
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        scale = w / 595.0  # A4 reference width
        is_last = (page_num == total)

        has_paraphe = page_num in paraphes and paraphes[page_num]
        has_final = is_last and final_sig and final_sig.get("sig_bytes")

        if has_paraphe or has_final:
            packet = _io.BytesIO()
            c = rl_canvas.Canvas(packet, pagesize=(w, h))

            if has_paraphe:
                pp = pos["paraphe"]
                c.drawImage(
                    ImageReader(_io.BytesIO(paraphes[page_num])),
                    pp["x"] * scale, pp["y"],
                    width=pp["w"] * scale, height=pp["h"],
                    mask="auto", preserveAspectRatio=True,
                )

            if has_final:
                sf = final_sig
                # "lu et approuve" image
                if sf.get("lu_bytes"):
                    lp = pos["final_lu"]
                    c.drawImage(
                        ImageReader(_io.BytesIO(sf["lu_bytes"])),
                        lp["x"] * scale, lp["y"],
                        width=lp["w"] * scale, height=lp["h"],
                        mask="auto", preserveAspectRatio=True,
                    )
                # Final signature image
                fp = pos["final_sig"]
                c.drawImage(
                    ImageReader(_io.BytesIO(sf["sig_bytes"])),
                    fp["x"] * scale, fp["y"],
                    width=fp["w"] * scale, height=fp["h"],
                    mask="auto", preserveAspectRatio=True,
                )
                # Signer metadata footer
                c.setFont("Helvetica", 7)
                c.setFillColorRGB(0.5, 0.5, 0.5)
                c.drawString(fp["x"] * scale, 30,
                             (sf.get("signer_name", "")[:24] + " | " +
                              sf.get("city", "") + " | " +
                              sf.get("signed_at", "")[:10]))
                c.drawString(fp["x"] * scale, 20,
                             "IP: " + sf.get("signer_ip", "")[:20])

                # School counter-signature
                if school_sig and school_sig.get("sig_bytes"):
                    sp2 = pos["school_sig"]
                    c.drawImage(
                        ImageReader(_io.BytesIO(school_sig["sig_bytes"])),
                        sp2["x"] * scale, sp2["y"],
                        width=sp2["w"] * scale, height=sp2["h"],
                        mask="auto", preserveAspectRatio=True,
                    )
                    c.setFont("Helvetica", 7)
                    c.setFillColorRGB(0.5, 0.5, 0.5)
                    c.drawString(sp2["x"] * scale, 18,
                                 "Admin: " + school_sig.get("signer_name", "")[:20] +
                                 " | " + school_sig.get("signed_at", "")[:10])

                # AES compliance footer
                doc_id = sf.get("doc_id", "")
                ts = sf.get("signed_at", "")[:19].replace("T", " ")
                mcp_base = os.getenv("MCP_BASE_URL", "https://mcp.clawshow.ai")
                c.setFont("Helvetica", 6)
                c.setFillColorRGB(0.4, 0.4, 0.4)
                footer1 = (
                    "Signe via ClawShow eSign — Signature Electronique Avancee (AES) conforme eIDAS | "
                    f"Document ID: {doc_id} | Horodatage: {ts}"
                )
                footer2 = f"Verification: {mcp_base}/esign/{doc_id}/verify"
                c.drawString(30, 8, footer1[:120])
                c.drawString(30, 2, footer2)

            c.save()
            packet.seek(0)
            overlay = PdfReader(packet).pages[0]
            page.merge_page(overlay)

        writer.add_page(page)

    os.makedirs(os.path.dirname(signed_pdf), exist_ok=True)
    with open(signed_pdf, "wb") as f:
        writer.write(f)


# ---------------------------------------------------------------------------
# Email helpers

def _send_school_notification_email(
    student_name: str, school_email: str, school_name: str,
    school_signing_url: str, doc_id: str,
) -> None:
    """Email school admin when student has signed asking for counter-signature."""
    try:
        if not school_email:
            return
        subject = f"[ClawShow eSign] Convention signee par {student_name} — a contresigner"
        html = f"""<div style="max-width:520px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
          <div style="background:#1a1a2e;padding:24px;text-align:center;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">ClawShow eSign</h1>
          </div>
          <div style="background:white;padding:28px;border:1px solid #eee;border-top:none">
            <p>Bonjour {school_name or "Administration"},</p>
            <p>L'etudiant(e) <strong>{student_name}</strong> a signe sa convention.</p>
            <p>Veuillez contresigner en cliquant sur le lien suivant :</p>
            <div style="text-align:center;margin:28px 0">
              <a href="{school_signing_url}"
                 style="background:#1a1a2e;color:white;padding:14px 32px;text-decoration:none;border-radius:8px;font-size:16px;font-weight:600">
                Contresigner le document
              </a>
            </div>
            <p style="font-size:12px;color:#999">Document ID: {doc_id}</p>
          </div>
        </div>"""
        _send_html_email(school_email, subject, html)
    except Exception:
        pass


def _send_completion_email(
    recipient_name: str, recipient_email: str, doc_id: str, signed_pdf_url: str,
) -> None:
    """Email both parties when document is fully signed."""
    try:
        if not recipient_email:
            return
        subject = "[ClawShow eSign] Document signé par toutes les parties"
        html = f"""<div style="max-width:520px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
          <div style="background:#1a1a2e;padding:24px;text-align:center;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">ClawShow eSign &#x2705;</h1>
          </div>
          <div style="background:white;padding:28px;border:1px solid #eee;border-top:none">
            <p>Bonjour {recipient_name},</p>
            <p>Le document a été signé par toutes les parties.</p>
            <div style="text-align:center;margin:28px 0">
              <a href="{signed_pdf_url}"
                 style="background:#28a745;color:white;padding:14px 32px;text-decoration:none;border-radius:8px;font-size:16px;font-weight:600">
                Télécharger le document signé
              </a>
            </div>
            <p style="font-size:12px;color:#999">Document ID: {doc_id}</p>
          </div>
        </div>"""
        _send_html_email(recipient_email, subject, html)
    except Exception:
        pass


def _send_expiration_email(recipient_name: str, recipient_email: str,
                            doc_id: str, lang: str = "fr") -> None:
    """Email signers when a document has expired."""
    try:
        if not recipient_email:
            return
        subject = "[ClawShow eSign] Document expiré" if lang == "fr" else "[ClawShow eSign] Document expiréd"
        body = (
            f"<p>Bonjour {recipient_name},</p><p>Le document (ID: {doc_id}) a expiré sans avoir été signé par toutes les parties.</p>"
            if lang == "fr" else
            f"<p>Hello {recipient_name},</p><p>Document (ID: {doc_id}) has expired without being signed by all parties.</p>"
        )
        html = f"""<div style="max-width:520px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
          <div style="background:#1a1a2e;padding:24px;text-align:center;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">ClawShow eSign</h1>
          </div>
          <div style="background:white;padding:28px;border:1px solid #eee;border-top:none">{body}</div>
        </div>"""
        _send_html_email(recipient_email, subject, html)
    except Exception:
        pass


def _send_otp_email(signer_name: str, signer_email: str, code: str) -> None:
    """Send OTP verification code email."""
    try:
        if not signer_email:
            return
        subject = f"[ClawShow eSign] Votre code de verification : {code}"
        html = f"""<div style="max-width:520px;margin:0 auto;font-family:Arial,sans-serif;color:#333">
          <div style="background:#1a1a2e;padding:24px;text-align:center;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">&#x1F512; ClawShow eSign</h1>
          </div>
          <div style="background:white;padding:28px;border:1px solid #eee;border-top:none">
            <p>Bonjour {signer_name},</p>
            <p>Votre code de verification pour signer le document est :</p>
            <div style="text-align:center;margin:28px 0;font-size:42px;font-weight:700;letter-spacing:12px;
                        font-family:monospace;color:#1a1a2e;background:#f5f5f5;padding:20px;border-radius:8px">
              {code}
            </div>
            <p style="color:#666">Ce code est valable <strong>10 minutes</strong>.</p>
            <p style="font-size:12px;color:#999">Si vous n&#x27;avez pas demande ce code, veuillez ignorer cet email.</p>
            <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
            <p style="font-size:11px;color:#aaa;text-align:center">ClawShow eSign — Signature Electronique Avancee (AES)</p>
          </div>
        </div>"""
        _send_html_email(signer_email, subject, html)
    except Exception:
        pass


def _send_html_email(to: str, subject: str, html: str) -> None:
    """Send HTML email via Resend API (ClawShow eSign <esign@clawshow.ai>)."""
    from adapters.esign.mailer import send_html
    send_html(to, subject, html)


# ---------------------------------------------------------------------------
# PDF digital signature (AES level — pyhanko + self-signed cert)

_CERT_KEY = "/opt/clawshow-data/certs/esign-key.pem"
_CERT_CRT = "/opt/clawshow-data/certs/esign-cert.pem"


def digitally_sign_pdf(input_path: str, output_path: str, document_id: str,
                        signers_info: list = None) -> str:
    """
    Apply an X.509 digital signature to the PDF using pyhanko.
    Returns output_path on success, input_path on failure (graceful degradation).
    """
    try:
        if not os.path.exists(_CERT_KEY) or not os.path.exists(_CERT_CRT):
            return input_path  # cert not generated yet, skip gracefully

        from pyhanko.sign import signers, fields
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.sign.fields import SigFieldSpec

        signer = signers.SimpleSigner.load(_CERT_KEY, _CERT_CRT)

        reason_parts = [f"Document signed via ClawShow eSign (ID: {document_id})"]
        if signers_info:
            names = ", ".join(s.get("signer_name", "") for s in signers_info if s.get("signer_name"))
            if names:
                reason_parts.append(f"Signataires: {names}")
        reason = " | ".join(reason_parts)

        with open(input_path, "rb") as f:
            writer = IncrementalPdfFileWriter(f)

            fields.append_signature_field(
                writer,
                sig_field_spec=SigFieldSpec(
                    sig_field_name="ClawShowESign",
                    on_page=0,
                    box=(30, 30, 250, 80),
                ),
            )

            meta = signers.PdfSignatureMetadata(
                field_name="ClawShowESign",
                reason=reason,
                name="ClawShow eSign Platform",
                location="Paris, France",
                contact_info="support@clawshow.ai",
            )

            with open(output_path, "wb") as out:
                signers.sign_pdf(writer, meta, signer=signer, output=out)

        return output_path
    except Exception as e:
        # Graceful degradation: digital signing optional, don't break the flow
        import shutil
        shutil.copy2(input_path, output_path)
        return output_path


def verify_signed_pdf(signed_pdf_path: str) -> dict:
    """
    Verify the digital signature on a signed PDF.
    Returns integrity dict.
    """
    try:
        if not os.path.exists(signed_pdf_path):
            return {"integrity": "unknown", "error": "File not found"}

        from pyhanko.sign.validation import validate_pdf_signature
        from pyhanko.pdf_utils.reader import PdfFileReader

        with open(signed_pdf_path, "rb") as f:
            reader = PdfFileReader(f)
            sigs = reader.embedded_signatures
            if not sigs:
                return {"integrity": "unsigned", "signed_by": None}

            sig = sigs[0]
            status = validate_pdf_signature(sig)
            return {
                "integrity": "valid" if status.intact else "tampered",
                "signed_by": status.signer_reported_dt and "ClawShow eSign Platform",
                "signed_at": str(status.signer_reported_dt) if status.signer_reported_dt else None,
                "intact": status.intact,
                "valid": status.valid,
            }
    except Exception as e:
        return {"integrity": "unknown", "error": str(e)}


# ---------------------------------------------------------------------------
# check_pending_reminders (cron)

def check_pending_reminders() -> None:
    """Called daily. Send reminder emails and process expired documents."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now = _dt.now(_tz.utc)
    pending = db.get_pending_esign_documents()

    for doc in pending:
        created = doc.get("created_at", "")
        try:
            created_dt = _dt.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            continue

        days_since = (now - created_dt).days
        expiry = int(doc.get("expiration_days") or 30)

        if days_since > expiry:
            db.update_document_status(doc["id"], "expired")
            db.log_esign_audit(doc["id"], "expired", {"days_since": days_since})
            try:
                for sg in db.get_signers_by_document(doc["id"]):
                    if sg.get("status") not in ("signed", "declined") and sg.get("signer_email"):
                        _send_expiration_email(sg.get("signer_name", ""), sg["signer_email"],
                                               doc["id"], doc.get("language", "fr"))
            except Exception:
                pass
            callback_url = doc.get("callback_url", "")
            if callback_url:
                import threading as _thr
                def _fire(cb=callback_url, did=doc["id"], ns=doc.get("namespace", "")):
                    import requests as _r
                    from datetime import datetime as _dt2, timezone as _tz2
                    ts = _dt2.now(_tz2.utc).isoformat()
                    try:
                        _r.post(cb, json={"event": "document.expired", "document_id": did,
                                          "namespace": ns, "status": "expired", "expired_at": ts},
                                timeout=10)
                    except Exception:
                        pass
                _thr.Thread(target=_fire, daemon=True).start()
            continue

        freq = (doc.get("reminder_frequency") or "EVERY_THIRD_DAY").upper()
        interval = 3 if "THIRD" in freq else 7 if "WEEK" in freq else 3
        last_reminder = doc.get("last_reminder_at", "")
        if last_reminder:
            try:
                last_dt = _dt.fromisoformat(last_reminder.replace("Z", "+00:00"))
                days_since_reminder = (now - last_dt).days
            except Exception:
                days_since_reminder = interval
        else:
            days_since_reminder = days_since

        if days_since_reminder >= interval:
            try:
                for sg in db.get_signers_by_document(doc["id"]):
                    if sg.get("status") == "pending" and sg.get("signer_email") and sg.get("token"):
                        mcp_base = os.getenv("MCP_BASE_URL", "https://mcp.clawshow.ai")
                        signing_url = f"{mcp_base}/esign/{doc['id']}?token={sg['token']}"
                        _send_signing_email(sg["signer_name"], sg["signer_email"],
                                            signing_url, doc["id"], doc.get("language", "fr"))
                db.update_esign_last_reminder(doc["id"])
                db.log_esign_audit(doc["id"], "reminder_sent", {"days_since": days_since})
            except Exception:
                pass
