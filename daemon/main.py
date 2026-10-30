import asyncio
import logging
import signal
import sys
from aiohttp import web
from daemon.config import config
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.classifier import DocumentClassifier, QuoteCartridgeSheet, OuterBoxSheet
from daemon.color_queue import ColorQueueManager
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.smtp_receiver import LocalSMTPReceiver
from daemon.web_ui import WebDashboard
from daemon.persistence import PersistenceManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("MainOrchestrator")

class AutoLabelOrchestrator:
    def __init__(self):
        logger.info("Initializing Auto-Label Orchestrator...")
        
        # 1. Non-Volatile Persistence Engine
        self.persistence = PersistenceManager()

        # 2. Hardware & Engine Components
        self.printer = ArgoxPrinterEngine(
            device_path=config.PRINTER_DEVICE,
            label_dir=config.LABEL_DIR,
            simulation_mode=config.SIMULATION_MODE
        )
        self.queue_mgr = ColorQueueManager()
        self.queue_mgr.set_persistence_manager(self.persistence)

        # Recover non-volatile queues and outer boxes from SD card/local storage
        recovered = self.persistence.load_active_queues(self.queue_mgr)
        if recovered.get("queues") or recovered.get("outer_boxes"):
            logger.info("Recovered active queues and outer boxes from non-volatile storage.")

        self.key_pool = GeminiKeyPoolManager(
            keys=config.GEMINI_API_KEYS,
            model=config.GEMINI_MODEL,
            max_output_tokens=config.MAX_OUTPUT_TOKENS,
            temperature=config.TEMPERATURE
        )
        self.classifier = DocumentClassifier(self.key_pool)

        # 3. Web UI Dashboard
        self.web_dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool, self.persistence)

        # 4. Local SMTP Ingestion Server
        self.smtp_server = LocalSMTPReceiver(
            host=config.SMTP_HOST,
            port=config.SMTP_PORT,
            on_pdf_received=self.on_pdf_received
        )

    def on_pdf_received(self, pdf_buffer: bytearray, filename: str):
        """
        Callback executed strictly in-memory when the Lexmark MX511 transmits a scanned PDF.
        """
        logger.info(f"Processing in-memory PDF scan '{filename}' through Gemini Flash Classifier...")
        self.persistence.log_transaction("SCAN_RECEIVED", {"filename": filename, "size_bytes": len(pdf_buffer)})
        try:
            # Analyze document
            result = self.classifier.classify_and_extract(bytes(pdf_buffer))

            if isinstance(result, QuoteCartridgeSheet):
                logger.info(f"Classified as Quote #{result.quote_number} with {len(result.items_to_print)} marked cartridge rows.")
                self.persistence.log_transaction(
                    "OCR_RESULT",
                    {"type": "CARTRIDGE_LABELS", "quote_number": result.quote_number, "items_count": len(result.items_to_print)},
                    quote_number=result.quote_number
                )
                for item in result.items_to_print:
                    self.queue_mgr.add_cartridge_item(result.quote_number, item)

            elif isinstance(result, OuterBoxSheet):
                logger.info(f"Classified as Outer Box for Quote #{result.quote_number} (Box {result.current_box}/{result.total_boxes}, Weight: {result.weight_kg}kg)")
                self.persistence.log_transaction(
                    "OCR_RESULT",
                    {"type": "OUTER_BOX", "quote_number": result.quote_number, "box": f"{result.current_box}/{result.total_boxes}"},
                    quote_number=result.quote_number
                )
                self.queue_mgr.add_outer_box(result)

        except Exception as e:
            logger.error(f"Error processing scanned PDF: {e}", exc_info=True)

    async def start(self):
        # Start SMTP server
        self.smtp_server.start()

        # Start Web Dashboard
        runner = web.AppRunner(self.web_dashboard.app)
        await runner.setup()
        site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
        await site.start()
        logger.info(f"BarTender Web Dashboard live at http://{config.WEB_HOST}:{config.WEB_PORT}")
        logger.info(f"Listening for Lexmark scans on {config.SMTP_HOST}:{config.SMTP_PORT}")

        # Keep running
        while True:
            await asyncio.sleep(3600)

    def stop(self):
        logger.info("Shutting down orchestrator...")
        self.persistence.save_active_queues(self.queue_mgr)
        logger.info("Persisted active queues state to non-volatile storage.")
        self.smtp_server.stop()

if __name__ == "__main__":
    orchestrator = AutoLabelOrchestrator()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(orchestrator.start())
    except (KeyboardInterrupt, SystemExit):
        orchestrator.stop()
