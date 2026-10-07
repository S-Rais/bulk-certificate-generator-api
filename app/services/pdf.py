"""Certificate rendering. One fixed template, drawn with ReportLab (pure Python, no system deps)."""
import os
from datetime import date
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

NAVY = HexColor("#12284c")
GOLD = HexColor("#b8892d")
GREY = HexColor("#5c6370")

PAGE_W, PAGE_H = landscape(A4)
MAX_TEXT_W = PAGE_W - 180


def _fit_font_size(text: str, font: str, start: float, minimum: float, max_width: float) -> float:
    size = start
    while size > minimum and stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def render_certificate(
    out_path: Path,
    *,
    recipient_name: str,
    course_name: str,
    issuer_name: str,
    issue_date: date,
    certificate_id: str,
    verification_code: str,
    verification_url: str,
) -> None:
    """Render to a temp file then atomically rename, so a crash never leaves a half-written PDF."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".pdf.tmp")

    c = canvas.Canvas(str(tmp), pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"Certificate - {recipient_name}")
    c.setAuthor(issuer_name)
    c.setSubject(course_name)

    # Double border
    c.setStrokeColor(NAVY)
    c.setLineWidth(6)
    c.rect(24, 24, PAGE_W - 48, PAGE_H - 48)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.5)
    c.rect(38, 38, PAGE_W - 76, PAGE_H - 76)

    cx = PAGE_W / 2
    c.setFillColor(NAVY)
    c.setFont("Times-Bold", 40)
    c.drawCentredString(cx, PAGE_H - 125, "CERTIFICATE OF COMPLETION")

    c.setStrokeColor(GOLD)
    c.setLineWidth(2)
    c.line(cx - 110, PAGE_H - 142, cx + 110, PAGE_H - 142)

    c.setFillColor(GREY)
    c.setFont("Helvetica", 14)
    c.drawCentredString(cx, PAGE_H - 185, "This is to certify that")

    name_font = "Times-BoldItalic"
    size = _fit_font_size(recipient_name, name_font, 46, 10, MAX_TEXT_W)
    c.setFillColor(NAVY)
    c.setFont(name_font, size)
    c.drawCentredString(cx, PAGE_H - 245, recipient_name)
    name_w = stringWidth(recipient_name, name_font, size)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(cx - name_w / 2 - 10, PAGE_H - 255, cx + name_w / 2 + 10, PAGE_H - 255)

    c.setFillColor(GREY)
    c.setFont("Helvetica", 14)
    c.drawCentredString(cx, PAGE_H - 295, "has successfully completed")

    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 24)
    lines = simpleSplit(course_name, "Helvetica-Bold", 24, MAX_TEXT_W)[:3]
    y = PAGE_H - 335
    for line in lines:
        c.drawCentredString(cx, y, line)
        y -= 30

    # Footer: issuer + date (left), QR (right)
    c.setFillColor(NAVY)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(90, 110, issuer_name[:60])
    c.setFillColor(GREY)
    c.setFont("Helvetica", 11)
    c.drawString(90, 92, f"Issued on {issue_date.strftime('%d %B %Y')}")

    c.setFont("Helvetica", 8)
    c.drawString(90, 60, f"Certificate ID: {certificate_id}")
    c.drawString(90, 49, f"Verify: {verification_url}")

    qr = QrCodeWidget(verification_url)
    x1, y1, x2, y2 = qr.getBounds()
    box = 80
    d = Drawing(box, box, transform=[box / (x2 - x1), 0, 0, box / (y2 - y1), 0, 0])
    d.add(qr)
    renderPDF.draw(d, c, PAGE_W - 90 - box, 52)
    c.setFont("Helvetica", 7)
    c.drawCentredString(PAGE_W - 90 - box / 2, 44, "Scan to verify")

    c.showPage()
    c.save()
    os.replace(tmp, out_path)
