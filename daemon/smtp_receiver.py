import email
import gc
import logging
from email import policy
from email.message import EmailMessage
from typing import Callable, Optional
from aiosmtpd.controller import Controller

logger = logging.getLogger("SMTPReceiver")

class InfluxMailHandler:
    """
    aiosmtpd handler that extracts PDF attachments strictly in RAM,
    passes them to the processing pipeline, and guarantees instant buffer shredding.
    """
    def __init__(self, on_pdf_received: Callable[[bytearray, str], None]):
        self.on_pdf_received = on_pdf_received

    async def handle_DATA(self, server, session, envelope):
        peer = session.peer
        mail_from = envelope.mail_from
        logger.info(f"Incoming transmission from {peer[0]}:{peer[1]} (From: {mail_from})")

        # Parse MIME message in memory
        raw_content = envelope.content
        if isinstance(raw_content, str):
            raw_bytes = raw_content.encode("utf-8", errors="replace")
        else:
            raw_bytes = bytes(raw_content)

        msg: EmailMessage = email.message_from_bytes(raw_bytes, policy=policy.default)
        pdf_found = False

        # Walk parts to find PDF attachments
        for part in msg.walk():
            content_type = part.get_content_type()
            filename = part.get_filename() or ""
            is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")

            if is_pdf:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                pdf_found = True
                size_kb = len(payload) / 1024.0
                logger.info(f"Extracted PDF attachment '{filename}' ({size_kb:.1f} KB) strictly in-memory.")

                # Convert to mutable bytearray for zero-overwrite shredding
                mem_buffer = bytearray(payload)
                del payload # drop original immutable bytes reference

                try:
                    # Pass mutable in-memory buffer to processing pipeline
                    self.on_pdf_received(mem_buffer, filename)
                finally:
                    # Execute Instant Shred Protocol
                    self._shred_buffer(mem_buffer)
                    del mem_buffer
                    gc.collect()
                    logger.info(f"Confidential Shred: in-memory PDF buffer zeroed and garbage collected.")

        if not pdf_found:
            logger.warning("Email received from scanner but no PDF attachment was present.")

        return "250 Message accepted for in-memory processing"

    def _shred_buffer(self, buffer: bytearray):
        """Zero-overwrites raw memory buffer before unlinking."""
        for i in range(len(buffer)):
            buffer[i] = 0


class LocalSMTPReceiver:
    def __init__(self, host: str, port: int, on_pdf_received: Callable[[bytearray, str], None]):
        self.host = host
        self.port = port
        self.handler = InfluxMailHandler(on_pdf_received)
        self.controller = Controller(self.handler, hostname=self.host, port=self.port)

    def start(self):
        logger.info(f"Starting local SMTP listener on {self.host}:{self.port}...")
        self.controller.start()
        logger.info(f"SMTP listener active! Ready to receive scans from Lexmark MX511.")

    def stop(self):
        logger.info("Stopping local SMTP listener...")
        self.controller.stop()
