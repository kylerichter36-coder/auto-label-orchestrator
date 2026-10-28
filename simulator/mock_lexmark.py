"""
Mock Lexmark MX511 Scanner
Simulates the Lexmark multifunction scanner sending a scanned quote or pick-and-pack sheet
over SMTP port 2525 with an attached PDF file.
"""

import smtplib
from email.message import EmailMessage
from pathlib import Path
from io import BytesIO

def create_synthetic_pdf(content_text: str = "SYNTHETIC TEST SCAN") -> bytes:
    """Creates a valid minimal PDF buffer for testing."""
    # Minimal standard PDF 1.4 binary structure
    pdf_template = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >> endobj\n"
        b"4 0 obj << /Length 55 >> stream\n"
        b"BT /F1 12 Tf 100 700 Td (" + content_text.encode('ascii') + b") Tj ET\n"
        b"endstream\nendobj\n"
        b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000214 00000 n \n"
        b"trailer << /Size 5 /Root 1 0 R >>\nstartxref\n318\n%%EOF\n"
    )
    return pdf_template

def send_scan_email(
    smtp_host: str = "127.0.0.1",
    smtp_port: int = 2525,
    pdf_bytes: bytes = None,
    filename: str = "scan_quote_20261028_1920.pdf",
    subject: str = "Scanned image from Lexmark MX511"
):
    if pdf_bytes is None:
        pdf_bytes = create_synthetic_pdf("QUOTE QT-90124 | Dell HCF 283x (Qty: 30)")

    msg = EmailMessage()
    msg["From"] = "scanner@lexmark.local"
    msg["To"] = "quotes@orangepi.local"
    msg["Subject"] = subject
    msg.set_content("Please find the attached scanned quote/invoice.")

    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=filename
    )

    print(f"Transmitting simulated Lexmark scan '{filename}' ({len(pdf_bytes)} bytes) to {smtp_host}:{smtp_port}...")
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.send_message(msg)
    print("Scan successfully transmitted via SMTP!")

if __name__ == "__main__":
    send_scan_email()
