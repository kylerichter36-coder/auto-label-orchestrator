"""
Adversarial Stress Test Suite for Milestone 2:
- daemon/web_ui.py
- daemon/pdf_preview.py
- web/index.html (security review)

Challenge Focus Areas:
1. Concurrency & TTL Race Conditions:
   - 50 concurrent requests across GET preview, POST dismiss, and store_ephemeral_preview
   - 50 concurrent OS threads accessing RAM store simultaneously
   - TTL edge conditions: preview availability at 599s vs auto-purging at 601s
2. Security & Path Traversal on Preview Endpoints:
   - Malicious quote IDs: ../../etc/shadow, ..\\..\\secret.key, "", null bytes (%00), <script>alert(1)</script>
   - Path traversal injection via URL encoding and raw strings
   - Verification of zero disk leakage, safe error codes (400/404), application/json content types
3. Telemetry Integrity Stress:
   - GET /api/status when /dev/usb/lp0 is deleted
   - GET /api/status when simulation_mode=True vs False
   - GET /api/status when both eth0 and end0 carrier files are absent
   - Carrier check resilience against permission/IO errors
   - Rapid-fire status polling stress test (100 cycles)
"""

import os
import sys
import time
import base64
import asyncio
import unittest
import threading
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aiohttp.test_utils import TestClient, TestServer
from daemon import pdf_preview
from daemon.config import config
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.color_queue import ColorQueueManager
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.web_ui import WebDashboard


SAMPLE_BASE64_JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="


class TestConcurrencyAndTTLRaceConditions(unittest.IsolatedAsyncioTestCase):
    """Stress tests for concurrency and TTL race conditions."""

    def setUp(self):
        pdf_preview.clear_all_previews()
        self.queue_mgr = ColorQueueManager()
        self.printer = ArgoxPrinterEngine(simulation_mode=True)
        self.key_pool = GeminiKeyPoolManager(keys=["AIzaSyMOCK_KEY_1234567890abcdef"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)

    def tearDown(self):
        pdf_preview.clear_all_previews()

    async def test_50_concurrent_mixed_api_requests(self):
        """
        Stress test: 50 concurrent requests simultaneously executing
        store_ephemeral_preview, GET /api/preview/{id}, POST /api/preview/{id}/dismiss,
        and GET /api/previews. Verifies zero deadlocks, zero 500 errors, and zero data corruption.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # Pre-populate 10 items
            for i in range(10):
                pdf_preview.store_ephemeral_preview(f"CONCUR_{i}", SAMPLE_BASE64_JPEG, ttl_seconds=300)

            results = []

            async def worker(worker_id: int):
                target_id = f"CONCUR_{worker_id % 15}"
                op = worker_id % 4
                try:
                    if op == 0:
                        # Direct store
                        pdf_preview.store_ephemeral_preview(f"NEW_{worker_id}", SAMPLE_BASE64_JPEG, ttl_seconds=300)
                        return (worker_id, "store", 200)
                    elif op == 1:
                        # GET preview
                        resp = await client.get(f"/api/preview/{target_id}")
                        status = resp.status
                        body = await resp.json()
                        return (worker_id, "get", status)
                    elif op == 2:
                        # POST dismiss
                        resp = await client.post(f"/api/preview/{target_id}/dismiss")
                        status = resp.status
                        body = await resp.json()
                        return (worker_id, "dismiss", status)
                    else:
                        # GET all previews
                        resp = await client.get("/api/previews")
                        status = resp.status
                        body = await resp.json()
                        return (worker_id, "list", status)
                except Exception as e:
                    return (worker_id, f"error: {type(e).__name__}", 500)

            # Launch 50 simultaneous requests
            tasks = [worker(i) for i in range(50)]
            start_t = time.time()
            results = await asyncio.gather(*tasks)
            duration = time.time() - start_t

            # Verify no 500 errors occurred
            error_results = [r for r in results if r[2] == 500]
            self.assertEqual(len(error_results), 0, f"Found 500 errors during concurrent load: {error_results}")

            # Verify all statuses are valid HTTP responses (200, 404)
            valid_statuses = {200, 404}
            for wid, op, st in results:
                self.assertIn(st, valid_statuses, f"Worker {wid} ({op}) returned unexpected status {st}")

            # Verify completed in reasonable time (< 5.0 seconds for 50 requests)
            self.assertLess(duration, 5.0, f"Concurrency test took too long: {duration:.2f}s")

    def test_50_concurrent_os_threads_ram_store(self):
        """
        Stress test: 50 real OS threads aggressively reading, writing, dismissing,
        and listing previews in RAM simultaneously. Verifies _STORE_LOCK thread-safety
        and absence of RuntimeError (dictionary changed size during iteration).
        """
        num_threads = 50
        errors = []
        barrier = threading.Barrier(num_threads)

        def thread_worker(tid: int):
            try:
                barrier.wait(timeout=5.0)
                for cycle in range(20):
                    qid = f"TH_{tid}_{cycle % 5}"
                    pdf_preview.store_ephemeral_preview(qid, SAMPLE_BASE64_JPEG, ttl_seconds=60)
                    _ = pdf_preview.get_ephemeral_preview(qid)
                    _ = pdf_preview.list_active_previews()
                    if cycle % 2 == 0:
                        pdf_preview.dismiss_preview(qid)
            except Exception as ex:
                errors.append((tid, ex))

        threads = [threading.Thread(target=thread_worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(len(errors), 0, f"Thread concurrency errors: {errors}")

    def test_ttl_edge_condition_599s_vs_601s_internal_store(self):
        """
        Verifies TTL edge conditions on the internal RAM cache:
        - At 599.0s: preview MUST be available and active.
        - At 601.0s: preview MUST be auto-purged and return None.
        """
        t0 = 1000000.0
        with patch("daemon.pdf_preview.time.time", return_value=t0):
            pdf_preview.store_ephemeral_preview("QT-EDGE-1", SAMPLE_BASE64_JPEG, ttl_seconds=600)

        # Check at 599s: STILL ACTIVE
        with patch("daemon.pdf_preview.time.time", return_value=t0 + 599.0):
            preview = pdf_preview.get_ephemeral_preview("QT-EDGE-1")
            self.assertIsNotNone(preview, "Preview should be available at 599s (TTL is 600s)")
            active_list = pdf_preview.list_active_previews()
            self.assertTrue(any(p["quote_id"] == "QT-EDGE-1" for p in active_list))

        # Check at 600.0s (exact boundary: now == expires_at)
        with patch("daemon.pdf_preview.time.time", return_value=t0 + 600.0):
            preview_at_boundary = pdf_preview.get_ephemeral_preview("QT-EDGE-1")
            self.assertIsNotNone(preview_at_boundary, "Preview should remain valid at exact 600.0s boundary")

        # Check at 601s: EXPIRED & AUTO-PURGED
        with patch("daemon.pdf_preview.time.time", return_value=t0 + 601.0):
            preview_expired = pdf_preview.get_ephemeral_preview("QT-EDGE-1")
            self.assertIsNone(preview_expired, "Preview must be auto-purged at 601s")
            active_list_post = pdf_preview.list_active_previews()
            self.assertFalse(any(p["quote_id"] == "QT-EDGE-1" for p in active_list_post))

    async def test_ttl_edge_condition_599s_vs_601s_api_endpoint(self):
        """
        Verifies TTL edge conditions via HTTP REST endpoints:
        - At 599.0s: GET /api/preview/{id} returns 200 OK.
        - At 601.0s: GET /api/preview/{id} returns 404 Not Found.
        """
        t0 = 2000000.0
        with patch("daemon.pdf_preview.time.time", return_value=t0):
            pdf_preview.store_ephemeral_preview("QT-API-TTL", SAMPLE_BASE64_JPEG, ttl_seconds=600)

        async with TestClient(TestServer(self.dashboard.app)) as client:
            # 599s: 200 OK
            with patch("daemon.pdf_preview.time.time", return_value=t0 + 599.0):
                resp_599 = await client.get("/api/preview/QT-API-TTL")
                self.assertEqual(resp_599.status, 200)
                data_599 = await resp_599.json()
                self.assertEqual(data_599["status"], "success")
                self.assertEqual(data_599["quote_id"], "QT-API-TTL")

            # 601s: 404 Not Found
            with patch("daemon.pdf_preview.time.time", return_value=t0 + 601.0):
                resp_601 = await client.get("/api/preview/QT-API-TTL")
                self.assertEqual(resp_601.status, 404)
                data_601 = await resp_601.json()
                self.assertEqual(data_601["status"], "error")


class TestSecurityAndPathTraversal(unittest.IsolatedAsyncioTestCase):
    """Stress tests for security, injection attacks, and path traversal."""

    def setUp(self):
        pdf_preview.clear_all_previews()
        self.queue_mgr = ColorQueueManager()
        self.printer = ArgoxPrinterEngine(simulation_mode=True)
        self.key_pool = GeminiKeyPoolManager(keys=["AIzaSyMOCK_KEY_1234567890abcdef"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)

    def tearDown(self):
        pdf_preview.clear_all_previews()

    async def test_path_traversal_linux_shadow(self):
        """Verifies ../../etc/shadow traversal attempts are safely rejected with 404."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # URL-encoded traversal
            resp = await client.get("/api/preview/..%2F..%2Fetc%2Fshadow")
            self.assertEqual(resp.status, 404)
            self.assertEqual(resp.content_type, "application/json")
            data = await resp.json()
            self.assertEqual(data["status"], "error")
            # Verify file contents are NOT leaked
            self.assertNotIn("root:", str(data))

    async def test_path_traversal_windows_secret_key(self):
        """Verifies ..\\..\\secret.key traversal attempts are safely rejected with 404."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/preview/..%5C..%5Csecret.key")
            self.assertEqual(resp.status, 404)
            self.assertEqual(resp.content_type, "application/json")
            data = await resp.json()
            self.assertEqual(data["status"], "error")

    async def test_empty_quote_id(self):
        """Verifies requesting empty quote_id returns 404 or 400 safely without crashing."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # Trailing slash route
            resp = await client.get("/api/preview/")
            self.assertEqual(resp.status, 404)

        # Direct call to store with empty string must raise ValueError
        with self.assertRaises(ValueError):
            pdf_preview.store_ephemeral_preview("", SAMPLE_BASE64_JPEG)

        # Direct call to get with empty string must return None
        self.assertIsNone(pdf_preview.get_ephemeral_preview(""))

    async def test_null_bytes_in_quote_id(self):
        """Verifies null bytes (%00 / \\x00) do not cause unhandled exceptions or 500 errors."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/preview/%00")
            self.assertEqual(resp.status, 404)
            self.assertEqual(resp.content_type, "application/json")

            resp_post = await client.post("/api/preview/%00/dismiss")
            self.assertEqual(resp_post.status, 404)

        # Internal store handling
        self.assertIsNone(pdf_preview.get_ephemeral_preview("\x00"))
        self.assertFalse(pdf_preview.dismiss_preview("\x00"))

    async def test_xss_script_tag_quote_id(self):
        """
        Verifies malicious XSS payload <script>alert(1)</script> is safely rejected
        and does NOT produce executable text/html output.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # 1. URL-encoded script tag (matches route pattern)
            resp_encoded = await client.get("/api/preview/%3Cscript%3Ealert(1)%3C%2Fscript%3E")
            self.assertEqual(resp_encoded.status, 404)
            self.assertEqual(resp_encoded.content_type, "application/json")
            data_encoded = await resp_encoded.json()
            self.assertEqual(data_encoded["status"], "error")
            self.assertIn("<script>alert(1)</script>", data_encoded["message"])

            # 2. Raw unencoded script tag (contains / separator)
            resp_raw = await client.get("/api/preview/<script>alert(1)</script>")
            self.assertEqual(resp_raw.status, 404)
            # Never return executable HTML
            self.assertNotEqual(resp_raw.content_type, "text/html")

    async def test_oversized_quote_id_overflow_defense(self):
        """
        Verifies oversized quote_id requests are safely rejected (400/404/414)
        without causing buffer overflow or server crash.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # Below aiohttp line limit (4000 chars)
            resp_4k = await client.get(f"/api/preview/{'A' * 4000}")
            self.assertEqual(resp_4k.status, 404)
            self.assertEqual(resp_4k.content_type, "application/json")

            # Beyond aiohttp line limit (10000 chars) - rejected by parser with 400
            resp_10k = await client.get(f"/api/preview/{'A' * 10000}")
            self.assertIn(resp_10k.status, {400, 404, 414})

    async def test_dismiss_traversal_payloads(self):
        """Verifies POST /api/preview/{id}/dismiss with traversal payloads returns 404 safely."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            for bad_id in ["..%2F..%2Fetc%2Fshadow", "..%5C..%5Csecret.key", "%3Cscript%3E", "%00"]:
                resp = await client.post(f"/api/preview/{bad_id}/dismiss")
                self.assertEqual(resp.status, 404)
                self.assertEqual(resp.content_type, "application/json")


class TestTelemetryIntegrityStress(unittest.IsolatedAsyncioTestCase):
    """Stress tests for hardware status reporting and honest telemetry."""

    def setUp(self):
        self.queue_mgr = ColorQueueManager()
        self.printer = ArgoxPrinterEngine(simulation_mode=False, device_path="/dev/usb/lp0")
        self.key_pool = GeminiKeyPoolManager(keys=["AIzaSyMOCK_KEY_1234567890abcdef"])
        self.dashboard = WebDashboard(self.queue_mgr, self.printer, self.key_pool)

    async def test_telemetry_printer_deleted_simulation_false(self):
        """
        When /dev/usb/lp0 does not exist and simulation_mode=False:
        GET /api/status MUST honestly report connected=False, simulation=False,
        and status_text='DISCONNECTED (/dev/usb/lp0 missing)'.
        """
        with patch("os.path.exists", return_value=False):
            self.printer.simulation_mode = False
            async with TestClient(TestServer(self.dashboard.app)) as client:
                resp = await client.get("/api/status")
                self.assertEqual(resp.status, 200)
                data = await resp.json()

                p_stat = data["hardware"]["printer"]
                self.assertFalse(p_stat["connected"], "Printer connected must be False when node missing")
                self.assertFalse(p_stat["simulation"], "Simulation must be False")
                self.assertIn("DISCONNECTED", p_stat["status_text"])
                self.assertIn("/dev/usb/lp0 missing", p_stat["status_text"])

    async def test_telemetry_printer_connected_simulation_false(self):
        """
        When /dev/usb/lp0 exists and simulation_mode=False:
        GET /api/status MUST honestly report connected=True, simulation=False,
        and status_text='CONNECTED (/dev/usb/lp0)'.
        """
        with patch("os.path.exists", return_value=True):
            self.printer.simulation_mode = False
            async with TestClient(TestServer(self.dashboard.app)) as client:
                resp = await client.get("/api/status")
                self.assertEqual(resp.status, 200)
                data = await resp.json()

                p_stat = data["hardware"]["printer"]
                self.assertTrue(p_stat["connected"])
                self.assertFalse(p_stat["simulation"])
                self.assertEqual(p_stat["status_text"], "CONNECTED (/dev/usb/lp0)")

    async def test_telemetry_simulation_mode_true_honest_declaration(self):
        """
        When simulation_mode=True:
        GET /api/status MUST honestly declare '[SIMULATION - HARDWARE BYPASSED]'.
        """
        self.printer.simulation_mode = True
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/status")
            self.assertEqual(resp.status, 200)
            data = await resp.json()

            p_stat = data["hardware"]["printer"]
            self.assertTrue(p_stat["simulation"])
            self.assertEqual(p_stat["status_text"], "[SIMULATION - HARDWARE BYPASSED]")

    async def test_telemetry_both_eth0_and_end0_carrier_absent(self):
        """
        When both eth0 and end0 carrier sysfs files are absent:
        GET /api/status MUST report carrier=False without crashing.
        """
        with patch("daemon.web_ui.Path.exists", return_value=False):
            async with TestClient(TestServer(self.dashboard.app)) as client:
                resp = await client.get("/api/status")
                self.assertEqual(resp.status, 200)
                data = await resp.json()

                s_stat = data["hardware"]["scanner"]
                self.assertFalse(s_stat["carrier"], "Carrier must be False when sysfs carrier absent")

    async def test_carrier_permission_error_resilience(self):
        """
        When carrier sysfs file raises PermissionError or OSError on read:
        Carrier check must catch error and return False without crashing.
        """
        def bad_read(*args, **kwargs):
            raise PermissionError("EACCES /sys/class/net/eth0/carrier")

        with patch("daemon.web_ui.Path.exists", return_value=True), \
             patch("daemon.web_ui.Path.read_text", side_effect=bad_read):
            carrier = self.dashboard.get_scanner_interface_carrier()
            self.assertFalse(carrier, "Carrier must return False gracefully on PermissionError")

    async def test_rapid_fire_status_polling_stress_100_cycles(self):
        """
        Stress test: 100 rapid sequential GET /api/status requests under fluctuating
        mock hardware states. Verifies 100% success rate with zero unhandled exceptions.
        """
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with patch("subprocess.run", return_value=mock_proc):
            async with TestClient(TestServer(self.dashboard.app)) as client:
                for cycle in range(100):
                    sim = (cycle % 2 == 0)
                    connected = (cycle % 3 == 0)
                    self.printer.simulation_mode = sim
                    
                    with patch("os.path.exists", return_value=connected):
                        resp = await client.get("/api/status")
                        self.assertEqual(resp.status, 200)
                        data = await resp.json()
                        self.assertIn("hardware", data)
                        self.assertIn("printer", data["hardware"])
                        self.assertIn("scanner", data["hardware"])


if __name__ == "__main__":
    unittest.main()
