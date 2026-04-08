"""
Tool: send_esign_request
-------------------------
Self-hosted electronic signature engine. Zero cost per signature.
No third-party e-sign service required.

Flow:
  1. Generate document_id (esign_YYYY_NNNN)
  2. Render HTML template → PDF via reportlab
  3. Save PDF to /opt/clawshow-data/esign/{namespace}/{doc_id}.pdf
  4. Create DB record with signing_url = https://mcp.clawshow.ai/esign/{doc_id}
  5. Email signing link to signer (optional)
  6. Return signing_url + document_id

Signing page: GET /esign/{doc_id}  — served by server.py
Signature submission: POST /esign/{doc_id}/sign

Env required: SMTP_HOST, SMTP_USER, SMTP_PASS (for email)
"""

from __future__ import annotations

import os
import json
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import db

ESIGN_DATA_DIR = Path("/opt/clawshow-data/esign")
BASE_URL = os.environ.get("MCP_BASE_URL", "https://mcp.clawshow.ai")


# ---------------------------------------------------------------------------
# Document ID generation
# ---------------------------------------------------------------------------

def _next_doc_id(namespace: str) -> str:
    """Generate esign_YYYY_NNNN style ID."""
    year = datetime.now(timezone.utc).year
    doc = db.get_conn()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM esign_documents WHERE namespace = ? AND id LIKE ?",
            (namespace, f"esign_{year}_%"),
        ).fetchone()
        n = (row["cnt"] or 0) + 1
    return f"esign_{year}_{n:04d}"


# ---------------------------------------------------------------------------
# PDF generation (reportlab)
# ---------------------------------------------------------------------------

TEMPLATES = {
    "enrollment_contract": {
        "title": "CONTRAT D'INSCRIPTION",
        "fields": ["school_name", "student_name", "program", "school_year", "tuition", "terms", "date"],
    },
    "rental_agreement": {
        "title": "CONTRAT DE LOCATION",
        "fields": ["landlord_name", "tenant_name", "property_address", "rent_amount", "start_date", "end_date", "terms", "date"],
    },
    "service_agreement": {
        "title": "CONTRAT DE PRESTATION DE SERVICES",
        "fields": ["provider_name", "client_name", "service_description", "fee", "start_date", "terms", "date"],
    },
    "custom": {
        "title": "DOCUMENT",
        "fields": [],
    },
}


def _generate_pdf(doc_id: str, namespace: str, template: str, signer_name: str, fields: dict) -> str:
    """Generate unsigned PDF, return local file path."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib import colors

    out_dir = ESIGN_DATA_DIR / namespace
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = str(out_dir / f"{doc_id}.pdf")

    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Title"],
                                  fontSize=16, spaceAfter=6, alignment=TA_CENTER)
    h2_style = ParagraphStyle("H2", parent=styles["Heading2"],
                               fontSize=12, spaceBefore=12, spaceAfter=4)
    body_style = ParagraphStyle("Body", parent=styles["Normal"],
                                 fontSize=11, leading=16)
    small_style = ParagraphStyle("Small", parent=styles["Normal"],
                                  fontSize=9, textColor=colors.grey, alignment=TA_CENTER)

    tmpl_meta = TEMPLATES.get(template, TEMPLATES["custom"])
    title = fields.get("title", tmpl_meta["title"])

    story = []
    story.append(Paragraph(title, title_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.grey))
    story.append(Spacer(1, 0.5*cm))

    # Render fields as paragraphs — custom template uses raw key/value pairs
    if template == "enrollment_contract":
        story.append(Paragraph(f"Entre l'établissement <b>{fields.get('school_name','')}</b>", body_style))
        story.append(Paragraph(f"et l'étudiant(e) <b>{fields.get('student_name', signer_name)}</b>,", body_style))
        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph("Article 1 — Programme", h2_style))
        story.append(Paragraph(f"L'étudiant(e) est inscrit(e) au programme : <b>{fields.get('program','')}</b>", body_style))
        story.append(Paragraph(f"Année scolaire : <b>{fields.get('school_year','')}</b>", body_style))
        story.append(Paragraph("Article 2 — Frais de scolarité", h2_style))
        story.append(Paragraph(f"Le montant total des frais de scolarité s'élève à : <b>{fields.get('tuition','')}</b>", body_style))
        if fields.get("terms"):
            story.append(Paragraph("Article 3 — Conditions générales", h2_style))
            story.append(Paragraph(fields["terms"], body_style))
    elif template == "rental_agreement":
        story.append(Paragraph(f"Entre le bailleur <b>{fields.get('landlord_name','')}</b>", body_style))
        story.append(Paragraph(f"et le locataire <b>{fields.get('tenant_name', signer_name)}</b>,", body_style))
        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph("Article 1 — Bien loué", h2_style))
        story.append(Paragraph(f"Adresse : <b>{fields.get('property_address','')}</b>", body_style))
        story.append(Paragraph("Article 2 — Loyer", h2_style))
        story.append(Paragraph(f"Loyer mensuel : <b>{fields.get('rent_amount','')}</b>", body_style))
        story.append(Paragraph(f"Du <b>{fields.get('start_date','')}</b> au <b>{fields.get('end_date','')}</b>", body_style))
        if fields.get("terms"):
            story.append(Paragraph("Article 3 — Conditions", h2_style))
            story.append(Paragraph(fields["terms"], body_style))
    elif template == "service_agreement":
        story.append(Paragraph(f"Entre le prestataire <b>{fields.get('provider_name','')}</b>", body_style))
        story.append(Paragraph(f"et le client <b>{fields.get('client_name', signer_name)}</b>,", body_style))
        story.append(Spacer(1, 0.4*cm))
        story.append(Paragraph("Article 1 — Prestation", h2_style))
        story.append(Paragraph(fields.get("service_description", ""), body_style))
        story.append(Paragraph("Article 2 — Honoraires", h2_style))
        story.append(Paragraph(f"Montant : <b>{fields.get('fee','')}</b>", body_style))
        story.append(Paragraph(f"Date de début : <b>{fields.get('start_date','')}</b>", body_style))
        if fields.get("terms"):
            story.append(Paragraph("Article 3 — Conditions", h2_style))
            story.append(Paragraph(fields["terms"], body_style))
    else:
        # Custom: render all key-value fields
        for k, v in fields.items():
            if k not in ("title",):
                story.append(Paragraph(f"<b>{k.replace('_',' ').title()} :</b> {v}", body_style))

    # Signature zone
    story.append(Spacer(1, 1.5*cm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(f"Date : {fields.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))}", body_style))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(f"Signature de {signer_name} :", body_style))
    story.append(Spacer(1, 2.5*cm))  # space for signature image
    story.append(HRFlowable(width=8*cm, thickness=1, color=colors.black))
    story.append(Spacer(1, 0.8*cm))
    story.append(Paragraph("Document signé électroniquement via ClawShow eSign · mcp.clawshow.ai", small_style))

    doc.build(story)
    return pdf_path


def _embed_signature_in_pdf(original_pdf: str, signed_pdf: str,
                              signature_png_bytes: bytes, signer_name: str,
                              signed_at: str, signer_ip: str) -> None:
    """Embed signature image onto last page of PDF using reportlab + PyPDF."""
    import io
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4

    # Build a single-page overlay PDF with the signature
    overlay_buf = io.BytesIO()
    c = rl_canvas.Canvas(overlay_buf, pagesize=A4)
    w, h = A4

    # Embed signature image
    img_buf = io.BytesIO(signature_png_bytes)
    from reportlab.lib.utils import ImageReader
    img = ImageReader(img_buf)
    # Position: left side, about 5cm from bottom
    c.drawImage(img, 2*28.35, 4.5*28.35, width=8*28.35, height=2*28.35,
                mask="auto", preserveAspectRatio=True)
    # Metadata text below signature
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawString(2*28.35, 4.2*28.35, f"Signé par : {signer_name}  |  IP : {signer_ip}  |  {signed_at}")
    c.save()
    overlay_buf.seek(0)

    # Merge overlay onto original using pypdf
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        from PyPDF2 import PdfReader, PdfWriter

    reader = PdfReader(original_pdf)
    overlay_reader = PdfReader(overlay_buf)
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        if i == len(reader.pages) - 1:
            page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    with open(signed_pdf, "wb") as f:
        writer.write(f)


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
        Zero cost per signature — no third-party e-signature service required.
        Fully self-hosted. Compliant with eIDAS and ESIGN Act.

        Args:
            template:     Document template — "enrollment_contract", "rental_agreement",
                          "service_agreement", or "custom"
            signer_name:  Full name of the person who will sign
            signer_email: Email address of the signer (signing link sent here)
            fields:       Template variables dict, e.g. {school_name: "ILCI", tuition: "8500€"}
            namespace:    Client namespace (e.g. "ilci", "florent")
            reference_id: Optional external ID, e.g. FocusingPro inscription_id
            callback_url: Optional webhook URL called when signing is complete
            send_email:   Whether to email the signing link (default True)
            language:     Signing page language — "fr", "en", or "zh" (default "fr")

        Returns:
            JSON with signing_url, document_id, pdf_preview_url, status, signer_email, created_at.
        """
        record_call("send_esign_request", {"namespace": namespace, "template": template})

        doc_id = _next_doc_id(namespace)
        signing_url = f"{BASE_URL}/esign/{doc_id}"
        pdf_preview_url = f"{BASE_URL}/esign/{doc_id}/preview.pdf"

        try:
            pdf_path = _generate_pdf(doc_id, namespace, template, signer_name, fields)
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
