"""PDF watermark: diagonal TEST stamp + top red banner."""
import io
import logging

logger = logging.getLogger(__name__)


def create_watermark_pdf(page_width: float, page_height: float) -> bytes:
    from reportlab.pdfgen import canvas
    from reportlab.lib.colors import Color

    packet = io.BytesIO()
    cv = canvas.Canvas(packet, pagesize=(page_width, page_height))

    # Top red banner
    banner_h = 24
    cv.setFillColor(Color(0.85, 0.1, 0.1, alpha=0.92))
    cv.rect(0, page_height - banner_h, page_width, banner_h, fill=True, stroke=False)
    cv.setFillColor(Color(1, 1, 1))
    cv.setFont("Helvetica-Bold", 10)
    cv.drawCentredString(
        page_width / 2, page_height - 16,
        "DOCUMENT TEST - SIGNE SANS ACCEPTATION DES CGU - AUCUNE VALEUR JURIDIQUE"
    )

    # Diagonal TEST watermark
    cv.saveState()
    cv.translate(page_width / 2, page_height / 2)
    cv.rotate(45)
    cv.setFillColor(Color(0.85, 0.1, 0.1, alpha=0.18))
    cv.setFont("Helvetica-Bold", 110)
    cv.drawCentredString(0, 20, "TEST")
    cv.setFillColor(Color(0.85, 0.1, 0.1, alpha=0.28))
    cv.setFont("Helvetica", 22)
    cv.drawCentredString(0, -40, "AUCUNE VALEUR JURIDIQUE")
    cv.restoreState()

    cv.save()
    packet.seek(0)
    return packet.read()


def apply_watermark(pdf_bytes: bytes) -> bytes:
    """Apply watermark to every page of a PDF, return new PDF bytes."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        wm_bytes = create_watermark_pdf(w, h)
        wm_page = PdfReader(io.BytesIO(wm_bytes)).pages[0]
        page.merge_page(wm_page)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    result = out.read()
    logger.info("Watermark applied: %d → %d bytes", len(pdf_bytes), len(result))
    return result
