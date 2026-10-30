"""
Unit Test Suite: In-Memory PDF Rasterization, Ephemeral Store, Shredding & Preview APIs.

Requirements: R2 (Web UI Previews & Enterprise Industrial Interface)
Integrity Standard: Strict Industrial Enterprise Layout, Zero Emojis, Zero Fake Readings.
"""

import io
import gc
import sys
import time
import base64
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from daemon import pdf_preview
from daemon.config import config
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.color_queue import ColorQueueManager
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.web_ui import WebDashboard
from simulator.mock_lexmark import create_synthetic_pdf


class TestInMemoryPDFRasterization(unittest.TestCase):
    """Verifies in-memory PDF page rasterization without touching flash storage."""

    def setUp(self):
        self.sample_pdf = create_synthetic_pdf("IN_MEMORY_RASTER_TEST")

    def test_render_pdf_page_preview_success(self):
        """Renders page 0 of a valid synthetic PDF directly in RAM to base64 data URI."""
        b64_res = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=150)
        self.assertIsInstance(b64_res, str)
        self.assertTrue(b64_res.startswith("data:image/jpeg;base64,"))
        self.assertGreater(len(b64_res), 500)

    def test_render_pdf_page_preview_data_uri_format(self):
        """Verifies the returned base64 decodes cleanly to valid JPEG magic header."""
        b64_res = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0)
        raw_b64 = b64_res.split(",", 1)[1]
        img_bytes = base64.b64decode(raw_b64)
        
        # JPEG SOI (Start of Image) marker: \xff\xd8\xff
        self.assertEqual(img_bytes[:2], b"\xff\xd8")

    def test_render_pdf_page_preview_custom_dpi(self):
        """Verifies custom DPI settings (72 DPI vs 150 DPI) scale output cleanly."""
        b64_72 = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=72)
        b64_150 = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=150)
        
        # 150 DPI rendering should yield a larger byte payload than 72 DPI
        self.assertGreater(len(b64_150), len(b64_72))

    def test_render_pdf_page_preview_page_index_bounds(self):
        """Verifies page index out of bounds raises IndexError."""
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=999)
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=-1)

    def test_render_pdf_page_preview_empty_bytes(self):
        """Verifies passing empty bytes raises ValueError."""
        with self.assertRaises(ValueError):
            pdf_preview.render_pdf_page_preview(b"")

    def test_render_pdf_page_preview_corrupt_stream(self):
        """Verifies passing corrupt non-PDF binary data raises ValueError."""
        with self.assertRaises(ValueError):
            pdf_preview.render_pdf_page_preview(b"THIS_IS_NOT_A_VALID_PDF_STREAM_DATA")

    def test_zero_disk_writes_during_rasterization(self):
        """Verifies that rendering PDF preview writes zero files to the filesystem."""
        before_files = set(PROJECT_ROOT.glob("**/*.jpg")) | set(PROJECT_ROOT.glob("**/*.jpeg")) | set(PROJECT_ROOT.glob("**/*.png"))
        
        # Execute rasterization
        _ = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=150)
        
        after_files = set(PROJECT_ROOT.glob("**/*.jpg")) | set(PROJECT_ROOT.glob("**/*.jpeg")) | set(PROJECT_ROOT.glob("**/*.png"))
        self.assertEqual(before_files, after_files, "Rasterizer must not write any image files to disk.")


class TestEphemeralPreviewStore(unittest.TestCase):
    """Verifies volatile RAM storage, TTL expiration, listing, and manual dismissal."""

    def setUp(self):
        pdf_preview.clear_all_previews()
        self.fake_b64 = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/"

    def tearDown(self):
        pdf_preview.clear_all_previews()

    def test_store_and_retrieve_preview(self):
        """Verifies storing and retrieving an ephemeral preview from memory."""
        pdf_preview.store_ephemeral_preview("QT-90124", self.fake_b64, ttl_seconds=600)
        retrieved = pdf_preview.get_ephemeral_preview("QT-90124")
        self.assertEqual(retrieved, self.fake_b64)

    def test_get_nonexistent_preview_returns_none(self):
        """Verifies query for unknown quote ID returns None."""
        self.assertIsNone(pdf_preview.get_ephemeral_preview("QT-DOES-NOT-EXIST"))

    def test_ttl_expiration(self):
        """Verifies previews expire and are purged when TTL elapses."""
        pdf_preview.store_ephemeral_preview("QT-SHORT-LIVED", self.fake_b64, ttl_seconds=-1)
        self.assertIsNone(pdf_preview.get_ephemeral_preview("QT-SHORT-LIVED"))

    def test_list_active_previews(self):
        """Verifies listing active previews returns dictionary metadata without full image payloads."""
        pdf_preview.store_ephemeral_preview("QT-AAA", self.fake_b64, ttl_seconds=300)
        pdf_preview.store_ephemeral_preview("QT-BBB", self.fake_b64, ttl_seconds=300)

        previews = pdf_preview.list_active_previews()
        self.assertEqual(len(previews), 2)
        quote_ids = [p["quote_id"] for p in previews]
        self.assertIn("QT-AAA", quote_ids)
        self.assertIn("QT-BBB", quote_ids)
        self.assertTrue(previews[0]["has_preview"])
        self.assertIn("expires_at", previews[0])

    def test_dismiss_preview(self):
        """Verifies dismissal purges the preview from RAM and returns correct boolean status."""
        pdf_preview.store_ephemeral_preview("QT-PURGE-ME", self.fake_b64, ttl_seconds=600)
        self.assertIsNotNone(pdf_preview.get_ephemeral_preview("QT-PURGE-ME"))

        dismissed = pdf_preview.dismiss_preview("QT-PURGE-ME")
        self.assertTrue(dismissed)
        self.assertIsNone(pdf_preview.get_ephemeral_preview("QT-PURGE-ME"))

        # Second dismissal should return False
        self.assertFalse(pdf_preview.dismiss_preview("QT-PURGE-ME"))

    def test_case_insensitive_quote_lookup(self):
        """Verifies quote lookups match case-insensitively."""
        pdf_preview.store_ephemeral_preview("QT-CASE-TEST", self.fake_b64, ttl_seconds=600)
        self.assertEqual(pdf_preview.get_ephemeral_preview("qt-case-test"), self.fake_b64)


class TestConfidentialShredding(unittest.TestCase):
    """Verifies confidential memory buffer zero-overwrite shredding and GC collection."""

    def test_shred_memory_buffer_zeroes_all_bytes(self):
        """Verifies 100% of bytes in mutable bytearray are overwritten with 0."""
        secret = b"CONFIDENTIAL_PRICE_AND_CUSTOMER_TAX_ID_987654321"
        buf = bytearray(secret)
        self.assertNotEqual(buf[0], 0)

        pdf_preview.shred_memory_buffer(buf)

        self.assertEqual(len(buf), len(secret))
        self.assertTrue(all(b == 0 for b in buf))

    def test_shred_memory_buffer_idempotent(self):
        """Verifies calling shred_memory_buffer multiple times is safe and idempotent."""
        buf = bytearray(b"REPEAT_TEST_DATA")
        pdf_preview.shred_memory_buffer(buf)
        pdf_preview.shred_memory_buffer(buf)
        self.assertTrue(all(b == 0 for b in buf))

    def test_shred_memory_buffer_zero_length(self):
        """Verifies shredding an empty bytearray does not raise an exception."""
        buf = bytearray(0)
        pdf_preview.shred_memory_buffer(buf)
        self.assertEqual(len(buf), 0)

    def test_shred_memory_buffer_none_input(self):
        """Verifies passing None handles gracefully without crashing."""
        pdf_preview.shred_memory_buffer(None)


class TestWebPreviewEndpoints(unittest.IsolatedAsyncioTestCase):
    """Verifies REST API preview endpoints on port 8080."""

    def setUp(self):
        pdf_preview.clear_all_previews()
        self.queue_mgr = ColorQueueManager()
        self.printer = ArgoxPrinterEngine(simulation_mode=True)
        self.key_pool = GeminiKeyPoolManager(keys=["AIzaSyTEST_KEY_1234567890abcdef"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)
        self.fake_b64 = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/"

    def tearDown(self):
        pdf_preview.clear_all_previews()

    async def test_api_get_previews_empty(self):
        """Verifies GET /api/previews returns empty list when no scans in RAM."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/previews")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["previews"], [])
            self.assertEqual(data["count"], 0)

    async def test_api_get_previews_populated(self):
        """Verifies GET /api/previews returns active scan metadata."""
        pdf_preview.store_ephemeral_preview("QT-1001", self.fake_b64, ttl_seconds=300)
        pdf_preview.store_ephemeral_preview("QT-1002", self.fake_b64, ttl_seconds=300)

        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/previews")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["count"], 2)
            self.assertEqual(len(data["previews"]), 2)

    async def test_api_get_preview_item_json(self):
        """Verifies GET /api/preview/{quote_id} returns JSON preview payload."""
        pdf_preview.store_ephemeral_preview("QT-2001", self.fake_b64, ttl_seconds=300)

        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/preview/QT-2001")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["quote_id"], "QT-2001")
            self.assertEqual(data["preview"], self.fake_b64)
            self.assertTrue(data["has_preview"])

    async def test_api_get_preview_item_raw_image(self):
        """Verifies GET /api/preview/{quote_id}?raw=1 returns binary image/jpeg stream."""
        pdf_preview.store_ephemeral_preview("QT-RAW-TEST", self.fake_b64, ttl_seconds=300)

        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/preview/QT-RAW-TEST?raw=1")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.content_type, "image/jpeg")
            body = await resp.read()
            self.assertEqual(body[:2], b"\xff\xd8")

    async def test_api_get_preview_not_found(self):
        """Verifies GET /api/preview/NONEXISTENT returns 404."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/preview/NONEXISTENT")
            self.assertEqual(resp.status, 404)
            data = await resp.json()
            self.assertEqual(data["status"], "error")

    async def test_api_dismiss_preview_success(self):
        """Verifies POST /api/preview/{quote_id}/dismiss purges preview and returns 200."""
        pdf_preview.store_ephemeral_preview("QT-DISMISS", self.fake_b64, ttl_seconds=300)

        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/preview/QT-DISMISS/dismiss")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["quote_id"], "QT-DISMISS")

            # Subsequent fetch returns 404
            get_resp = await client.get("/api/preview/QT-DISMISS")
            self.assertEqual(get_resp.status, 404)

    async def test_api_dismiss_preview_not_found(self):
        """Verifies POST /api/preview/NONEXISTENT/dismiss returns 404."""
        from aiohttp.test_utils import TestClient, TestServer
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/preview/NONEXISTENT/dismiss")
            self.assertEqual(resp.status, 404)


class TestDynamicNetworkCarrier(unittest.TestCase):
    """Verifies dynamic carrier detection across eth0 and end0 sysfs paths."""

    def setUp(self):
        self.queue_mgr = ColorQueueManager()
        self.printer = ArgoxPrinterEngine(simulation_mode=True)
        self.key_pool = GeminiKeyPoolManager(keys=["AIzaSyTEST_KEY_1234567890abcdef"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)

    @patch("daemon.web_ui.Path")
    def test_get_scanner_interface_carrier_eth0(self, mock_path_cls):
        """Verifies carrier is detected when eth0 reports carrier=1."""
        def fake_path(p_str):
            m = MagicMock()
            if "eth0/carrier" in str(p_str).replace("\\", "/"):
                m.exists.return_value = True
                m.read_text.return_value = "1\n"
            else:
                m.exists.return_value = False
            return m

        mock_path_cls.side_effect = fake_path
        carrier = self.dashboard.get_scanner_interface_carrier()
        self.assertTrue(carrier)

    @patch("daemon.web_ui.Path")
    def test_get_scanner_interface_carrier_end0(self, mock_path_cls):
        """Verifies carrier is detected when end0 reports carrier=1 (Orange Pi Allwinner H3)."""
        def fake_path(p_str):
            m = MagicMock()
            if "end0/carrier" in str(p_str).replace("\\", "/"):
                m.exists.return_value = True
                m.read_text.return_value = "1\n"
            else:
                m.exists.return_value = False
            return m

        mock_path_cls.side_effect = fake_path
        carrier = self.dashboard.get_scanner_interface_carrier()
        self.assertTrue(carrier)

    @patch("daemon.web_ui.Path")
    def test_get_scanner_interface_carrier_disconnected(self, mock_path_cls):
        """Verifies carrier returns False when interface is disconnected."""
        m = MagicMock()
        m.exists.return_value = False
        mock_path_cls.return_value = m
        carrier = self.dashboard.get_scanner_interface_carrier()
        self.assertFalse(carrier)


class TestHonestHardwareTelemetry(unittest.TestCase):
    """Verifies honest reporting of hardware presence without fake masks."""

    def setUp(self):
        self.queue_mgr = ColorQueueManager()
        self.key_pool = GeminiKeyPoolManager(keys=["AIzaSyKEY1_1234567890abcd", "AIzaSyKEY2_1234567890abcd"])

    def test_telemetry_simulation_mode(self):
        """Verifies simulation mode is explicitly declared in hardware telemetry."""
        sim_printer = ArgoxPrinterEngine(device_path="/dev/usb/lp0", simulation_mode=True)
        dashboard = WebDashboard(self.queue_mgr, sim_printer, self.key_pool)
        hw = dashboard.get_hardware_status()

        self.assertTrue(hw["printer"]["simulation"])
        self.assertEqual(hw["printer"]["status_text"], "[SIMULATION - HARDWARE BYPASSED]")

    def test_telemetry_real_hardware_offline(self):
        """Verifies disconnected physical printer reports disconnected with missing path."""
        real_printer = ArgoxPrinterEngine(device_path="/dev/nonexistent_device_node_999", simulation_mode=False)
        dashboard = WebDashboard(self.queue_mgr, real_printer, self.key_pool)
        hw = dashboard.get_hardware_status()

        self.assertFalse(hw["printer"]["simulation"])
        self.assertFalse(hw["printer"]["connected"])
        self.assertIn("DISCONNECTED", hw["printer"]["status_text"])
        self.assertIn("/dev/nonexistent_device_node_999", hw["printer"]["status_text"])

    def test_telemetry_key_pool_counts(self):
        """Verifies active key pool counts match actual pool state."""
        dashboard = WebDashboard(self.queue_mgr, ArgoxPrinterEngine(simulation_mode=True), self.key_pool)
        hw = dashboard.get_hardware_status()

        self.assertEqual(hw["gemini"]["total_keys"], 2)
        self.assertEqual(hw["gemini"]["active_keys"], 2)


if __name__ == "__main__":
    unittest.main()
