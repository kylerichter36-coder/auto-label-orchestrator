import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aiohttp.test_utils import TestClient, TestServer
from daemon.config import config
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.color_queue import ColorQueueManager
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.web_ui import WebDashboard
from daemon.bartender_converter import parse_pplb_to_ast, PPLBEmitter, LabelAST


class TestBarTenderDesignerAPI(unittest.IsolatedAsyncioTestCase):
    """
    Comprehensive test suite for the BarTender WYSIWYG Label Designer & Creator.
    Tests API endpoints, AST round-tripping, file storage, security bounds,
    and simulation test printing.
    """

    def setUp(self):
        self.queue_mgr = ColorQueueManager()
        self.printer = ArgoxPrinterEngine(simulation_mode=True)
        self.key_pool = GeminiKeyPoolManager(keys=["AIzaSyMOCK_KEY_1234567890abcdef"])
        self.persistence = MagicMock()
        self.dashboard = WebDashboard(
            self.queue_mgr,
            self.printer,
            self.key_pool,
            persistence=self.persistence
        )

    async def test_list_templates(self):
        """Verifies GET /api/designer/templates returns list of available sample labels."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/designer/templates")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            self.assertIn("templates", data)
            filenames = [t["filename"] for t in data["templates"]]
            self.assertIn("CB540A_BLACK.prd", filenames)
            self.assertIn("OUTER_BOX_TEMPLATE.prd", filenames)

    async def test_get_template_prd(self):
        """Verifies GET /api/designer/template loads and parses a .prd file into AST."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/designer/template?file=CB540A_BLACK.prd")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["filename"], "CB540A_BLACK.prd")
            self.assertEqual(data["width_dots"], 800)
            self.assertEqual(data["height_dots"], 400)
            self.assertIn("ast", data)
            self.assertIn("raw_pplb", data)
            self.assertGreater(len(data["ast"]["elements"]), 0)

    async def test_get_template_path_traversal_blocked(self):
        """Verifies GET /api/designer/template rejects path traversal attacks."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            for bad in ["../../etc/shadow", "..\\..\\secret.key", "dir/file.prd", ""]:
                resp = await client.get(f"/api/designer/template?file={bad}")
                self.assertEqual(resp.status, 400)

    async def test_get_template_not_found(self):
        """Verifies GET /api/designer/template returns 404 for nonexistent files."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/designer/template?file=NONEXISTENT_12345.prd")
            self.assertEqual(resp.status, 404)

    async def test_render_ast_to_pplb(self):
        """Verifies POST /api/designer/render compiles AST into PPLB stream with variables."""
        sample_ast = {
            "width_dots": 800,
            "height_dots": 400,
            "density": 8,
            "speed": 2,
            "gap_dots": 24,
            "copies": 1,
            "elements": [
                {"type": "text", "x": 50, "y": 30, "content": "ORDER: {QUOTE_NUMBER}", "font": 4, "h_mult": 1, "v_mult": 1},
                {"type": "barcode_1d", "x": 50, "y": 120, "content": "{SKU}", "barcode_type": "1", "narrow": 2, "wide": 5, "height": 60, "hri": "B"},
                {"type": "line", "x": 50, "y": 200, "width": 700, "height": 4}
            ]
        }

        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/designer/render", json={
                "ast": sample_ast,
                "variables": {"QUOTE_NUMBER": "Q-8899", "SKU": "TONER-BLACK"}
            })
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            pplb = data["pplb"]
            self.assertIn("ORDER: Q-8899", pplb)
            self.assertIn("TONER-BLACK", pplb)
            self.assertIn("LO50,200,700,4", pplb)

    async def test_save_and_reload_label(self):
        """Verifies POST /api/designer/save writes a valid .prd file and reloads cleanly."""
        test_filename = f"TEST_DESIGNER_UNIT_{int(tempfile.NamedTemporaryFile().fileno())}.prd"
        sample_ast = {
            "width_dots": 800,
            "height_dots": 400,
            "density": 10,
            "speed": 2,
            "gap_dots": 24,
            "copies": 2,
            "elements": [
                {"type": "text", "x": 60, "y": 40, "content": "UNIT TEST LABEL", "font": 3, "h_mult": 1, "v_mult": 1, "reverse": False},
                {"type": "barcode_1d", "x": 60, "y": 100, "content": "TEST-1234", "barcode_type": "1", "narrow": 2, "wide": 5, "height": 60, "hri": "B"},
                {"type": "box", "x": 40, "y": 20, "width": 720, "height": 360, "thickness": 3}
            ]
        }

        target_dir = config.LABEL_DIR if (config.LABEL_DIR and config.LABEL_DIR.exists()) else (config.BASE_DIR / "templates" / "sample_labels")
        saved_file_path = target_dir / test_filename
        try:
            async with TestClient(TestServer(self.dashboard.app)) as client:
                # 1. Save label
                resp_save = await client.post("/api/designer/save", json={
                    "filename": test_filename,
                    "ast": sample_ast,
                    "overwrite": True
                })
                self.assertEqual(resp_save.status, 200)
                data_save = await resp_save.json()
                self.assertEqual(data_save["status"], "success")
                self.assertTrue(saved_file_path.exists())

                # 2. Prevent accidental overwrite when overwrite=False
                resp_dup = await client.post("/api/designer/save", json={
                    "filename": test_filename,
                    "ast": sample_ast,
                    "overwrite": False
                })
                self.assertEqual(resp_dup.status, 409)

                # 3. Reload via get_template
                resp_load = await client.get(f"/api/designer/template?file={test_filename}")
                self.assertEqual(resp_load.status, 200)
                data_load = await resp_load.json()
                loaded_elements = data_load["ast"]["elements"]
                self.assertEqual(len(loaded_elements), 3)
                self.assertEqual(loaded_elements[0]["content"], "UNIT TEST LABEL")
                self.assertEqual(loaded_elements[1]["content"], "TEST-1234")
                self.assertEqual(data_load["ast"]["copies"], 2)
        finally:
            if saved_file_path.exists():
                saved_file_path.unlink()

    async def test_save_path_traversal_blocked(self):
        """Verifies POST /api/designer/save rejects filenames with directory traversal."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            for bad_name in ["../../evil.prd", "..\\evil.prd", "sub/evil.prd", ""]:
                resp = await client.post("/api/designer/save", json={
                    "filename": bad_name,
                    "ast": {"width_dots": 800, "height_dots": 400, "elements": []}
                })
                self.assertEqual(resp.status, 400)

    async def test_designer_test_print_simulation(self):
        """Verifies POST /api/designer/test_print dispatches compiled PPLB to printer."""
        sample_ast = {
            "width_dots": 800,
            "height_dots": 400,
            "density": 8,
            "speed": 2,
            "gap_dots": 24,
            "copies": 1,
            "elements": [
                {"type": "text", "x": 50, "y": 30, "content": "DIRECT TEST PRINT", "font": 4, "h_mult": 1, "v_mult": 1},
                {"type": "barcode_1d", "x": 50, "y": 100, "content": "PRINT999", "barcode_type": "1", "narrow": 2, "wide": 5, "height": 60, "hri": "B"}
            ]
        }

        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/designer/test_print", json={
                "ast": sample_ast,
                "quantity": 3
            })
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            self.assertTrue(data["simulation"])
            self.assertIn("Successfully dispatched 3 label(s)", data["message"])

            # Verify simulation history in printer engine
            self.assertEqual(len(self.printer.simulated_print_history), 1)
            hist_item = self.printer.simulated_print_history[-1]
            self.assertEqual(hist_item["job_info"]["type"], "DESIGNER_TEST_PRINT")
            self.assertEqual(hist_item["job_info"]["quantity"], 3)
            self.assertIn("DIRECT TEST PRINT", hist_item["job_info"]["payload_preview"])

    async def test_html_contains_designer_features_and_pure_ascii(self):
        """Verifies index.html serves designer components, rulers, toolbar, and zero emojis."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/")
            self.assertEqual(resp.status, 200)
            html = await resp.text()

            # Designer UI presence
            self.assertIn("BarTender Designer", html)
            self.assertIn("designerContainer", html)
            self.assertIn("bt-workspace", html)
            self.assertIn("bt-label-board", html)
            self.assertIn("svgRulerH", html)
            self.assertIn("svgRulerV", html)
            self.assertIn("bt-inspector", html)
            self.assertIn("bt-drawer-stream", html)

            # Pure ASCII verification (strictly 0 emojis)
            non_ascii = [c for c in html if ord(c) > 127]
            self.assertEqual(len(non_ascii), 0, f"HTML must be pure ASCII, found: {non_ascii}")


    async def test_designer_malformed_json_and_missing_ast(self):
        """Verifies endpoints handle invalid JSON payloads and missing AST keys gracefully."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # 1. Invalid JSON to render
            resp = await client.post("/api/designer/render", data="not-json", headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status, 400)

            # 2. Missing AST to render
            resp = await client.post("/api/designer/render", json={})
            self.assertEqual(resp.status, 400)

            # 3. Invalid JSON to save
            resp = await client.post("/api/designer/save", data="not-json", headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status, 400)

            # 4. Missing filename to save
            resp = await client.post("/api/designer/save", json={"ast": {}})
            self.assertEqual(resp.status, 400)

            # 5. Invalid JSON to test_print
            resp = await client.post("/api/designer/test_print", data="not-json", headers={"Content-Type": "application/json"})
            self.assertEqual(resp.status, 400)

    async def test_designer_render_qr_and_datamatrix(self):
        """Verifies 2D barcodes (QR code and DataMatrix) compile into valid PPLB commands."""
        ast_2d = {
            "width_dots": 800,
            "height_dots": 400,
            "elements": [
                {"type": "barcode_2d", "x": 50, "y": 50, "content": "https://example.com/qr", "subtype": "QR", "model": 2, "cell_size": 5, "ecc": "H"},
                {"type": "barcode_2d", "x": 300, "y": 50, "content": "DM-DATA-7788", "subtype": "DATAMATRIX", "cols": 0, "rows": 0}
            ]
        }
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/designer/render", json={"ast": ast_2d})
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            pplb = data["pplb"]
            # QR command in PPLB: b<x>,<y>,Q,s<model>,m<cell_size>,r<ecc>,"<content>"
            self.assertIn('b50,50,Q,s2,m5,rH,"https://example.com/qr"', pplb)
            # DataMatrix command in PPLB: b<x>,<y>,D,c<cols>,r<rows>,"<content>"
            self.assertIn('b300,50,D,c0,r0,"DM-DATA-7788"', pplb)

    async def test_save_with_raw_pplb_string(self):
        """Verifies POST /api/designer/save accepts raw_pplb string directly."""
        test_filename = f"TEST_RAW_{int(tempfile.NamedTemporaryFile().fileno())}.prd"
        raw_stream = "N\nq800\nQ400,24\nD8\nS2\nA50,50,0,3,1,1,N,\"RAW TEST\"\nP1\n"

        target_dir = config.LABEL_DIR if (config.LABEL_DIR and config.LABEL_DIR.exists()) else (config.BASE_DIR / "templates" / "sample_labels")
        saved_file = target_dir / test_filename
        try:
            async with TestClient(TestServer(self.dashboard.app)) as client:
                resp = await client.post("/api/designer/save", json={
                    "filename": test_filename,
                    "raw_pplb": raw_stream,
                    "overwrite": True
                })
                self.assertEqual(resp.status, 200)
                self.assertTrue(saved_file.exists())
                self.assertEqual(saved_file.read_text(encoding="utf-8"), raw_stream)
        finally:
            if saved_file.exists():
                saved_file.unlink()

    async def test_get_template_prn_and_xml(self):
        """Verifies loading .prn and .xml templates parses properly."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # 1. Load .prn template
            resp_prn = await client.get("/api/designer/template?file=CB541A_CYAN.prn")
            self.assertEqual(resp_prn.status, 200)
            data_prn = await resp_prn.json()
            self.assertEqual(data_prn["status"], "success")
            self.assertGreater(len(data_prn["ast"]["elements"]), 0)

            # 2. Load .xml BTXML template
            resp_xml = await client.get("/api/designer/template?file=SAMPLE_EXPORT_BTXML.xml")
            self.assertEqual(resp_xml.status, 200)
            data_xml = await resp_xml.json()
            self.assertEqual(data_xml["status"], "success")
            self.assertGreater(len(data_xml["ast"]["elements"]), 0)

    async def test_get_template_btw_rejected(self):
        """Verifies proprietary binary .btw files are explicitly rejected with 400."""
        target_dir = config.LABEL_DIR if (config.LABEL_DIR and config.LABEL_DIR.exists()) else (config.BASE_DIR / "templates" / "sample_labels")
        dummy_btw = target_dir / "TEST_PROPRIETARY.btw"
        try:
            dummy_btw.write_bytes(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1BarTenderBinaryData")
            async with TestClient(TestServer(self.dashboard.app)) as client:
                resp = await client.get(f"/api/designer/template?file={dummy_btw.name}")
                self.assertEqual(resp.status, 400)
                data = await resp.json()
                self.assertEqual(data["status"], "error")
                self.assertIn("proprietary binary BarTender document (.btw)", data["message"])
        finally:
            if dummy_btw.exists():
                dummy_btw.unlink()

    async def test_designer_test_print_default_variables_interpolated(self):
        """Verifies designer test print interpolates realistic defaults when variables are not specified."""
        ast_with_vars = {
            "width_dots": 800,
            "height_dots": 400,
            "elements": [
                {"type": "text", "x": 50, "y": 30, "content": "ORDER: {QUOTE_NUMBER}"},
                {"type": "text", "x": 50, "y": 80, "content": "PRODUCT: {SKU} - {ITEM_NAME}"},
                {"type": "barcode_1d", "x": 50, "y": 140, "content": "{BARCODE}", "barcode_type": "1"}
            ]
        }
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/designer/test_print", json={
                "ast": ast_with_vars,
                "quantity": 1
            })
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            hist_item = self.printer.simulated_print_history[-1]
            preview = hist_item["job_info"]["payload_preview"]
            # Must interpolate rather than leave raw braces
            self.assertNotIn("{QUOTE_NUMBER}", preview)
            self.assertNotIn("{SKU}", preview)
            self.assertIn("Q-8821", preview)
            self.assertIn("CB540A", preview)

    async def test_designer_line_thickness_height_sync(self):
        """Verifies line element thickness and height are synchronized during render."""
        ast_line = {
            "width_dots": 800,
            "height_dots": 400,
            "elements": [
                {"type": "line", "x": 50, "y": 100, "width": 600, "height": 6, "thickness": 6}
            ]
        }
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post("/api/designer/render", json={"ast": ast_line})
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            pplb = data["pplb"]
            self.assertIn("LO50,100,600,6", pplb)

    async def test_variable_alias_expansion(self):
        """Verifies bidirectional enterprise variable aliases in substitute_placeholders."""
        from daemon.bartender_converter import substitute_placeholders

        # 1. Alias QUOTE_ID provided, template uses {QUOTE_NUMBER}
        text1 = "Quote: {QUOTE_NUMBER}, SKU: {SKU}, Name: {ITEM_NAME}, Weight: {WEIGHT_KG}kg"
        vars1 = {"QUOTE_ID": "Q-9901", "SKU": "CE285A", "DESCRIPTION": "HP 85A Toner", "GROSS_WEIGHT": "0.85"}
        res1 = substitute_placeholders(text1, vars1)
        self.assertEqual(res1, "Quote: Q-9901, SKU: CE285A, Name: HP 85A Toner, Weight: 0.85kg")

        # 2. Alias QUOTE_NUMBER provided, template uses {QUOTE_ID}
        text2 = "ID: {QUOTE_ID}, Weight: {GROSS_WEIGHT}kg, Qty: {QTY}"
        vars2 = {"QUOTE_NUMBER": "Q-7722", "WEIGHT_KG": "1.40", "QUANTITY": "5"}
        res2 = substitute_placeholders(text2, vars2)
        self.assertEqual(res2, "ID: Q-7722, Weight: 1.40kg, Qty: 5")


if __name__ == "__main__":
    unittest.main()
