import unittest
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.bartender_converter import (
    CoordinateEngine,
    sanitize_text,
    sanitize_barcode_data,
    LabelAST,
    TextElement,
    Barcode1DElement,
    Barcode2DElement,
    LineElement,
    BoxElement,
    PPLBEmitter,
    PPLAEmitter,
    inject_quantity,
    substitute_placeholders,
    parse_bartender_template,
    parse_btxml,
    parse_csv_integration,
    parse_pplb_to_ast,
    render_intermediate_ast,
    convert_bartender_to_pplb,
    convert_bartender_to_ppla,
    CARTRIDGE_WIDTH_DOTS,
    CARTRIDGE_HEIGHT_DOTS
)
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.classifier import OuterBoxSheet, BoxItemContent
from simulator.mock_argox import VirtualArgoxPrinter


class TestBarTenderConverter(unittest.TestCase):
    def setUp(self):
        self.label_dir = Path(__file__).resolve().parent.parent / "templates" / "sample_labels"
        self.printer_pplb = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True, emulation="PPLB")
        self.printer_ppla = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True, emulation="PPLA")
        self.virtual_argox = VirtualArgoxPrinter()

    # 1. Coordinate Engine Tests
    def test_coordinate_conversions(self):
        """Verify millimeter, inch, and dot conversions at 203.2 DPI."""
        self.assertEqual(CoordinateEngine.mm_to_dots(100), 800)
        self.assertEqual(CoordinateEngine.mm_to_dots(50), 400)
        self.assertAlmostEqual(CoordinateEngine.dots_to_mm(800), 100.0)

        # 4 inches @ 203.2 DPI ~ 813 dots
        self.assertEqual(CoordinateEngine.inches_to_dots(4.0), 813)
        self.assertAlmostEqual(CoordinateEngine.dots_to_inches(813), 4.0, delta=0.01)

        # scale_coordinate helper
        self.assertEqual(CoordinateEngine.scale_coordinate(100, "mm"), 800)
        self.assertEqual(CoordinateEngine.scale_coordinate(4, "in"), 813)
        self.assertEqual(CoordinateEngine.scale_coordinate(400, "dots"), 400)

    def test_coordinate_bounds_validation(self):
        """Verify coordinate bounds checking against printable label boundaries."""
        valid, msg = CoordinateEngine.validate_bounds(50, 30, width=700, height=300, max_width=800, max_height=400)
        self.assertTrue(valid)
        self.assertIsNone(msg)

        # Negative coordinate
        valid_neg, msg_neg = CoordinateEngine.validate_bounds(-10, 20)
        self.assertFalse(valid_neg)
        self.assertIn("negative", msg_neg)

        # Width overflow
        valid_w, msg_w = CoordinateEngine.validate_bounds(150, 20, width=700, max_width=800)
        self.assertFalse(valid_w)
        self.assertIn("exceeds max width", msg_w)

        # AST bounds validation in strict mode
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_text(x=850, y=50, content="Overflow")
        with self.assertRaises(ValueError):
            ast.validate_bounds(strict=True)

    # 2. String Sanitization Tests
    def test_string_sanitization(self):
        """Verify double quotes, backslashes, control characters, and newlines are safely sanitized."""
        raw_text = 'Toner "High Yield" \\ Model #1\x00\x07\nLine 2\r'
        clean = sanitize_text(raw_text)
        self.assertNotIn('"', clean)
        self.assertIn("'", clean)
        self.assertNotIn('\\', clean)
        self.assertIn('/', clean)
        self.assertNotIn('\x00', clean)
        self.assertNotIn('\x07', clean)
        self.assertNotIn('\n', clean)
        self.assertNotIn('\r', clean)
        self.assertEqual(clean, "Toner 'High Yield' / Model #1 Line 2")

    def test_barcode_data_sanitization(self):
        """Verify barcode data removes quotes, slashes, and control chars."""
        raw_barcode = ' "CRG-716\\BK\x03" '
        clean = sanitize_barcode_data(raw_barcode)
        self.assertEqual(clean, "CRG-716BK")

    # 3. Ingestion of .prd and .prn Templates
    def test_ingest_prd_template(self):
        """Verify .prd template is resolved and ingested with dynamic quantity."""
        success = self.printer_pplb.dispatch_cartridge_label("CB540A_BLACK.prd", quantity=12)
        self.assertTrue(success)

        last_job = self.printer_pplb.simulated_print_history[-1]
        self.assertEqual(last_job["job_info"]["file"], "CB540A_BLACK.prd")
        self.assertEqual(last_job["job_info"]["quantity"], 12)

        label_info = self.virtual_argox.process_pplb(last_job["data"].encode("utf-8"))
        self.assertEqual(label_info["quantity"], 12)
        texts = [t["text"] for t in label_info["text_elements"]]
        self.assertIn("HP CB540A (125A)", texts)

    def test_ingest_prn_template(self):
        """Verify .prn template continues to be resolved and dispatched accurately."""
        success = self.printer_pplb.dispatch_cartridge_label("CANON_716_BLACK.prn", quantity=25)
        self.assertTrue(success)

        last_job = self.printer_pplb.simulated_print_history[-1]
        self.assertEqual(last_job["job_info"]["quantity"], 25)

        label_info = self.virtual_argox.process_pplb(last_job["data"].encode("utf-8"))
        self.assertEqual(label_info["quantity"], 25)
        texts = [t["text"] for t in label_info["text_elements"]]
        self.assertIn("CANON 716 BLACK", texts)

    # 4. Dynamic Placeholder Substitution
    def test_dynamic_placeholder_substitution(self):
        """Verify {ITEM_NAME}, {BARCODE}, {QUANTITY}, {NOTE} placeholders are substituted cleanly."""
        variables = {
            "ITEM_NAME": 'LEXMARK "EXTRA HIGH" TONER',
            "BARCODE": "LEX-501X",
            "QUANTITY": 45,
            "NOTE": "Urgent Batch 9"
        }
        res_bytes = convert_bartender_to_pplb(
            str(self.label_dir / "CARTRIDGE_TEMPLATE_PARAMETRIC.prd"),
            variables
        )
        res_text = res_bytes.decode("utf-8")

        self.assertIn("LEXMARK 'EXTRA HIGH' TONER", res_text)
        self.assertIn("LEX-501X", res_text)
        self.assertIn("Urgent Batch 9", res_text)
        self.assertIn("P45", res_text)
        self.assertNotIn("{ITEM_NAME}", res_text)
        self.assertNotIn("{BARCODE}", res_text)
        self.assertNotIn("{QUANTITY}", res_text)

    # 5. Symbology Formatting (Code 128, QR Code, DataMatrix)
    def test_barcode_symbologies_pplb(self):
        """Verify Code 128, QR Code, and DataMatrix emission in PPLB."""
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_barcode_1d(50, 50, "SKU-128", barcode_type="1", narrow=2, wide=5, height=60, hri="B")
        ast.add_barcode_2d(50, 150, "https://argox.local/sku", subtype="QR", model=2, cell_size=4, ecc="M")
        ast.add_barcode_2d(50, 250, "BATCH-DM200", subtype="DATAMATRIX", cols=20, rows=20)

        pplb_bytes = PPLBEmitter.emit(ast)
        pplb_str = pplb_bytes.decode("utf-8")

        # Code 128 check
        self.assertIn('B50,50,0,1,2,5,60,B,"SKU-128"', pplb_str)
        # QR Code check
        self.assertIn('b50,150,Q,s2,m4,rM,"https://argox.local/sku"', pplb_str)
        # DataMatrix check
        self.assertIn('b50,250,D,c20,r20,"BATCH-DM200"', pplb_str)

    def test_barcode_symbologies_ppla(self):
        """Verify Code 128, QR Code, and DataMatrix emission in PPLA (Datamax DPL)."""
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_barcode_1d(50, 50, "SKU-128", barcode_type="1", narrow=2, wide=4, height=60)
        ast.add_barcode_2d(50, 150, "QR-PAYLOAD", subtype="QR", cell_size=3, ecc="M")
        ast.add_barcode_2d(50, 250, "DM-PAYLOAD", subtype="DATAMATRIX", cols=16, rows=16)

        ppla_bytes = PPLAEmitter.emit(ast)
        ppla_str = ppla_bytes.decode("utf-8")

        # PPLA header
        self.assertIn("\x02L", ppla_str)
        # Code 128: 1e<w><n><hhh><yyyy><xxxx><data>
        # Height is 400 - 50 - 60 = 290 dots in PPLA inverted Y
        self.assertIn("1e4206002900050SKU-128", ppla_str)
        # QR Code in PPLA
        self.assertIn("W1QM3", ppla_str)
        self.assertIn("QR-PAYLOAD", ppla_str)
        # DataMatrix in PPLA
        self.assertIn("W1D1616", ppla_str)
        self.assertIn("DM-PAYLOAD", ppla_str)

    # 6. Dynamic Quantity Injection Tests
    def test_dynamic_quantity_injection_pplb(self):
        """Verify trailing P<N> is rewritten without corrupting data containing 'P'."""
        sample_pplb = (
            "N\n"
            "q800\n"
            "Q400,24\n"
            'A50,30,0,4,1,1,N,"PART P100 - PRODUCT CODE P9"\n'
            'B50,140,0,1,2,5,60,B,"P12345"\n'
            "P1\n"
        )
        # Single digit
        q5 = inject_quantity(sample_pplb, 5, emulation="PPLB").decode("utf-8")
        self.assertIn('A50,30,0,4,1,1,N,"PART P100 - PRODUCT CODE P9"', q5)
        self.assertIn('B50,140,0,1,2,5,60,B,"P12345"', q5)
        self.assertTrue(q5.endswith("P5\n"))

        # Multi digit
        q350 = inject_quantity(sample_pplb, 350, emulation="PPLB").decode("utf-8")
        self.assertTrue(q350.endswith("P350\n"))
        self.assertIn('A50,30,0,4,1,1,N,"PART P100 - PRODUCT CODE P9"', q350)

    def test_dynamic_quantity_injection_ppla(self):
        """Verify trailing Q<N> in PPLA is rewritten with 4-digit zero padding."""
        sample_ppla = "\x02L\rD11\r121100002000050Hello\rQ0001\rE\r"
        res = inject_quantity(sample_ppla, 42, emulation="PPLA").decode("utf-8")
        self.assertIn("Q0042\rE", res)
        self.assertNotIn("Q0001", res)

    # 7. Intermediate AST & PPLA Coordinate Inversion
    def test_ppla_emitter_coordinate_inversion(self):
        """Verify Y coordinate is reflected from top-left (PPLB) to bottom-left (PPLA)."""
        ast = LabelAST(width_dots=800, height_dots=400, copies=3)
        # Font 4 height is 24 dots. Placed at y=30.
        # Bottom-left Y should be: 400 - 30 - 24 = 346 dots.
        ast.add_text(x=50, y=30, content="TOP LINE", font=4, h_mult=1, v_mult=1)

        ppla_str = PPLAEmitter.emit(ast).decode("utf-8")
        # Text command: 1 4 1 1 000 0346 0050 TOP LINE
        self.assertIn("141100003460050TOP LINE", ppla_str)
        self.assertIn("Q0003", ppla_str)
        self.assertIn("E", ppla_str)

    # 8. BTXML Ingestion Tests
    def test_btxml_ingestion(self):
        """Verify BTXML records are extracted and formatted into PPLB."""
        btxml_path = self.label_dir / "SAMPLE_EXPORT_BTXML.xml"
        parsed = parse_btxml(str(btxml_path))

        self.assertEqual(parsed["format_name"], "CANON_716_BLACK.btw")
        self.assertEqual(len(parsed["records"]), 1)
        rec = parsed["records"][0]
        self.assertEqual(rec["ITEM_NAME"], "CANON 716 BLACK")
        self.assertEqual(rec["BARCODE"], "CRG-716BK")
        self.assertEqual(rec["QUANTITY"], "25")

    def test_btxml_label_definition_to_ast(self):
        """Verify direct <Label> XML element definition creates a valid LabelAST."""
        xml_data = """
        <Label Width="100mm" Height="50mm">
            <Text X="50" Y="40" Font="4" Content="XML TONER"/>
            <Barcode X="50" Y="120" Type="Code128" Height="60" Content="XT-100"/>
        </Label>
        """
        parsed = parse_btxml(xml_data)
        ast = parsed["ast"]
        self.assertIsNotNone(ast)
        self.assertEqual(ast.width_dots, 800)
        self.assertEqual(ast.height_dots, 400)
        self.assertEqual(len(ast.elements), 2)
        self.assertEqual(ast.elements[0].content, "XML TONER")

    # 9. CSV Integration Tests
    def test_csv_integration_parsing(self):
        """Verify CSV rows are parsed with header keys."""
        csv_path = self.label_dir / "SAMPLE_EXPORT_CSV.csv"
        rows = parse_csv_integration(str(csv_path))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["ITEM_NAME"], "HP CB540A (125A)")
        self.assertEqual(rows[0]["QUANTITY"], "15")
        self.assertEqual(rows[1]["ITEM_NAME"], "CANON 716 BLACK")
        self.assertEqual(rows[2]["ITEM_NAME"], "DR3115 DRUM UNIT")

    # 10. Reverse Parsing PPLB to AST
    def test_reverse_parse_pplb_to_ast(self):
        """Verify round-trip parsing of native PPLB stream into AST."""
        raw_pplb = (
            "N\n"
            "q800\n"
            "Q400,24\n"
            "D8\n"
            "S2\n"
            'A50,30,0,4,1,1,N,"ROUND TRIP TEST"\n'
            'B50,140,0,1,2,5,60,B,"RT-128"\n'
            "LO50,220,700,4\n"
            "P10\n"
        )
        ast = parse_pplb_to_ast(raw_pplb)
        self.assertEqual(ast.width_dots, 800)
        self.assertEqual(ast.height_dots, 400)
        self.assertEqual(ast.gap_dots, 24)
        self.assertEqual(ast.density, 8)
        self.assertEqual(ast.speed, 2)
        self.assertEqual(ast.copies, 10)
        self.assertEqual(len(ast.elements), 3)
        self.assertEqual(ast.elements[0].content, "ROUND TRIP TEST")
        self.assertEqual(ast.elements[1].content, "RT-128")
        self.assertEqual(ast.elements[2].width, 700)

    # 11. Printer Engine Integration & Emulation Selection
    def test_printer_engine_ppla_mode(self):
        """Verify ArgoxPrinterEngine dispatches PPLA format when emulation='PPLA'."""
        box = OuterBoxSheet(
            quote_number="QT-88001",
            weight_kg="8.5",
            current_box=1,
            total_boxes=2,
            copies_per_box=2,
            box_contents=[
                BoxItemContent(quantity=4, unit_type="Toner Unit", model="HP CB540A")
            ]
        )
        success = self.printer_ppla.dispatch_outer_box_label(box)
        self.assertTrue(success)

        last_job = self.printer_ppla.simulated_print_history[-1]
        self.assertEqual(last_job["job_info"]["emulation"], "PPLA")
        payload = last_job["job_info"]["payload_preview"]
        self.assertTrue(payload.startswith("\x02L") or "L\r" in payload)
        self.assertIn("Q0002", payload)
        self.assertIn("BOX 1 OF 2", payload)

    def test_printer_engine_fuzzy_match_prd_and_prn(self):
        """Verify printer engine matches file names regardless of case, hyphens, or .prd/.prn."""
        # Exact stem matching .prd
        res_prd = self.printer_pplb._resolve_label_file("cb540a_black")
        self.assertIsNotNone(res_prd)
        self.assertTrue(res_prd.name.endswith(".prd") or res_prd.name.endswith(".prn"))

        # Exact stem matching .prn
        res_prn = self.printer_pplb._resolve_label_file("dell_hcf_283x")
        self.assertIsNotNone(res_prn)
        self.assertEqual(res_prn.name, "Dell_HCF_283x.prn")

        # Partial matching
        res_partial = self.printer_pplb._resolve_label_file("Dell 283x")
        self.assertIsNotNone(res_partial)
        self.assertEqual(res_partial.name, "Dell_HCF_283x.prn")

    def test_resolve_label_file_empty_and_none(self):
        """Verify empty string, whitespace, None, and lone extensions return None."""
        self.assertIsNone(self.printer_pplb._resolve_label_file(""))
        self.assertIsNone(self.printer_pplb._resolve_label_file("   "))
        self.assertIsNone(self.printer_pplb._resolve_label_file(None))
        self.assertIsNone(self.printer_pplb._resolve_label_file(".prd"))
        self.assertIsNone(self.printer_pplb._resolve_label_file(".prn"))

    def test_ppla_emitter_floating_point_coordinates(self):
        """Verify PPLA emitter casts float coordinates to int without ValueError."""
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_text(10.5, 20.7, "FloatText")
        ast.add_barcode_1d(30.2, 40.8, "12345", height=50.6, narrow=2.1, wide=4.2)
        ast.add_barcode_2d(50.4, 60.9, "QRDATA", subtype="QR", cell_size=4.3)
        ast.add_line(10.1, 80.2, 200.7, 4.4)
        ast.add_box(15.2, 90.3, 100.8, 50.5, thickness=2.2)

        ppla_bytes = PPLAEmitter.emit(ast)
        self.assertIsInstance(ppla_bytes, bytes)
        ppla_str = ppla_bytes.decode("utf-8")
        self.assertIn("FloatText", ppla_str)
        self.assertIn("\x02L", ppla_str)

    def test_outer_box_label_none_weight(self):
        """Verify dispatch_outer_box_label cleanly handles None weight_kg with 0.0 KG fallback."""
        box = OuterBoxSheet.model_construct(
            quote_number="QT-NONE-WEIGHT",
            weight_kg=None,
            current_box=1,
            total_boxes=1,
            copies_per_box=1,
            box_contents=[BoxItemContent(model="TEST-ITEM", quantity=1, unit_type="Toner Unit")]
        )
        success = self.printer_pplb.dispatch_outer_box_label(box)
        self.assertTrue(success)
        last_job = self.printer_pplb.simulated_print_history[-1]
        self.assertIn("WEIGHT: 0.0 KG", last_job["job_info"]["payload_preview"])


if __name__ == "__main__":
    unittest.main()
