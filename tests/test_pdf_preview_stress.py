"""
Empirical Stress Test Suite: In-Memory PDF Rasterizer & Confidential Memory Shredder.

Target Hardware: Orange Pi PC Plus (Allwinner H3, 1GB DDR3 RAM).
Security Protocol: Zero disk writes. Ephemeral in-memory rendering.
Confidential Shredding: Overwrites all mutable byte buffers with zeros in RAM.
Integrity Standard: Strict Industrial Enterprise Standard, Zero Emojis, Zero Fake Readings.
"""

import io
import gc
import os
import sys
import time
import base64
import psutil
import tempfile
import builtins
import threading
import unittest
from pathlib import Path

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from daemon import pdf_preview
from simulator.mock_lexmark import create_synthetic_pdf
from reportlab.pdfgen import canvas
from reportlab.lib.pdfencrypt import StandardEncryption


def create_multipage_synthetic_pdf(num_pages: int = 5) -> bytes:
    """Helper to generate an in-memory multi-page PDF."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(num_pages):
        c.drawString(100, 750, f"Empirical Stress Test Page {i + 1} of {num_pages}")
        c.rect(50, 50, 500, 700)
        for line in range(25):
            c.drawString(70, 700 - line * 22, f"Item {line}: SKU-TEST-{i}-{line} QTY: {line + 1} PRICE: ${line * 9.99:.2f}")
        c.showPage()
    c.save()
    return buf.getvalue()


def create_encrypted_synthetic_pdf(password: str = "secretpass") -> bytes:
    """Helper to generate an encrypted PDF with user password protection."""
    enc = StandardEncryption(password, "ownerpassword")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, encrypt=enc)
    c.drawString(100, 750, "Confidential Encrypted Invoice - Unauthorized Access Prohibited")
    c.rect(50, 50, 500, 700)
    c.showPage()
    c.save()
    return buf.getvalue()


class TestMalformedAndExtremePDFInputs(unittest.TestCase):
    """Stress tests rasterizer against malformed, boundary, and hostile PDF inputs."""

    def setUp(self):
        self.valid_1page_pdf = create_synthetic_pdf("QT-1PAGE-VALID")
        self.valid_5page_pdf = create_multipage_synthetic_pdf(5)

    def test_zero_length_bytes_buffer(self):
        """Verifies zero-length byte buffer raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            pdf_preview.render_pdf_page_preview(b"")
        self.assertIn("Empty PDF document received", str(ctx.exception))

    def test_non_pdf_ascii_headers(self):
        """Verifies plain text and HTML payloads without %PDF header raise ValueError."""
        test_payloads = [
            b"This is a plain ASCII document without PDF headers.",
            b"<!DOCTYPE html><html><body>Not a PDF</body></html>",
            b"     \t\r\n     ",
            b"ORDER_ID,SKU,QTY,PRICE\n1001,CART-BLK,5,45.00\n",
        ]
        for payload in test_payloads:
            with self.subTest(payload=payload[:20]):
                with self.assertRaises(ValueError) as ctx:
                    pdf_preview.render_pdf_page_preview(payload)
                self.assertIn("Invalid PDF stream", str(ctx.exception))

    def test_non_pdf_binary_headers(self):
        """Verifies non-PDF binary magic bytes raise ValueError."""
        test_binaries = [
            b"PK\x03\x04" + b"\x00" * 60,  # ZIP / DOCX / BTW header
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 60,  # PNG header
            b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 60,  # JPEG header
            b"\x1f\x8b\x08" + b"\x00" * 60,  # GZIP header
        ]
        for b_data in test_binaries:
            with self.subTest(header=b_data[:4]):
                with self.assertRaises(ValueError) as ctx:
                    pdf_preview.render_pdf_page_preview(b_data)
                self.assertIn("Invalid PDF stream", str(ctx.exception))

    def test_truncated_pdf_streams(self):
        """Verifies truncated PDF streams raise ValueError."""
        truncated_cases = [
            b"%PDF-1.4\n",  # Header only
            self.valid_1page_pdf[:20],
            self.valid_1page_pdf[:100],
            self.valid_1page_pdf[:len(self.valid_1page_pdf) // 2],
        ]
        for truncated in truncated_cases:
            with self.subTest(trunc_len=len(truncated)):
                with self.assertRaises(ValueError) as ctx:
                    pdf_preview.render_pdf_page_preview(truncated)
                self.assertIn("Invalid PDF stream", str(ctx.exception))

    def test_corrupted_xref_and_trailer(self):
        """Verifies severely corrupted PDF with invalid trailer and xref raises ValueError."""
        corrupted = b"%PDF-1.4\n" + b"X" * 1024 + b"\n%%EOF"
        with self.assertRaises(ValueError) as ctx:
            pdf_preview.render_pdf_page_preview(corrupted)
        self.assertIn("Invalid PDF stream", str(ctx.exception))

    def test_encrypted_password_protected_pdf(self):
        """Verifies encrypted/password-protected PDFs raise ValueError upon opening."""
        enc_pdf = create_encrypted_synthetic_pdf("Confidential123")
        with self.assertRaises(ValueError) as ctx:
            pdf_preview.render_pdf_page_preview(enc_pdf)
        self.assertIn("Invalid PDF stream", str(ctx.exception))

    def test_page_index_boundaries_single_page(self):
        """Verifies boundary page indices on a 1-page document (-1, 0, 1, 9999)."""
        # Valid page 0
        res = pdf_preview.render_pdf_page_preview(self.valid_1page_pdf, page_idx=0)
        self.assertTrue(res.startswith("data:image/jpeg;base64,"))

        # Index -1
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.valid_1page_pdf, page_idx=-1)

        # Index 1
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.valid_1page_pdf, page_idx=1)

        # Index 9999
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.valid_1page_pdf, page_idx=9999)

        # Index -9999
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.valid_1page_pdf, page_idx=-9999)

    def test_page_index_boundaries_multi_page(self):
        """Verifies boundary page indices on a 5-page document (-1, 0, 4, 5, 100)."""
        # Negative index
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.valid_5page_pdf, page_idx=-1)

        # First page (0)
        p0 = pdf_preview.render_pdf_page_preview(self.valid_5page_pdf, page_idx=0)
        self.assertTrue(p0.startswith("data:image/jpeg;base64,"))

        # Last page (4)
        p4 = pdf_preview.render_pdf_page_preview(self.valid_5page_pdf, page_idx=4)
        self.assertTrue(p4.startswith("data:image/jpeg;base64,"))

        # Beyond last page (5)
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.valid_5page_pdf, page_idx=5)

        # Large index (100)
        with self.assertRaises(IndexError):
            pdf_preview.render_pdf_page_preview(self.valid_5page_pdf, page_idx=100)


class TestInMemoryZeroDiskVerification(unittest.TestCase):
    """Empirically verifies that zero temporary files are written to disk during rasterization."""

    def setUp(self):
        self.sample_pdf = create_synthetic_pdf("ZERO_DISK_VERIFICATION")
        self.temp_dir = tempfile.gettempdir()

    def test_zero_files_created_in_temp_and_project_directories(self):
        """Empirically monitors temp directory and project tree to verify zero files created."""
        before_temp = set(os.listdir(self.temp_dir))
        before_proj = set(
            os.path.join(r, f)
            for r, d, fs in os.walk(str(PROJECT_ROOT))
            for f in fs
            if "__pycache__" not in r
        )

        # Perform multiple rasterizations with different DPIs and pages
        _ = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=72)
        _ = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=150)
        _ = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=200)

        after_temp = set(os.listdir(self.temp_dir))
        after_proj = set(
            os.path.join(r, f)
            for r, d, fs in os.walk(str(PROJECT_ROOT))
            for f in fs
            if "__pycache__" not in r
        )

        new_temp_files = after_temp - before_temp
        new_proj_files = after_proj - before_proj

        self.assertEqual(
            new_temp_files,
            set(),
            f"Temporary files leaked into temp directory: {new_temp_files}",
        )
        self.assertEqual(
            new_proj_files,
            set(),
            f"Files leaked into project directory: {new_proj_files}",
        )

    def test_zero_disk_write_syscalls_via_open_hook(self):
        """Hooks builtins.open to verify zero write operations are executed during rasterization."""
        orig_open = builtins.open
        write_operations = []

        def spy_open(*args, **kwargs):
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            if any(m in mode for m in ["w", "a", "+", "x"]):
                write_operations.append((args, kwargs))
            return orig_open(*args, **kwargs)

        builtins.open = spy_open
        try:
            _ = pdf_preview.render_pdf_page_preview(self.sample_pdf, page_idx=0, dpi=150)
        finally:
            builtins.open = orig_open

        self.assertEqual(
            len(write_operations),
            0,
            f"File write operations intercepted during rasterization: {write_operations}",
        )

    def test_zero_disk_writes_on_malformed_inputs(self):
        """Ensures exception handling for corrupted inputs also produces zero disk files."""
        before_temp = set(os.listdir(self.temp_dir))

        for malformed in [b"", b"GARBAGE_DATA", b"%PDF-CORRUPT_STREAM"]:
            try:
                pdf_preview.render_pdf_page_preview(malformed)
            except (ValueError, IndexError):
                pass

        after_temp = set(os.listdir(self.temp_dir))
        self.assertEqual(after_temp - before_temp, set())


class TestConfidentialShreddingStress(unittest.TestCase):
    """Stress tests the in-memory confidential shredder with 1MB and 5MB pseudo-random buffers."""

    def test_shred_1mb_buffer_pseudo_random(self):
        """Allocates 1MB pseudo-random buffer, executes shredding, and asserts 100% bytes equal 0."""
        size = 1 * 1024 * 1024  # 1MB
        buf = bytearray(os.urandom(size))

        # Sanity check: buffer must contain non-zero data initially
        self.assertTrue(any(b != 0 for b in buf), "Initial buffer was unexpectedly all zero.")

        pdf_preview.shred_memory_buffer(buf)

        self.assertEqual(len(buf), size, "Buffer size altered by shredder.")
        self.assertTrue(all(b == 0 for b in buf), "Buffer contains non-zero bytes after 1MB shredding.")

    def test_shred_5mb_buffer_pseudo_random(self):
        """Allocates 5MB pseudo-random buffer, executes shredding, and asserts 100% bytes equal 0."""
        size = 5 * 1024 * 1024  # 5MB
        buf = bytearray(os.urandom(size))

        self.assertTrue(any(b != 0 for b in buf), "Initial buffer was unexpectedly all zero.")

        t0 = time.perf_counter()
        pdf_preview.shred_memory_buffer(buf)
        elapsed = time.perf_counter() - t0

        self.assertEqual(len(buf), size, "Buffer size altered by shredder.")
        self.assertTrue(all(b == 0 for b in buf), "Buffer contains non-zero bytes after 5MB shredding.")
        # Performance check: 5MB shredding should complete in under 1.0 second
        self.assertLess(elapsed, 1.0, f"5MB shredding exceeded latency threshold: {elapsed:.3f}s")

    def test_shred_memoryview_wrapper(self):
        """Verifies shredding works directly on memoryview objects."""
        size = 256 * 1024  # 256KB
        raw_buf = bytearray(os.urandom(size))
        mv = memoryview(raw_buf)

        pdf_preview.shred_memory_buffer(mv)

        self.assertTrue(all(b == 0 for b in raw_buf), "Underlying bytearray not zeroed via memoryview.")

    def test_shred_idempotency_and_edge_cases(self):
        """Verifies shredding handles repeated calls, empty buffers, and None without failure."""
        buf = bytearray(b"SENSITIVE_QUOTE_DATA_12345")
        pdf_preview.shred_memory_buffer(buf)
        self.assertTrue(all(b == 0 for b in buf))

        # Second call on already shredded buffer
        pdf_preview.shred_memory_buffer(buf)
        self.assertTrue(all(b == 0 for b in buf))

        # Zero length buffer
        empty_buf = bytearray(0)
        pdf_preview.shred_memory_buffer(empty_buf)
        self.assertEqual(len(empty_buf), 0)

        # None input
        pdf_preview.shred_memory_buffer(None)


class TestMemoryConsumptionAndOOMGuarding(unittest.TestCase):
    """Stress tests RAM consumption during multi-page rendering against Orange Pi 1GB budget."""

    def test_ram_consumption_20_page_rendering(self):
        """Renders 20 pages consecutively and verifies RAM overhead stays within strict budget."""
        proc = psutil.Process()
        pdf_data = create_multipage_synthetic_pdf(num_pages=20)

        gc.collect()
        base_ram_mb = proc.memory_info().rss / (1024 * 1024)

        ram_measurements = []
        previews = []
        for p in range(20):
            preview = pdf_preview.render_pdf_page_preview(pdf_data, page_idx=p, dpi=150)
            previews.append(preview)
            current_ram_mb = proc.memory_info().rss / (1024 * 1024)
            ram_measurements.append(current_ram_mb)

        peak_ram_mb = max(ram_measurements)
        delta_ram_mb = peak_ram_mb - base_ram_mb

        # Orange Pi budget: MemoryMax=350M, total 1GB RAM.
        # Delta for rendering 20 pages should not exceed 100MB.
        self.assertLess(
            delta_ram_mb,
            100.0,
            f"RAM consumption delta ({delta_ram_mb:.2f} MB) exceeded 100MB threshold.",
        )

        # Verify all pages rendered successfully
        self.assertEqual(len(previews), 20)
        for p in previews:
            self.assertTrue(p.startswith("data:image/jpeg;base64,"))

        # Cleanup verification
        del previews
        gc.collect()
        final_ram_mb = proc.memory_info().rss / (1024 * 1024)
        self.assertLess(final_ram_mb, peak_ram_mb + 20.0)

    def test_ephemeral_store_ram_purging(self):
        """Verifies storing 50 ephemeral previews and purging cleanly empties RAM cache."""
        pdf_preview.clear_all_previews()
        sample_img = "data:image/jpeg;base64," + "A" * 10000  # ~10KB payload

        for i in range(50):
            pdf_preview.store_ephemeral_preview(f"QT-STRESS-{i:03d}", sample_img, ttl_seconds=300)

        active = pdf_preview.list_active_previews()
        self.assertEqual(len(active), 50)

        pdf_preview.clear_all_previews()
        active_after = pdf_preview.list_active_previews()
        self.assertEqual(len(active_after), 0)


class TestConcurrentPreviewAccess(unittest.TestCase):
    """Stress tests thread-safety and race conditions on ephemeral store."""

    def setUp(self):
        pdf_preview.clear_all_previews()

    def tearDown(self):
        pdf_preview.clear_all_previews()

    def test_multi_threaded_store_and_lookup_stress(self):
        """Runs 10 concurrent threads executing 50 operations each on ephemeral store."""
        thread_errors = []

        def worker(tid: int):
            try:
                for i in range(50):
                    qid = f"QT-THREAD-{tid}-{i}"
                    b64 = f"data:image/jpeg;base64,PAYLOAD_{tid}_{i}"
                    pdf_preview.store_ephemeral_preview(qid, b64, ttl_seconds=5)
                    val = pdf_preview.get_ephemeral_preview(qid)
                    if val != b64:
                        thread_errors.append(f"Mismatch in {qid}: got {val}")
                    _ = pdf_preview.list_active_previews()
                    if i % 3 == 0:
                        pdf_preview.dismiss_preview(qid)
            except Exception as e:
                thread_errors.append(f"Exception in thread {tid}: {e}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(
            len(thread_errors),
            0,
            f"Thread-safety violations detected: {thread_errors[:5]}",
        )


if __name__ == "__main__":
    unittest.main()
