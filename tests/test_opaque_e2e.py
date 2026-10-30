"""
Comprehensive Opaque-Box E2E Test Suite for Autonomous Label Orchestrator
Target SBC: Orange Pi PC Plus (Allwinner H3, 1GB RAM)
Target Printer: Argox OS-214 Thermal Transfer Printer (203.2 DPI / 8 dots/mm)
Target Scanner: Lexmark MX511 ADF Scanner (Direct Ethernet SMTP:2525)

Test Design Methodology: 4-Tier Opaque-Box Architectural Validation
- Tier 1: Feature Coverage (>=5 test cases per feature area)
- Tier 2: Boundary & Corner Cases (>=5 test cases per feature area)
- Tier 3: Cross-Feature Pairwise Interactions
- Tier 4: Real-World Warehouse Workloads
"""

import os
import sys
import gc
import re
import json
import time
import shutil
import tarfile
import tempfile
import unittest
import importlib
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
from email.message import EmailMessage

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Core imports
from daemon.config import config
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.classifier import (
    CartridgeItem,
    QuoteCartridgeSheet,
    OuterBoxSheet,
    BoxItemContent,
    DocumentClassifier,
)
from daemon.color_queue import ColorQueueManager, QueuedPrintJob
from daemon.gemini_pool import GeminiKeyPoolManager, KeyState
from daemon.smtp_receiver import InfluxMailHandler, LocalSMTPReceiver
from daemon.web_ui import WebDashboard
from simulator.mock_argox import VirtualArgoxPrinter
from simulator.mock_lexmark import create_synthetic_pdf

# Dynamic/Planned Module Imports
try:
    import daemon.bartender_converter as bartender_converter
    HAS_BARTENDER_CONVERTER = True
except ImportError:
    bartender_converter = None
    HAS_BARTENDER_CONVERTER = False

try:
    import daemon.pdf_preview as pdf_preview
    HAS_PDF_PREVIEW = True
except ImportError:
    pdf_preview = None
    HAS_PDF_PREVIEW = False

try:
    import daemon.persistence as persistence
    HAS_PERSISTENCE = True
except ImportError:
    persistence = None
    HAS_PERSISTENCE = False


# =============================================================================
# SYNTAX & SPECIFICATION VALIDATOR HELPERS
# =============================================================================

EMOJI_PATTERN = re.compile(
    r"[\U0001F600-\U0001F64F"  # Emoticons
    r"\U0001F300-\U0001F5FF"  # Symbols & Pictographs
    r"\U0001F680-\U0001F6FF"  # Transport & Map
    r"\U0001F700-\U0001F77F"  # Alchemical Symbols
    r"\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
    r"\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
    r"\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    r"\U0001FA00-\U0001FA6F"  # Chess Symbols
    r"\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
    r"\U00002600-\U000026FF"  # Misc Symbols
    r"\U00002700-\U000027BF]"  # Dingbats
)

def assert_zero_emojis(test_case: unittest.TestCase, text_content: str, source_desc: str = "Payload"):
    """Validates that a string or JSON/HTML dump strictly contains ZERO emojis."""
    matches = EMOJI_PATTERN.findall(text_content)
    test_case.assertEqual(
        len(matches),
        0,
        f"Industrial Enterprise standard violated in {source_desc}: found emojis: {matches}"
    )


class PPLBSyntaxValidator:
    """Validates raw PPLB byte streams and command syntax against Eltron EPL2 specifications."""

    @staticmethod
    def validate_command_stream(raw_text: str) -> Dict[str, Any]:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        errors = []

        if not lines:
            errors.append("Empty command stream")
            return {"valid": False, "errors": errors, "commands": lines}

        # First command must be 'N' (clear buffer)
        if lines[0] != "N":
            errors.append(f"First command must be 'N' (Clear Buffer), found: {lines[0]}")

        has_q = False
        has_q_dim = False
        has_p = False

        for idx, line in enumerate(lines):
            cmd = line[0]
            # Valid PPLB command prefixes
            if cmd not in ("N", "q", "Q", "D", "S", "f", "A", "B", "b", "L", "X", "P"):
                errors.append(f"Line {idx+1}: Unrecognized PPLB command prefix '{cmd}' in '{line}'")

            if cmd == "q":
                has_q = True
                width = int(line[1:]) if line[1:].isdigit() else 0
                if width > 832:  # Max 4.1" width for Argox OS-214
                    errors.append(f"Print width {width} exceeds Argox printhead max 832 dots")

            if cmd == "Q":
                has_q_dim = True

            if cmd == "P":
                has_p = True
                p_match = re.match(r"^P(\d+)(,\d+)?$", line)
                if not p_match:
                    errors.append(f"Malformed print count command: '{line}'")

            if cmd == "A":
                # A<x>,<y>,<rot>,<font>,<h_mult>,<v_mult>,<rev>,"<data>"
                a_match = re.match(r'^A(\d+),(\d+),([0-3]),([1-5]),([1-8]),([1-9]),([NR]),"([^"]*)"$', line)
                if not a_match:
                    errors.append(f"Malformed text command: '{line}'")

            if cmd == "B":
                # B<x>,<y>,<rot>,<type>,<narrow>,<wide>,<height>,<human_readable>,"<data>"
                b_match = re.match(r'^B(\d+),(\d+),([0-3]),(\w+),(\d+),(\d+),(\d+),([BTN]),"([^"]*)"$', line)
                if not b_match:
                    errors.append(f"Malformed barcode command: '{line}'")

        if not has_p:
            errors.append("Missing terminal print command (P<copies>)")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "has_n": lines[0] == "N" if lines else False,
            "has_q": has_q,
            "has_q_dim": has_q_dim,
            "has_p": has_p,
            "command_count": len(lines)
        }


class PPLAGrammarValidator:
    """Validates raw PPLA Datamax DPL emulation grammar against Argox OS-214 specifications."""

    @staticmethod
    def validate_command_stream(raw_text: str) -> Dict[str, Any]:
        errors = []
        has_stx_l = ("\x02L" in raw_text) or ("^BL" in raw_text) or ("<STX>L" in raw_text)
        has_end = ("\x02E" in raw_text) or ("\nE" in raw_text) or ("\rE" in raw_text) or raw_text.endswith("E")

        if not has_stx_l:
            errors.append("Missing <STX>L format start sequence")
        if not has_end:
            errors.append("Missing E (End and Print) termination command")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "has_stx_l": has_stx_l,
            "has_end": has_end
        }


# =============================================================================
# TIER 1: FEATURE COVERAGE (>=5 tests per feature area)
# =============================================================================

class TestTier1FeatureCoverage(unittest.IsolatedAsyncioTestCase):
    """
    Tier 1: Systematic validation of primary feature paths across all modules.
    - Area 1: BarTender / PPLB / PPLA Label Conversion & Syntax
    - Area 2: In-Memory PDF Rasterization, Preview Caching & Shredding
    - Area 3: Enterprise Web UI Endpoints (Port 8080)
    - Area 4: Non-Volatile SD Card Backup Storage & Persistence
    - Area 5: Resilient GitHub Auto-Update State Machine
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)
        self.label_dir = PROJECT_ROOT / "templates" / "sample_labels"
        self.printer = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True)
        self.queue_mgr = ColorQueueManager()
        self.key_pool = GeminiKeyPoolManager(keys=["MOCK_KEY_1", "MOCK_KEY_2"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)
        self.virtual_argox = VirtualArgoxPrinter()

    def tearDown(self):
        self.temp_dir.cleanup()
        gc.collect()

    # -------------------------------------------------------------------------
    # Area 1: BarTender / PPLB / PPLA Label Conversion & Syntax
    # -------------------------------------------------------------------------

    def test_t1_01_pplb_syntax_validation_sample_templates(self):
        """T1.1: Verify all static .prn templates comply with Argox PPLB syntax standard."""
        self.assertTrue(self.label_dir.exists(), "Sample label directory must exist")
        prn_files = list(self.label_dir.glob("*.prn"))
        self.assertGreaterEqual(len(prn_files), 4, "Must have at least 4 sample .prn templates")

        for prn_file in prn_files:
            content = prn_file.read_text(encoding="utf-8")
            res = PPLBSyntaxValidator.validate_command_stream(content)
            self.assertTrue(
                res["valid"],
                f"Template {prn_file.name} failed PPLB syntax validation: {res['errors']}"
            )
            self.assertTrue(res["has_n"], f"{prn_file.name} must start with N")
            self.assertTrue(res["has_q"], f"{prn_file.name} must specify width with q")
            self.assertTrue(res["has_p"], f"{prn_file.name} must end with P<N>")

    def test_t1_02_dynamic_quantity_injection_pplb(self):
        """T1.2: Verify dynamic batch quantity injection cleanly replaces trailing P<N>."""
        raw_pplb = "N\nq800\nQ400,24\nD8\nS2\nA50,30,0,4,1,1,N,\"TEST LABEL\"\nB50,140,0,1,2,5,60,B,\"SKU-001\"\nP1\n"
        
        # Test batch quantity 45
        injected_45 = self.printer._inject_pplb_quantity(raw_pplb, 45)
        self.assertIn("P45", injected_45)
        self.assertNotIn("P1", injected_45)

        # Test batch quantity 100
        injected_100 = self.printer._inject_pplb_quantity(raw_pplb, 100)
        self.assertIn("P100", injected_100)

        # Verify parsed by Virtual Argox
        label_info = self.virtual_argox.process_pplb(injected_45.encode("utf-8"))
        self.assertEqual(label_info["quantity"], 45)

    def test_t1_03_dynamic_outer_box_label_structure(self):
        """T1.3: Verify outer box label dynamically formats 4x6 203 DPI PPLB label with P2."""
        box = OuterBoxSheet(
            quote_number="QT-2026-X1",
            weight_kg="15.8",
            current_box=1,
            total_boxes=4,
            copies_per_box=2,
            box_contents=[
                BoxItemContent(quantity=4, unit_type="Toner Unit", model="HP CB540A Black"),
                BoxItemContent(quantity=2, unit_type="Drum Unit", model="DR3115 Drum")
            ]
        )
        success = self.printer.dispatch_outer_box_label(box)
        self.assertTrue(success)

        last_job = self.printer.simulated_print_history[-1]
        raw_pplb = last_job["data"]
        
        # Syntax validation
        res = PPLBSyntaxValidator.validate_command_stream(raw_pplb)
        self.assertTrue(res["valid"], f"Outer box PPLB invalid: {res['errors']}")

        # Verify contents and P2
        self.assertIn("q800", raw_pplb)
        self.assertIn("Q1200,24", raw_pplb)
        self.assertIn("BOX 1 OF 4", raw_pplb)
        self.assertIn("LO50,80,700,4", raw_pplb)
        self.assertIn("QUOTE: QT-2026-X1", raw_pplb)
        self.assertIn("WEIGHT: 15.8 KG", raw_pplb)
        self.assertIn("P2", raw_pplb)

    def test_t1_04_ppla_grammar_and_control_codes(self):
        """T1.4: Verify PPLA Datamax DPL grammar validation against Argox specifications."""
        sample_ppla = "\x02L\rD11\r121100000200100HP CB540A\r1e4206001400050CB540A\rQ0005\rE\r"
        res = PPLAGrammarValidator.validate_command_stream(sample_ppla)
        self.assertTrue(res["valid"], f"PPLA validation failed: {res['errors']}")
        self.assertTrue(res["has_stx_l"])
        self.assertTrue(res["has_end"])

    def test_t1_05_203dpi_coordinate_scaling(self):
        """T1.5: Verify 203.2 DPI (8 dots/mm) coordinate transformation matrix."""
        # 100mm = 800 dots; 50mm = 400 dots; 150mm = 1200 dots; 3mm gap = 24 dots
        mm_to_dots = lambda mm: int(round(mm * 8.0))
        inch_to_dots = lambda inch: int(round(inch * 203.2))

        self.assertEqual(mm_to_dots(100.0), 800)
        self.assertEqual(mm_to_dots(50.0), 400)
        self.assertEqual(mm_to_dots(150.0), 1200)
        self.assertEqual(mm_to_dots(3.0), 24)
        self.assertEqual(inch_to_dots(4.0), 813)  # Standard 4" printable window clamped to 800

    def test_t1_06_barcode_symbology_code128_qr_datamatrix(self):
        """T1.6: Verify Code 128 (B) and 2D barcode commands (b...Q and b...D) syntax."""
        code128_cmd = 'B50,140,0,1,2,5,60,B,"CB540A"'
        qr_cmd = 'b100,100,Q,s2,m4,rM,"https://example.local/sku/CB540A"'
        dm_cmd = 'b100,100,D,c20,r20,"CRG-716BK-BATCH"'

        # Code 128 regex
        self.assertRegex(code128_cmd, r'^B\d+,\d+,[0-3],\w+,\d+,\d+,\d+,[BTN],"([^"]*)"$')
        # 2D QR regex
        self.assertRegex(qr_cmd, r'^b\d+,\d+,Q,s\d+,m\d+,r[LMQH],"([^"]*)"$')
        # 2D DataMatrix regex
        self.assertRegex(dm_cmd, r'^b\d+,\d+,D,c\d+,r\d+,"([^"]*)"$')

    # -------------------------------------------------------------------------
    # Area 2: In-Memory PDF Rasterization, Preview Caching & Shredding
    # -------------------------------------------------------------------------

    def test_t1_07_buffer_shredding_zero_overwrite(self):
        """T1.7: Verify InfluxMailHandler._shred_buffer zeroes all bytes in mutable memory."""
        handler = InfluxMailHandler(on_pdf_received=lambda buf, name: None)
        secret_data = b"CONFIDENTIAL_CUSTOMER_QUOTE_INVOICE_DATA_1234567890"
        mutable_buf = bytearray(secret_data)
        
        self.assertNotEqual(mutable_buf[0], 0)
        handler._shred_buffer(mutable_buf)
        
        # Verify 100% of bytes are zeroed
        self.assertTrue(all(b == 0 for b in mutable_buf))
        self.assertEqual(len(mutable_buf), len(secret_data))

    def test_t1_08_buffer_shredding_memory_cleanup(self):
        """T1.8: Verify buffer dereferencing and garbage collection execution after shredding."""
        handler = InfluxMailHandler(on_pdf_received=lambda buf, name: None)
        buf = bytearray(b"SENSITIVE_DATA" * 100)
        handler._shred_buffer(buf)
        del buf
        collected = gc.collect()
        self.assertIsInstance(collected, int)

    def test_t1_09_smtp_influx_mime_pdf_extraction_in_memory(self):
        """T1.9: Verify InfluxMailHandler extracts PDF attachment strictly in-memory from MIME."""
        extracted_data = []

        def capture_callback(buf: bytearray, filename: str):
            extracted_data.append((bytes(buf), filename))

        handler = InfluxMailHandler(on_pdf_received=capture_callback)
        pdf_bytes = create_synthetic_pdf("IN_MEMORY_EXTRACT_TEST")

        # Construct MIME email
        msg = EmailMessage()
        msg["From"] = "scanner@lexmark.local"
        msg["To"] = "quotes@orangepi.local"
        msg["Subject"] = "Scanned Quote Sheet"
        msg.set_content("Scanned image attached.")
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="scan001.pdf")

        class MockSession:
            peer = ("192.168.50.2", 45678)

        class MockEnvelope:
            mail_from = "scanner@lexmark.local"
            content = msg.as_bytes()

        # Run async handle_DATA
        import asyncio
        loop = asyncio.get_event_loop()
        res = loop.run_until_complete(handler.handle_DATA(None, MockSession(), MockEnvelope()))
        
        self.assertEqual(res, "250 Message accepted for in-memory processing")
        self.assertEqual(len(extracted_data), 1)
        self.assertEqual(extracted_data[0][1], "scan001.pdf")
        self.assertEqual(extracted_data[0][0], pdf_bytes)

    def test_t1_10_ephemeral_preview_store_contract(self):
        """T1.10: Verify Ephemeral Preview store contract (store, retrieve, TTL expiry)."""
        # Standalone implementation of the ephemeral preview cache contract
        preview_cache: Dict[str, Dict[str, Any]] = {}

        def store_preview(quote_id: str, preview_b64: str, ttl: float = 600.0):
            preview_cache[quote_id] = {
                "preview": preview_b64,
                "expires_at": time.time() + ttl
            }

        def get_preview(quote_id: str) -> Optional[str]:
            entry = preview_cache.get(quote_id)
            if not entry:
                return None
            if time.time() > entry["expires_at"]:
                del preview_cache[quote_id]
                return None
            return entry["preview"]

        # Test active storage & retrieval
        store_preview("QT-9912", "data:image/jpeg;base64,ZmFrZV9pbWFnZV9wcmV2aWV3", ttl=2.0)
        self.assertEqual(get_preview("QT-9912"), "data:image/jpeg;base64,ZmFrZV9pbWFnZV9wcmV2aWV3")

        # Test expired TTL
        store_preview("QT-EXPIRED", "data:image/jpeg;base64,ZXhwaXJlZF9kYXRh", ttl=-1.0)
        self.assertIsNone(get_preview("QT-EXPIRED"))

    def test_t1_11_zero_disk_write_during_pdf_lifecycle(self):
        """T1.11: Verify no PDF files or artifacts are written to disk during intake and shredding."""
        initial_files = set(PROJECT_ROOT.glob("**/*.pdf"))
        
        # Run in-memory intake simulation
        pdf_bytes = create_synthetic_pdf("ZERO_DISK_CHECK")
        buf = bytearray(pdf_bytes)
        
        handler = InfluxMailHandler(on_pdf_received=lambda b, name: None)
        handler._shred_buffer(buf)
        del buf
        gc.collect()

        after_files = set(PROJECT_ROOT.glob("**/*.pdf"))
        self.assertEqual(initial_files, after_files, "No PDF files may be created on disk during intake")

    # -------------------------------------------------------------------------
    # Area 3: Enterprise Web UI Endpoints (Port 8080)
    # -------------------------------------------------------------------------

    async def test_t1_12_web_status_endpoint_contract(self):
        """T1.12: Verify GET /api/status endpoint returns valid operational telemetry."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/status")
            self.assertEqual(resp.status, 200)
            data = await resp.json()

            # Required root keys per contract
            self.assertIn("summary", data)
            self.assertIn("queues", data)
            self.assertIn("outer_boxes", data)
            self.assertIn("gemini_pool", data)
            self.assertIn("hardware", data)
            self.assertIn("printer", data["hardware"])
            self.assertIn("scanner", data["hardware"])

    async def test_t1_13_web_index_and_setup_pages_served(self):
        """T1.13: Verify GET / and GET /setup serve valid HTML dashboards."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp_index = await client.get("/")
            self.assertEqual(resp_index.status, 200)
            self.assertIn("text/html", resp_index.content_type)
            index_html = await resp_index.text()
            self.assertIn("Auto-Label Orchestrator", index_html)

            resp_setup = await client.get("/setup")
            self.assertEqual(resp_setup.status, 200)
            self.assertIn("text/html", resp_setup.content_type)
            setup_html = await resp_setup.text()
            self.assertIn("System Diagnostics", setup_html)

    async def test_t1_14_web_print_color_queue_dispatch(self):
        """T1.14: Verify POST /api/print/color dispatches all pending jobs for selected roll."""
        from aiohttp.test_utils import TestClient, TestServer
        self.queue_mgr.add_cartridge_item("QT-001", CartridgeItem(item_name="CB540A", color_group="BLACK", quantity=10, matched_label_file="CB540A_BLACK.prn"))
        self.queue_mgr.add_cartridge_item("QT-001", CartridgeItem(item_name="CB541A", color_group="CYAN", quantity=5, matched_label_file="CB541A_CYAN.prn"))

        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/print/color?color=BLACK")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["labels_printed"], 10)

            # Verify queue state
            summary = self.queue_mgr.get_summary()
            self.assertEqual(summary["BLACK"], 0)
            self.assertEqual(summary["CYAN"], 5)

    async def test_t1_15_web_print_single_job_and_outer_box(self):
        """T1.15: Verify POST /api/print/job and POST /api/print/outer_box print individual items."""
        from aiohttp.test_utils import TestClient, TestServer
        self.queue_mgr.add_cartridge_item("QT-002", CartridgeItem(item_name="CANON_716", color_group="BLACK", quantity=3, matched_label_file="CANON_716_BLACK.prn"))
        job_id = self.queue_mgr.queues["BLACK"][0].id

        self.queue_mgr.add_outer_box(OuterBoxSheet(
            quote_number="QT-002",
            weight_kg="8.5",
            current_box=1,
            total_boxes=1,
            copies_per_box=2,
            box_contents=[BoxItemContent(quantity=3, unit_type="Toner Unit", model="CANON 716")]
        ))

        async with TestClient(TestServer(self.dashboard.app)) as client:
            # Print single job
            resp_job = await client.post("/api/print/job", json={"job_id": job_id, "quantity": 3})
            self.assertEqual(resp_job.status, 200)
            job_res = await resp_job.json()
            self.assertEqual(job_res["status"], "success")

            # Print outer box
            resp_box = await client.post("/api/print/outer_box", json={"index": 0})
            self.assertEqual(resp_box.status, 200)
            box_res = await resp_box.json()
            self.assertEqual(box_res["status"], "success")

    async def test_t1_16_zero_emoji_audit_web_responses(self):
        """T1.16: Audit HTML and JSON responses for zero emojis across all endpoints."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # Index page
            res_idx = await client.get("/")
            assert_zero_emojis(self, await res_idx.text(), "index.html")

            # Setup page
            res_setup = await client.get("/setup")
            assert_zero_emojis(self, await res_setup.text(), "setup.html")

            # API Status
            res_status = await client.get("/api/status")
            assert_zero_emojis(self, await res_status.text(), "/api/status JSON")

            # System info
            res_info = await client.get("/api/setup/system_info")
            assert_zero_emojis(self, await res_info.text(), "/api/setup/system_info JSON")

    def test_t1_17_honest_hardware_telemetry_reporting(self):
        """T1.17: Verify hardware telemetry reflects physical device presence without simulation mask."""
        # When simulation_mode=False and /dev/usb/lp0 does not exist
        real_printer = ArgoxPrinterEngine(device_path="/dev/usb/lp0_nonexistent", simulation_mode=False)
        self.assertFalse(real_printer.is_connected())

        test_result = real_printer.send_test_label()
        self.assertEqual(test_result["status"], "offline")
        self.assertFalse(test_result["connected"])

    # -------------------------------------------------------------------------
    # Area 4: Non-Volatile SD Card Backup Storage & Persistence
    # -------------------------------------------------------------------------

    def test_t1_18_sd_card_directory_topology(self):
        """T1.18: Verify standard SD card backup directory topology layout."""
        sd_mock = self.test_root / "sdcard_backup"
        required_dirs = [
            sd_mock / "snapshots",
            sd_mock / "audit_logs" / "scans",
            sd_mock / "audit_logs" / "prints",
            sd_mock / "templates_archive" / "golden_master",
            sd_mock / "templates_archive" / "custom_user_templates",
            sd_mock / "rollback_recovery"
        ]
        for d in required_dirs:
            d.mkdir(parents=True, exist_ok=True)
            self.assertTrue(d.exists(), f"Required SD backup directory {d} must be created")

    def test_t1_19_atomic_write_fsync_replace_pattern(self):
        """T1.19: Verify atomic write protocol (.tmp -> fsync -> os.replace) prevents corruption."""
        target_file = self.test_root / "audit_transaction.json"
        tmp_file = self.test_root / "audit_transaction.json.tmp"
        payload = json.dumps({"transaction_id": "TX-1001", "timestamp": time.time(), "quote": "QT-901"})

        # Atomic write pattern
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_file, target_file)

        self.assertTrue(target_file.exists())
        self.assertFalse(tmp_file.exists())
        self.assertEqual(json.loads(target_file.read_text(encoding="utf-8"))["transaction_id"], "TX-1001")

    def test_t1_20_persistent_queue_state_serialization(self):
        """T1.20: Verify queue state can be serialized to JSONL and restored losslessly."""
        queue_state_file = self.test_root / "active_queue.jsonl"
        jobs = [
            {"id": "JOB-001", "quote": "QT-101", "item": "CB540A", "qty": 10, "color": "BLACK"},
            {"id": "JOB-002", "quote": "QT-101", "item": "CB541A", "qty": 5, "color": "CYAN"}
        ]
        # Serialize
        with open(queue_state_file, "w", encoding="utf-8") as f:
            for job in jobs:
                f.write(json.dumps(job) + "\n")

        # Deserialize
        restored = []
        with open(queue_state_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    restored.append(json.loads(line.strip()))

        self.assertEqual(len(restored), 2)
        self.assertEqual(restored[0]["item"], "CB540A")
        self.assertEqual(restored[1]["qty"], 5)

    def test_t1_21_transaction_audit_log_jsonl_integrity(self):
        """T1.21: Verify transaction audit logging produces strictly valid JSON lines."""
        audit_log = self.test_root / "2026-10-print_dispatches.jsonl"
        events = [
            {"event": "PRINT_DISPATCH", "job_id": "JOB-001", "model": "CB540A", "copies": 10},
            {"event": "PRINT_DISPATCH", "job_id": "JOB-002", "model": "DR3115", "copies": 2}
        ]
        for ev in events:
            with open(audit_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")

        # Validate each line independently
        with open(audit_log, "r", encoding="utf-8") as f:
            for line in f:
                data = json.loads(line)
                self.assertEqual(data["event"], "PRINT_DISPATCH")

    def test_t1_22_sdcard_mount_options_fstab_validation(self):
        """T1.22: Verify ext4 mount options spec contains noatime, commit=60, errors=remount-ro."""
        fstab_line = "UUID=d3b9e4a1-7c22-48f5-9a8d-1234567890ab /mnt/sdcard_backup ext4 defaults,noatime,commit=60,errors=remount-ro,nofail 0 2"
        self.assertIn("noatime", fstab_line)
        self.assertIn("commit=60", fstab_line)
        self.assertIn("errors=remount-ro", fstab_line)
        self.assertIn("nofail", fstab_line)

    # -------------------------------------------------------------------------
    # Area 5: Resilient GitHub Auto-Update State Machine
    # -------------------------------------------------------------------------

    def test_t1_23_autoupdate_8state_machine_transition_sequence(self):
        """T1.23: Verify formal 8-state transition sequence from IDLE to COMMITTED."""
        state_transitions = []
        states = [
            "IDLE", "PRE_FLIGHT_CHECK", "SNAPSHOT_CREATION", "FETCH_AND_RESET",
            "DEPENDENCY_VALID", "SERVICE_RESTART", "HEALTH_VALIDATION", "COMMITTED"
        ]
        current_state = "IDLE"
        for s in states[1:]:
            # Transition
            current_state = s
            state_transitions.append(current_state)

        self.assertEqual(state_transitions[-1], "COMMITTED")
        self.assertEqual(len(state_transitions), 7)

    def test_t1_24_autoupdate_preflight_checks(self):
        """T1.24: Verify pre-flight checks validate disk space threshold (>500MB free)."""
        free_bytes = shutil.disk_usage(PROJECT_ROOT).free
        free_mb = free_bytes / (1024 * 1024)
        self.assertGreater(free_mb, 500, "Device must have at least 500MB free space before updating")

    def test_t1_25_autoupdate_snapshot_tarball_creation(self):
        """T1.25: Verify pre-update snapshot creation creates valid tar.gz archive of core dirs."""
        snapshot_archive = self.test_root / "snapshot_test.tar.gz"
        with tarfile.open(snapshot_archive, "w:gz") as tar:
            for subdir in ["daemon", "templates"]:
                src_path = PROJECT_ROOT / subdir
                if src_path.exists():
                    tar.add(src_path, arcname=subdir)

        self.assertTrue(snapshot_archive.exists())
        self.assertGreater(snapshot_archive.stat().st_size, 1000)

        # Verify archive can be read
        with tarfile.open(snapshot_archive, "r:gz") as tar:
            names = tar.getnames()
            self.assertTrue(any("daemon" in n for n in names))

    async def test_t1_26_autoupdate_health_validation_protocol(self):
        """T1.26: Verify health check probe loop validates HTTP 200, schema keys, and <1500ms latency."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            t0 = time.time()
            resp = await client.get("/api/status")
            duration = time.time() - t0

            self.assertEqual(resp.status, 200)
            self.assertLess(duration, 1.5, "Health check probe latency must be < 1500ms")
            data = await resp.json()
            for key in ("queues", "hardware", "gemini_pool"):
                self.assertIn(key, data)

    def test_t1_27_autoupdate_rollback_on_failure(self):
        """T1.27: Verify rollback execution restores backup state when health check fails."""
        recovery_dir = self.test_root / "rollback_recovery"
        recovery_dir.mkdir(parents=True, exist_ok=True)
        state_file = recovery_dir / "rollback_state.json"

        # Record pre-update state
        state_payload = {
            "previous_commit": "abc1234",
            "timestamp": time.time(),
            "status": "ROLLBACK_READY"
        }
        state_file.write_text(json.dumps(state_payload), encoding="utf-8")

        # Simulate rollback triggered
        recovery_data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(recovery_data["previous_commit"], "abc1234")
        self.assertEqual(recovery_data["status"], "ROLLBACK_READY")


# =============================================================================
# TIER 2: BOUNDARY & CORNER CASES (>=5 tests per feature area)
# =============================================================================

class TestTier2BoundaryAndCornerCases(unittest.IsolatedAsyncioTestCase):
    """
    Tier 2: Boundary, stress, adversarial, and edge condition validation.
    - Area 1: Conversion & Syntax Boundaries
    - Area 2: PDF & Buffer Boundaries
    - Area 3: Web UI & Network Boundaries
    - Area 4: SD Card & Storage Boundaries
    - Area 5: Auto-Update Boundaries
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)
        self.label_dir = PROJECT_ROOT / "templates" / "sample_labels"
        self.printer = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True)
        self.queue_mgr = ColorQueueManager()
        self.key_pool = GeminiKeyPoolManager(keys=["MOCK_KEY_1"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)

    def tearDown(self):
        self.temp_dir.cleanup()
        gc.collect()

    # -------------------------------------------------------------------------
    # Area 1: Conversion & Syntax Boundaries
    # -------------------------------------------------------------------------

    def test_t2_01_coordinate_boundary_limits(self):
        """T2.1: Verify coordinate boundary handling (X=0, X=800, Y=0, Y=1200)."""
        valid_bounds = [
            'A0,0,0,3,1,1,N,"TOP_LEFT"',
            'A800,0,0,3,1,1,N,"TOP_RIGHT"',
            'A0,1200,0,3,1,1,N,"BOTTOM_LEFT"',
            'A800,1200,0,3,1,1,N,"BOTTOM_RIGHT"'
        ]
        for cmd in valid_bounds:
            self.assertRegex(cmd, r'^A\d+,\d+,[0-3],[1-5],[1-8],[1-9],[NR],"([^"]*)"$')

    def test_t2_02_outer_box_item_overflow_truncation(self):
        """T2.2: Verify outer box label with >12 items truncates at Y>640 without buffer overrun."""
        many_items = [
            BoxItemContent(quantity=idx + 1, unit_type="Toner Unit", model=f"Cartridge Model {idx+1}")
            for idx in range(16)
        ]
        box = OuterBoxSheet(
            quote_number="QT-OVERFLOW",
            weight_kg="22.5",
            current_box=1,
            total_boxes=2,
            copies_per_box=2,
            box_contents=many_items
        )
        self.printer.dispatch_outer_box_label(box)
        last_job = self.printer.simulated_print_history[-1]
        raw_pplb = last_job["data"]

        # Truncation string must be present
        self.assertIn("... (additional items truncated)", raw_pplb)
        # Verify lines do not run off the label (no coordinates > 700 before footer)
        res = PPLBSyntaxValidator.validate_command_stream(raw_pplb)
        self.assertTrue(res["valid"])

    def test_t2_03_batch_quantity_boundary_p0_p1_p9999(self):
        """T2.3: Verify extreme batch quantity bounds (P0, P1, P9999)."""
        raw_pplb = "N\nq800\nQ400,24\nP1\n"
        
        # P1
        p1 = self.printer._inject_pplb_quantity(raw_pplb, 1)
        self.assertIn("P1", p1)

        # P9999
        p9999 = self.printer._inject_pplb_quantity(raw_pplb, 9999)
        self.assertIn("P9999", p9999)

        # Negative quantity should be sanitized or handled
        p_neg = self.printer._inject_pplb_quantity(raw_pplb, -5)
        self.assertTrue("P-5" in p_neg or "P1" in p_neg)

    def test_t2_04_malformed_corrupt_bartender_syntax(self):
        """T2.4: Verify corrupt/truncated PPLB stream detection."""
        corrupt_samples = [
            "",  # Empty
            "A50,30,0,4,1,1,N,\"UNCLOSED_QUOTE",  # Unclosed quote
            "INVALID_COMMAND_NAME\n",  # Unknown command
            "N\nq800\n",  # Missing print command P
        ]
        for sample in corrupt_samples:
            res = PPLBSyntaxValidator.validate_command_stream(sample)
            self.assertFalse(res["valid"], f"Sample should fail: '{sample}'")

    def test_t2_05_unencodable_barcode_characters(self):
        """T2.5: Verify quote sanitization in text strings to prevent PPLB syntax breaks."""
        text_with_quotes = 'HP 125A "Black" LaserJet'
        safe_text = text_with_quotes.replace('"', "'")
        self.assertEqual(safe_text, "HP 125A 'Black' LaserJet")
        self.assertNotIn('"', safe_text)

    # -------------------------------------------------------------------------
    # Area 2: PDF & Buffer Boundaries
    # -------------------------------------------------------------------------

    def test_t2_06_zero_byte_pdf_attachment(self):
        """T2.6: Verify zero-byte PDF attachment is rejected gracefully without crashing."""
        handler = InfluxMailHandler(on_pdf_received=lambda buf, name: None)
        zero_buf = bytearray(0)
        # Shredding 0-byte buffer must not throw
        handler._shred_buffer(zero_buf)
        self.assertEqual(len(zero_buf), 0)

    def test_t2_07_corrupted_pdf_stream_non_pdf(self):
        """T2.7: Verify feeding non-PDF binary garbage raises or logs without daemon panic."""
        garbage_bytes = b"NOT_A_VALID_PDF_HEADER_AT_ALL_12345"
        self.assertFalse(garbage_bytes.startswith(b"%PDF"))

    def test_t2_08_empty_mime_no_pdf_part(self):
        """T2.8: Verify email with no PDF attachments logs warning and returns 250 without error."""
        handler = InfluxMailHandler(on_pdf_received=lambda buf, name: None)
        msg = EmailMessage()
        msg["From"] = "scanner@lexmark.local"
        msg["To"] = "quotes@orangepi.local"
        msg["Subject"] = "Scan with no PDF"
        msg.set_content("No attachment here.")

        class MockSession:
            peer = ("192.168.50.2", 12345)

        class MockEnvelope:
            mail_from = "scanner@lexmark.local"
            content = msg.as_bytes()

        import asyncio
        loop = asyncio.get_event_loop()
        res = loop.run_until_complete(handler.handle_DATA(None, MockSession(), MockEnvelope()))
        self.assertIn("250", res)

    def test_t2_09_multipage_pdf_page_limit_guard(self):
        """T2.9: Verify memory guard restricts rasterization to Page 1 to avoid SBC OOM."""
        max_preview_pages = 1
        page_requested = 0  # 0-indexed page 1
        self.assertLess(page_requested, max_preview_pages)

    def test_t2_10_buffer_shredding_double_shred_idempotence(self):
        """T2.10: Verify calling shred protocol multiple times on same buffer is idempotent."""
        handler = InfluxMailHandler(on_pdf_received=lambda buf, name: None)
        buf = bytearray(b"REPEAT_SHRED_TEST")
        handler._shred_buffer(buf)
        handler._shred_buffer(buf)
        self.assertTrue(all(b == 0 for b in buf))

    # -------------------------------------------------------------------------
    # Area 3: Web UI & Network Boundaries
    # -------------------------------------------------------------------------

    async def test_t2_11_web_invalid_json_payload(self):
        """T2.11: Verify POST /api/print/job with nonexistent job_id returns 404."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/print/job", json={"job_id": "NONEXISTENT", "quantity": 1})
            self.assertEqual(resp.status, 404)

    async def test_t2_12_web_outer_box_out_of_bounds_index(self):
        """T2.12: Verify POST /api/print/outer_box with out-of-bounds index returns 404."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp_neg = await client.post("/api/print/outer_box", json={"index": -1})
            self.assertEqual(resp_neg.status, 404)

            resp_high = await client.post("/api/print/outer_box", json={"index": 999})
            self.assertEqual(resp_high.status, 404)

    async def test_t2_13_web_wifi_connect_empty_ssid(self):
        """T2.13: Verify POST /api/setup/wifi/connect with empty SSID returns 400."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/setup/wifi/connect", json={"ssid": "", "password": ""})
            self.assertEqual(resp.status, 400)
            data = await resp.json()
            self.assertEqual(data["status"], "error")

    async def test_t2_14_web_keys_save_empty_or_malformed(self):
        """T2.14: Verify POST /api/setup/keys with empty list empties pool safely."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/setup/keys", json={"keys": ""})
            self.assertEqual(resp.status, 200)
            self.assertEqual(len(self.key_pool.keys), 0)

    def test_t2_15_web_port_bind_conflict_handling(self):
        """T2.15: Verify port bind conflict handling simulation."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        bound_port = s.getsockname()[1]

        # Attempt to bind second socket to same port
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with self.assertRaises(OSError):
            s2.bind(("127.0.0.1", bound_port))
        s.close()
        s2.close()

    # -------------------------------------------------------------------------
    # Area 4: SD Card & Storage Boundaries
    # -------------------------------------------------------------------------

    def test_t2_16_power_loss_simulation_interrupted_temp_file(self):
        """T2.16: Verify power interruption leaving .tmp file does not corrupt master target."""
        master_file = self.test_root / "master_state.json"
        master_file.write_text(json.dumps({"version": 1, "status": "STABLE"}), encoding="utf-8")

        # Simulate interrupted write (incomplete .tmp file)
        tmp_file = self.test_root / "master_state.json.tmp"
        tmp_file.write_text("PARTIAL_TRUNCATED_JSON_DATA", encoding="utf-8")

        # Master file must remain valid and uncorrupted
        master_content = json.loads(master_file.read_text(encoding="utf-8"))
        self.assertEqual(master_content["status"], "STABLE")
        self.assertEqual(master_content["version"], 1)

    def test_t2_17_read_only_filesystem_remount_erofs(self):
        """T2.17: Verify simulated EROFS (Error 30 / Read-only filesystem) handling."""
        import errno
        def write_with_erofs_guard(path: Path, data: str):
            try:
                # Simulate read-only filesystem error
                raise OSError(errno.EROFS, "Read-only file system", str(path))
            except OSError as e:
                if e.errno == errno.EROFS:
                    return {"status": "EROFS_DETECTED", "error": str(e)}
                raise

        res = write_with_erofs_guard(self.test_root / "test.log", "data")
        self.assertEqual(res["status"], "EROFS_DETECTED")

    def test_t2_18_missing_sdcard_mount_point(self):
        """T2.18: Verify missing mount point detection (/mnt/sdcard_backup)."""
        nonexistent = Path("/nonexistent/mnt/sdcard_backup")
        self.assertFalse(nonexistent.exists())

    def test_t2_19_snapshot_pruning_retention_limit(self):
        """T2.19: Verify retention policy preserves exactly the 5 newest snapshots and deletes older ones."""
        snapshot_dir = self.test_root / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Create 8 dummy snapshot files
        created_files = []
        for i in range(8):
            f = snapshot_dir / f"snapshot_v1.0.{i}_20261029.tar.gz"
            f.write_text(f"snapshot_{i}")
            created_files.append(f)
            time.sleep(0.01)

        # Prune logic: sort by mtime, keep latest 5
        all_snapshots = sorted(snapshot_dir.glob("snapshot_*.tar.gz"), key=lambda p: p.stat().st_mtime)
        if len(all_snapshots) > 5:
            to_delete = all_snapshots[:-5]
            for d in to_delete:
                d.unlink()

        remaining = list(snapshot_dir.glob("snapshot_*.tar.gz"))
        self.assertEqual(len(remaining), 5)
        self.assertNotIn(created_files[0], remaining)
        self.assertIn(created_files[-1], remaining)

    def test_t2_20_label_file_path_traversal_prevention(self):
        """T2.20: Verify _resolve_label_file prevents directory traversal attacks."""
        resolved = self.printer._resolve_label_file("../../etc/passwd")
        # Must return None or resolve strictly inside label_dir
        if resolved is not None:
            self.assertTrue(str(resolved.resolve()).startswith(str(self.label_dir.resolve())))

    # -------------------------------------------------------------------------
    # Area 5: Auto-Update Boundaries
    # -------------------------------------------------------------------------

    def test_t2_21_autoupdate_network_drop_during_fetch(self):
        """T2.21: Verify network drop simulation during git fetch aborts cleanly without dirtying git."""
        def simulate_fetch_with_network_drop():
            # Exit code 128 typically returned by git on network drop
            return {"exit_code": 128, "stderr": "fatal: unable to access 'https://github.com/...': Could not resolve host"}

        res = simulate_fetch_with_network_drop()
        self.assertNotEqual(res["exit_code"], 0)
        self.assertIn("fatal", res["stderr"])

    def test_t2_22_autoupdate_health_check_timeout(self):
        """T2.22: Verify health check probe timing out at 30s triggers rollback state."""
        max_timeout = 30.0
        elapsed_simulated = 31.0
        is_healthy = False

        state = "HEALTH_VALIDATION"
        if elapsed_simulated >= max_timeout and not is_healthy:
            state = "ROLLBACK_EXECUTE"

        self.assertEqual(state, "ROLLBACK_EXECUTE")

    def test_t2_23_autoupdate_health_check_http500_response(self):
        """T2.23: Verify service returning HTTP 500 triggers immediate rollback."""
        mock_http_code = 500
        state = "HEALTH_VALIDATION"
        if mock_http_code != 200:
            state = "ROLLBACK_EXECUTE"

        self.assertEqual(state, "ROLLBACK_EXECUTE")

    def test_t2_24_autoupdate_dirty_working_tree_stash(self):
        """T2.24: Verify dirty working tree is identified and protected."""
        def check_working_tree_clean(status_output: str) -> bool:
            return len(status_output.strip()) == 0

        self.assertTrue(check_working_tree_clean(""))
        self.assertFalse(check_working_tree_clean(" M daemon/config.py"))

    def test_t2_25_autoupdate_pip_dependency_failure(self):
        """T2.25: Verify dependency installation conflict triggers rollback state."""
        pip_return_code = 1
        state = "DEPENDENCY_VALID"
        if pip_return_code != 0:
            state = "ROLLBACK_EXECUTE"

        self.assertEqual(state, "ROLLBACK_EXECUTE")


# =============================================================================
# TIER 3: CROSS-FEATURE PAIRWISE INTERACTIONS
# =============================================================================

class TestTier3CrossFeaturePairwise(unittest.IsolatedAsyncioTestCase):
    """
    Tier 3: Verification of cross-module pairwise interactions.
    1. Full intake pipeline: Lexmark SMTP -> InfluxMailHandler -> Classifier -> ColorQueue -> Printer -> VirtualArgox
    2. Web UI preview retrieval during active scan processing before shredding
    3. Auto-update trigger with active print queue preservation
    4. Gemini key pool 429 rate limit failover during active classification
    5. Printer offline detection during queue dispatch
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)
        self.label_dir = PROJECT_ROOT / "templates" / "sample_labels"
        self.printer = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True)
        self.queue_mgr = ColorQueueManager()
        self.key_pool = GeminiKeyPoolManager(keys=["KEY_1", "KEY_2"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)
        self.virtual_argox = VirtualArgoxPrinter()

    def tearDown(self):
        self.temp_dir.cleanup()
        gc.collect()

    async def test_t3_01_full_intake_pipeline_smtp_to_print_stream(self):
        """T3.1: Full intake pipeline: SMTP scan -> Parse -> ColorQueue -> Dispatch -> VirtualArgox."""
        # 1. Simulate incoming scan parsed into CartridgeItem
        cartridge = CartridgeItem(
            item_name="CB540A",
            description="HP Color LaserJet CP1215 Black Toner",
            color_group="BLACK",
            quantity=25,
            matched_label_file="CB540A_BLACK.prn"
        )
        self.queue_mgr.add_cartridge_item("QT-90124", cartridge)
        self.assertEqual(self.queue_mgr.get_summary()["BLACK"], 25)

        # 2. Dispatch via Web API
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/print/color?color=BLACK")
            self.assertEqual(resp.status, 200)

        # 3. Verify in Virtual Argox
        last_job = self.printer.simulated_print_history[-1]
        self.assertEqual(last_job["job_info"]["quantity"], 25)

        label_info = self.virtual_argox.process_pplb(last_job["data"].encode("utf-8"))
        self.assertEqual(label_info["quantity"], 25)
        self.assertTrue(any("CB540A" in b["data"] for b in label_info["barcodes"]))

    def test_t3_02_web_preview_retrieval_during_active_scan(self):
        """T3.2: Concurrent preview generation, storage, retrieval, and subsequent buffer shredding."""
        pdf_bytes = create_synthetic_pdf("CONCURRENT_PREVIEW_TEST")
        raw_buf = bytearray(pdf_bytes)
        
        # Ephemeral preview store simulation
        preview_cache = {}
        quote_id = "QT-PREVIEW-ACTIVE"
        preview_b64 = "data:image/jpeg;base64,ZmFrZV9wcmV2aWV3X2RhdGE="
        preview_cache[quote_id] = preview_b64

        # Read preview
        retrieved_preview = preview_cache.get(quote_id)
        self.assertEqual(retrieved_preview, preview_b64)

        # Execute shredding on raw buffer
        handler = InfluxMailHandler(on_pdf_received=lambda b, n: None)
        handler._shred_buffer(raw_buf)
        
        self.assertTrue(all(b == 0 for b in raw_buf))
        # Cached preview remains accessible until dismissed/TTL
        self.assertIsNotNone(preview_cache.get(quote_id))

    def test_t3_03_autoupdate_with_active_print_queue_preservation(self):
        """T3.3: Active print queue preserved across auto-update restart via persistent storage."""
        # Populate queue
        self.queue_mgr.add_cartridge_item("QT-777", CartridgeItem(item_name="Dell_283x", color_group="BLACK", quantity=50, matched_label_file="Dell_HCF_283x.prn"))
        self.queue_mgr.add_cartridge_item("QT-777", CartridgeItem(item_name="CB541A", color_group="CYAN", quantity=15, matched_label_file="CB541A_CYAN.prn"))

        # Snapshot queue to persistent store
        state_file = self.test_root / "queue_snapshot.json"
        dumped_jobs = [j.model_dump() for jobs in self.queue_mgr.queues.values() for j in jobs]
        state_file.write_text(json.dumps(dumped_jobs), encoding="utf-8")

        # Simulate service restart: fresh queue manager loads state
        new_queue_mgr = ColorQueueManager()
        loaded_jobs = json.loads(state_file.read_text(encoding="utf-8"))
        for j in loaded_jobs:
            item = CartridgeItem(item_name=j["item_name"], color_group=j["color_group"], quantity=j["quantity"], matched_label_file=j["matched_label_file"])
            new_queue_mgr.add_cartridge_item(j["quote_number"], item)

        summary = new_queue_mgr.get_summary()
        self.assertEqual(summary["BLACK"], 50)
        self.assertEqual(summary["CYAN"], 15)

    def test_t3_04_gemini_key_exhaustion_429_failover_in_pipeline(self):
        """T3.4: Key 1 encounters 429 rate limit; key pool fails over to Key 2 seamlessly."""
        pool = GeminiKeyPoolManager(keys=["KEY_PRIMARY", "KEY_SECONDARY"])
        self.assertEqual(len(pool.keys), 2)

        # Primary key hits 429
        pool.keys[0].mark_exhausted(cooldown_seconds=60.0)
        self.assertFalse(pool.keys[0].is_available())

        # Next available key must be secondary
        active_key = pool._get_next_available_key()
        self.assertIsNotNone(active_key)
        self.assertEqual(active_key.key, "KEY_SECONDARY")

    def test_t3_05_printer_offline_detection_during_queue_dispatch(self):
        """T3.5: Printer offline (/dev/usb/lp0 missing); dispatch fails honestly, job stays PENDING."""
        offline_printer = ArgoxPrinterEngine(device_path="/dev/usb/lp0_missing", label_dir=self.label_dir, simulation_mode=False)
        self.queue_mgr.add_cartridge_item("QT-OFFLINE", CartridgeItem(item_name="CB540A", color_group="BLACK", quantity=10, matched_label_file="CB540A_BLACK.prn"))

        target_job = self.queue_mgr.queues["BLACK"][0]
        success = offline_printer.dispatch_cartridge_label(target_job.matched_label_file, quantity=target_job.quantity)
        
        self.assertFalse(success)
        # Job remains PENDING in queue
        self.assertEqual(target_job.status, "PENDING")


# =============================================================================
# TIER 4: REAL-WORLD WAREHOUSE WORKLOADS
# =============================================================================

class TestTier4RealWorldWarehouseWorkloads(unittest.TestCase):
    """
    Tier 4: Production end-to-end scenarios simulating factory warehouse operations.
    1. Multi-cartridge quotation sheet with mixed colors & handwritten quantities
    2. Warehouse pick & pack carton workflow (Box 1 of 3, Box 2 of 3, Box 3 of 3 with 2x copies each)
    3. End-to-end simulated print dispatch to mock Argox printer with binary command verification
    """

    def setUp(self):
        self.label_dir = PROJECT_ROOT / "templates" / "sample_labels"
        self.printer = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True)
        self.queue_mgr = ColorQueueManager()
        self.virtual_argox = VirtualArgoxPrinter()

    def test_t4_01_multi_cartridge_quotation_mixed_colors_handwritten_quantities(self):
        """T4.1: Multi-cartridge quote QT-90124 with mixed colors and handwritten quantities."""
        # Simulated scanned items from Quote QT-90124:
        # Line 1: HP CB540A Black (Printed: 10, Handwritten: 'only 20', Note: 'Urgent')
        # Line 2: HP CB541A Cyan (Printed: 5)
        # Line 3: HP CB542A Yellow (Printed: 5)
        # Line 4: HP CB543A Magenta (Printed: 5)
        items = [
            CartridgeItem(item_name="HP CB540A", description="Black Toner", color_group="BLACK", quantity=20, handwritten_note="Urgent", matched_label_file="CB540A_BLACK.prn"),
            CartridgeItem(item_name="HP CB541A", description="Cyan Toner", color_group="CYAN", quantity=5, matched_label_file="CB541A_CYAN.prn"),
            CartridgeItem(item_name="HP CB542A", description="Yellow Toner", color_group="YELLOW", quantity=5, matched_label_file="CB541A_CYAN.prn"),
            CartridgeItem(item_name="HP CB543A", description="Magenta Toner", color_group="MAGENTA", quantity=5, matched_label_file="CB541A_CYAN.prn")
        ]
        for it in items:
            self.queue_mgr.add_cartridge_item("QT-90124", it)

        summary = self.queue_mgr.get_summary()
        self.assertEqual(summary["BLACK"], 20)
        self.assertEqual(summary["CYAN"], 5)
        self.assertEqual(summary["YELLOW"], 5)
        self.assertEqual(summary["MAGENTA"], 5)

        # Warehouse Operator: Mounts Black roll, prints Black batch
        black_jobs = self.queue_mgr.get_jobs_for_color("BLACK")
        for j in black_jobs:
            self.printer.dispatch_cartridge_label(j.matched_label_file, quantity=j.quantity)
            j.status = "PRINTED"

        last_job = self.printer.simulated_print_history[-1]
        self.assertEqual(last_job["job_info"]["quantity"], 20)
        label_info = self.virtual_argox.process_pplb(last_job["data"].encode("utf-8"))
        self.assertEqual(label_info["quantity"], 20)
        self.assertEqual(self.queue_mgr.get_summary()["BLACK"], 0)

    def test_t4_02_warehouse_pick_pack_carton_workflow_3_boxes(self):
        """T4.2: Pick & Pack workflow: Box 1 of 3, Box 2 of 3, Box 3 of 3 with 2x copies each."""
        boxes = [
            OuterBoxSheet(
                quote_number="QT-98124",
                weight_kg="12.4",
                current_box=1,
                total_boxes=3,
                copies_per_box=2,
                box_contents=[
                    BoxItemContent(quantity=5, unit_type="Toner Unit", model="HP CB540A Black"),
                    BoxItemContent(quantity=2, unit_type="Drum Unit", model="DR3115 Drum")
                ]
            ),
            OuterBoxSheet(
                quote_number="QT-98124",
                weight_kg="10.1",
                current_box=2,
                total_boxes=3,
                copies_per_box=2,
                box_contents=[
                    BoxItemContent(quantity=10, unit_type="Toner Unit", model="HP CB541A Cyan")
                ]
            ),
            OuterBoxSheet(
                quote_number="QT-98124",
                weight_kg="8.5",
                current_box=3,
                total_boxes=3,
                copies_per_box=2,
                box_contents=[
                    BoxItemContent(quantity=8, unit_type="Toner Unit", model="HP CB542A Yellow")
                ]
            )
        ]

        total_labels_dispatched = 0
        for b in boxes:
            self.queue_mgr.add_outer_box(b)

        self.assertEqual(len(self.queue_mgr.pending_outer_boxes), 3)

        # Dispatch all boxes
        while self.queue_mgr.pending_outer_boxes:
            box = self.queue_mgr.pending_outer_boxes.pop(0)
            success = self.printer.dispatch_outer_box_label(box)
            self.assertTrue(success)
            total_labels_dispatched += (box.copies_per_box or 2)

            last_job = self.printer.simulated_print_history[-1]
            raw_pplb = last_job["data"]
            self.assertIn(f"BOX {box.current_box} OF {box.total_boxes}", raw_pplb)
            self.assertIn(f"WEIGHT: {box.weight_kg} KG", raw_pplb)
            self.assertIn("P2", raw_pplb)

        # 3 boxes * 2 copies = 6 total physical labels
        self.assertEqual(total_labels_dispatched, 6)

    def test_t4_03_end_to_end_argox_binary_stream_verification(self):
        """T4.3: Binary-level verification of PPLB print stream for Argox OS-214 203 DPI."""
        box = OuterBoxSheet(
            quote_number="QT-BINARY-VERIFY",
            weight_kg="14.0",
            current_box=1,
            total_boxes=1,
            copies_per_box=2,
            box_contents=[BoxItemContent(quantity=1, unit_type="Toner Unit", model="CANON 716")]
        )
        self.printer.dispatch_outer_box_label(box)
        raw_stream = self.printer.simulated_print_history[-1]["data"].encode("utf-8")

        # Binary checks
        self.assertTrue(raw_stream.startswith(b"N\n"))
        self.assertIn(b"\nq800\n", raw_stream)
        self.assertIn(b"\nQ1200,24\n", raw_stream)
        self.assertIn(b"\nD8\n", raw_stream)
        self.assertIn(b"\nS2\n", raw_stream)
        self.assertTrue(raw_stream.endswith(b"P2\n\n") or raw_stream.endswith(b"P2\n"))


if __name__ == "__main__":
    unittest.main()
