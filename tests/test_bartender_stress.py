import unittest
import sys
import os
import random
import xml.etree.ElementTree as ET
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    CARTRIDGE_HEIGHT_DOTS,
    OUTER_BOX_WIDTH_DOTS,
    OUTER_BOX_HEIGHT_DOTS,
    FONT_HEIGHT_MAP
)
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.classifier import OuterBoxSheet, BoxItemContent
from simulator.mock_argox import VirtualArgoxPrinter


class TestBarTenderStress(unittest.TestCase):
    """
    Empirical stress testing suite for BarTender template ingestion, format translation,
    BTXML/CSV parsing, PPLA vs PPLB emulation fidelity, and thread concurrency.
    """

    def setUp(self):
        self.label_dir = Path(__file__).resolve().parent.parent / "templates" / "sample_labels"
        self.printer_pplb = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True, emulation="PPLB")
        self.printer_ppla = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True, emulation="PPLA")
        self.virtual_argox = VirtualArgoxPrinter()

    # =========================================================================
    # 1. BarTender File Format Parsing Stress
    # =========================================================================

    def test_btxml_parsing_valid_with_extra_whitespace(self):
        """Verify BTXML parser handles extreme whitespace, indentation, and newlines."""
        xml_content = """
        \t\r\n   <XMLScript Version="2.0">
            <Command Name="Job_Print">
                <Print>
                    <Format>  HP_CB540A.btw  </Format>
                    <RecordSet Name="Data" Type="btTextFile">
                        <Record>
                            <Field Name="ITEM_NAME">   HP CB540A (125A)   </Field>
                            <Field Name="BARCODE">   CB540A   </Field>
                            <Field Name="QUANTITY">   15   </Field>
                            <Field Name="NOTE">   Warehouse Shelf 2   </Field>
                        </Record>
                    </RecordSet>
                </Print>
            </Command>
        </XMLScript>  \r\n\t
        """
        res = parse_btxml(xml_content)
        self.assertEqual(res["format_name"], "HP_CB540A.btw")
        self.assertEqual(len(res["records"]), 1)
        rec = res["records"][0]
        self.assertEqual(rec["ITEM_NAME"], "HP CB540A (125A)")
        self.assertEqual(rec["BARCODE"], "CB540A")
        self.assertEqual(rec["QUANTITY"], "15")
        self.assertEqual(rec["NOTE"], "Warehouse Shelf 2")

    def test_btxml_parsing_malformed_xml_errors(self):
        """Verify malformed XML triggers ET.ParseError cleanly without crash or hang."""
        # 1. Missing closing tags
        with self.assertRaises(ET.ParseError):
            parse_btxml("<XMLScript><Print><RecordSet><Record>")

        # 2. Broken attributes
        with self.assertRaises(ET.ParseError):
            parse_btxml('<Label Width="800 Height="400"><Text X="50">')

        # 3. Truncated XML
        with self.assertRaises(ET.ParseError):
            parse_btxml("<?xml version='1.0'?><XMLScript><")

        # 4. Completely empty string or non-XML text
        with self.assertRaises(ET.ParseError):
            parse_btxml("")

        with self.assertRaises(ET.ParseError):
            parse_btxml("PLAIN_NON_XML_TEXT_PAYLOAD")

    def test_btxml_label_ast_with_mm_and_in_units(self):
        """Verify BTXML <Label> parsing with metric (mm) and imperial (in) dimensions."""
        xml_mm = """<Label Width="100mm" Height="50mm">
            <Text X="50" Y="30" Content="Metric Label" Font="4"/>
            <Barcode X="50" Y="100" Content="123456" Type="Code128" Height="50"/>
            <Line X="50" Y="160" Width="700" Height="4"/>
        </Label>"""
        res_mm = parse_btxml(xml_mm)
        ast_mm = res_mm["ast"]
        self.assertIsNotNone(ast_mm)
        self.assertEqual(ast_mm.width_dots, 800)  # 100mm * 8 = 800
        self.assertEqual(ast_mm.height_dots, 400) # 50mm * 8 = 400
        self.assertEqual(len(ast_mm.elements), 3)

        xml_in = """<Label Width="4in" Height="6in">
            <Text X="50" Y="50" Content="Imperial Label" Font="3"/>
        </Label>"""
        res_in = parse_btxml(xml_in)
        ast_in = res_in["ast"]
        self.assertIsNotNone(ast_in)
        self.assertEqual(ast_in.width_dots, 813)   # 4in * 203.2 = 812.8 -> 813
        self.assertEqual(ast_in.height_dots, 1219) # 6in * 203.2 = 1219.2 -> 1219

    def test_csv_parsing_missing_headers(self):
        """Verify CSV parsing with missing header row or single row."""
        # Empty CSV content
        rows = parse_csv_integration("")
        self.assertEqual(rows, [])

        # Whitespace-only CSV content
        rows_ws = parse_csv_integration("   \n\t  \r\n")
        self.assertEqual(rows_ws, [])

        # Single row (becomes fieldnames header with no data rows)
        rows_single = parse_csv_integration("HP CB540A,CB540A,15,Urgent")
        self.assertEqual(rows_single, [])

    def test_csv_parsing_quoted_commas_and_escapes(self):
        """Verify CSV integration handles quoted fields containing commas and double quotes."""
        csv_content = (
            'ITEM_NAME,BARCODE,QUANTITY,NOTE\n'
            '"HP LaserJet, Pro 400",CB540A,15,"Warehouse Shelf, Row B"\n'
            '"Canon ""Extra"", High",CRG-716,20,"Note with ""quotes"" and , commas"\n'
        )
        rows = parse_csv_integration(csv_content)
        self.assertEqual(len(rows), 2)

        self.assertEqual(rows[0]["ITEM_NAME"], "HP LaserJet, Pro 400")
        self.assertEqual(rows[0]["BARCODE"], "CB540A")
        self.assertEqual(rows[0]["QUANTITY"], "15")
        self.assertEqual(rows[0]["NOTE"], "Warehouse Shelf, Row B")

        self.assertEqual(rows[1]["ITEM_NAME"], 'Canon "Extra", High')
        self.assertEqual(rows[1]["BARCODE"], "CRG-716")
        self.assertEqual(rows[1]["NOTE"], 'Note with "quotes" and , commas')

    def test_csv_parsing_irregular_commas(self):
        """Verify CSV integration handles rows with too many or too few commas without throwing unhandled exceptions."""
        csv_content = (
            'ITEM_NAME,BARCODE,QUANTITY,NOTE\n'
            'HP CB540A,CB540A\n'  # Missing columns
            'CANON 716,CRG-716,25,Urgent,EXTRA_COL_1,EXTRA_COL_2\n'  # Extra columns
        )
        rows = parse_csv_integration(csv_content)
        self.assertEqual(len(rows), 2)

        # Row 1: missing columns filled with None
        self.assertEqual(rows[0]["ITEM_NAME"], "HP CB540A")
        self.assertEqual(rows[0]["BARCODE"], "CB540A")
        self.assertIsNone(rows[0]["QUANTITY"])
        self.assertIsNone(rows[0]["NOTE"])

        # Row 2: extra columns placed in None key
        self.assertEqual(rows[1]["ITEM_NAME"], "CANON 716")
        self.assertEqual(rows[1]["QUANTITY"], "25")
        self.assertIn(None, rows[1])
        self.assertEqual(rows[1][None], ["EXTRA_COL_1", "EXTRA_COL_2"])

    def test_parameterized_template_missing_variables(self):
        """Verify template with missing variables leaves placeholders intact without crashing."""
        template = 'A50,30,0,4,1,1,N,"{ITEM_NAME}"\nB50,100,0,1,2,5,60,B,"{BARCODE}"\nP{QUANTITY}\n'
        # Provide only ITEM_NAME
        res = substitute_placeholders(template, {"ITEM_NAME": "HP CB540A"})
        self.assertIn('"HP CB540A"', res)
        self.assertIn('"{BARCODE}"', res)
        self.assertIn('P{QUANTITY}', res)

        # Pass through convert_bartender_to_pplb
        pplb_bytes = convert_bartender_to_pplb(template, {"ITEM_NAME": "HP CB540A"})
        self.assertTrue(isinstance(pplb_bytes, bytes))
        pplb_str = pplb_bytes.decode("utf-8")
        self.assertIn("HP CB540A", pplb_str)

    def test_parameterized_template_extra_variables(self):
        """Verify extra variables not present in template are safely ignored."""
        template = 'A50,30,0,4,1,1,N,"{ITEM_NAME}"\nP1\n'
        variables = {
            "ITEM_NAME": "Standard Toner",
            "EXTRA_KEY_1": "IgnoreMe",
            "UNKNOWN_PARAM": "AlsoIgnore",
            "BATCH_FLAG": True
        }
        res = substitute_placeholders(template, variables)
        self.assertEqual(res, 'A50,30,0,4,1,1,N,"Standard Toner"\nP1\n')

    def test_parameterized_template_circular_references(self):
        """Verify circular and self-referencing variable dictionaries terminate without infinite loops."""
        template = 'A50,30,0,4,1,1,N,"{A}"\nP1\n'
        # Circular substitution: A -> {B}, B -> {C}, C -> {A}
        variables = {"A": "{B}", "B": "{C}", "C": "{A}"}
        res = substitute_placeholders(template, variables)
        self.assertIsInstance(res, str)
        # Should terminate in a single pass without hanging
        self.assertTrue(len(res) > 0)

        # Self-referencing: A -> {A}
        self_ref = {"A": "{A}"}
        res_self = substitute_placeholders(template, self_ref)
        self.assertIn('{A}', res_self)

    def test_parameterized_template_none_and_numeric_types(self):
        """Verify None, boolean, integer, and float variables are safely stringified."""
        template = 'A50,30,0,4,1,1,N,"{STR_VAL}|{INT_VAL}|{FLOAT_VAL}|{BOOL_VAL}|{NONE_VAL}"\nP1\n'
        variables = {
            "STR_VAL": "Cartridge",
            "INT_VAL": 120,
            "FLOAT_VAL": 3.1415,
            "BOOL_VAL": False,
            "NONE_VAL": None
        }
        res = substitute_placeholders(template, variables)
        self.assertIn('"Cartridge|120|3.1415|False|None"', res)

    def test_sanitization_defense_against_command_injection(self):
        """Verify string sanitization prevents rogue printer command injection."""
        malicious_note = 'Normal Note"\nP999\nA50,50,0,4,1,1,N,"ROGUE PRINT COMMAND'
        sanitized = sanitize_text(malicious_note)
        # Newlines flattened to spaces, double quotes to single quotes
        self.assertNotIn("\n", sanitized)
        self.assertNotIn("\r", sanitized)
        self.assertNotIn('"', sanitized)
        self.assertIn("'", sanitized)

        # In barcode data
        malicious_barcode = '12345"\nP999\nB50,50,0,1,2,5,60,B,"FAKE'
        sanitized_bc = sanitize_barcode_data(malicious_barcode)
        self.assertNotIn("\n", sanitized_bc)
        self.assertNotIn("\r", sanitized_bc)
        self.assertNotIn('"', sanitized_bc)
        self.assertEqual(sanitized_bc, "12345P999B50,50,0,1,2,5,60,B,FAKE")

    # =========================================================================
    # 2. Dual-Target Compiler Emulation Validation
    # =========================================================================

    def test_pplb_virtual_argox_interpreter_fidelity(self):
        """Verify that PPLB output across all sample templates parses cleanly in VirtualArgoxPrinter."""
        sample_files = [
            "CB540A_BLACK.prd",
            "CANON_716_BLACK.prd",
            "CB540A_BLACK.prn",
            "CANON_716_BLACK.prn",
            "CB541A_CYAN.prn",
            "DR3115_DRUM.prn",
            "Dell_HCF_283x.prn"
        ]

        for fname in sample_files:
            file_path = self.label_dir / fname
            if not file_path.exists():
                continue
            with self.subTest(file=fname):
                # Test with quantity=7
                pplb_bytes = convert_bartender_to_pplb(str(file_path), {"QUANTITY": 7})
                label_info = self.virtual_argox.process_pplb(pplb_bytes)
                self.assertIsInstance(label_info, dict)
                self.assertEqual(label_info["quantity"], 7, f"Quantity mismatch in {fname}")
                self.assertTrue(len(label_info["text_elements"]) >= 1, f"Missing text in {fname}")
                self.assertTrue(len(label_info["barcodes"]) >= 1, f"Missing barcodes in {fname}")

    def test_pplb_quantity_injection_ranges(self):
        """Verify PPLB quantity injection across single-digit, 2-digit, 3-digit, and 4-digit quantities."""
        raw_pplb = "N\nq800\nQ400,24\nA50,50,0,3,1,1,N,\"TEST\"\nP1\n"
        for test_qty in (1, 5, 25, 250, 1500):
            with self.subTest(quantity=test_qty):
                injected = inject_quantity(raw_pplb, test_qty, emulation="PPLB")
                parsed = self.virtual_argox.process_pplb(injected)
                self.assertEqual(parsed["quantity"], test_qty)

    def test_ppla_datamax_structure_compliance(self):
        """Verify PPLA output strictly adheres to Datamax DPL structure (<STX>L, D11, Q<N:04d>, E)."""
        ast = LabelAST(width_dots=800, height_dots=400, density=8, speed=2, copies=4)
        ast.add_text(x=50, y=30, content="DATAMAX PPLA TEST", font=4, h_mult=1, v_mult=1)
        ast.add_barcode_1d(x=50, y=140, content="DPL12345", barcode_type="1", height=60)
        ast.add_line(x=50, y=250, width=700, height=4)

        ppla_bytes = PPLAEmitter.emit(ast)
        ppla_str = ppla_bytes.decode("utf-8")

        # 1. Starts with <STX>L (\x02L)
        self.assertTrue(ppla_str.startswith("\x02L\r"), "PPLA must start with <STX>L\\r")

        # 2. Darkness & speed line
        self.assertIn("\rD82\r", ppla_str, "PPLA must contain D<density><speed> command")

        # 3. Delimited by carriage return \r
        lines = [l for l in ppla_str.split("\r") if l]

        # 4. Ends with Q<copies:04d> and E
        self.assertEqual(lines[-2], "Q0004", "PPLA quantity must be formatted as Q<copies:04d>")
        self.assertEqual(lines[-1], "E", "PPLA must terminate with 'E'")

    def test_ppla_inverted_y_axis_calculation(self):
        """Verify PPLA bottom-left coordinate transformation: Y_PPLA = Height - Y_PPLB - ElementHeight."""
        label_height = 400
        ast = LabelAST(width_dots=800, height_dots=label_height, copies=1)

        # Text Element: y=30, font=4 (base height 24, v_mult=1 -> font_h=24)
        # Expected Y_PPLA = 400 - 30 - 24 = 346
        ast.add_text(x=50, y=30, content="HEADING", font=4, h_mult=1, v_mult=1)

        # Text Element: y=85, font=3 (base height 20, v_mult=2 -> font_h=40)
        # Expected Y_PPLA = 400 - 85 - 40 = 275
        ast.add_text(x=50, y=85, content="SUBHEAD", font=3, h_mult=1, v_mult=2)

        # Barcode 1D: y=140, height=60
        # Expected Y_PPLA = 400 - 140 - 60 = 200
        ast.add_barcode_1d(x=50, y=140, content="BAR123", height=60)

        # Line: y=250, height=4
        # Expected Y_PPLA = 400 - 250 - 4 = 146
        ast.add_line(x=50, y=250, width=700, height=4)

        ppla_str = PPLAEmitter.emit(ast).decode("utf-8")

        # Format of text command: R t v h 000 yyyy xxxx [text]
        # Text 1: y=0346, x=0050
        self.assertIn("00003460050HEADING", ppla_str)
        # Text 2: y=0275, x=0050
        self.assertIn("00002750050SUBHEAD", ppla_str)
        # Barcode 1: y=0200, x=0050
        self.assertIn("06002000050BAR123", ppla_str)
        # Line 1: y=0146, x=0050
        self.assertIn("00001460050b0700h0004", ppla_str)

    def test_ppla_quantity_injection(self):
        """Verify dynamic quantity injection into existing PPLA byte stream."""
        raw_ppla = "\x02L\rD82\r141100003460050Test Item\rQ0001\rE\r"
        for qty in (1, 12, 99, 450, 9999):
            with self.subTest(qty=qty):
                injected = inject_quantity(raw_ppla, qty, emulation="PPLA")
                injected_str = injected.decode("utf-8")
                self.assertIn(f"Q{qty:04d}", injected_str)
                self.assertTrue(injected_str.startswith("\x02L"))
                self.assertTrue(injected_str.endswith("E\r"))

    def test_outer_box_dispatch_pplb_and_ppla_fidelity(self):
        """Verify outer box label generation compiles cleanly to both PPLB and PPLA."""
        box_data = OuterBoxSheet(
            quote_number="Q-2026-9901",
            current_box=1,
            total_boxes=3,
            weight_kg="14.2",
            copies_per_box=2,
            box_contents=[
                BoxItemContent(unit_type="Toner Unit", model="HP CB540A Black", quantity=6),
                BoxItemContent(unit_type="Toner Unit", model="HP CB541A Cyan", quantity=4),
                BoxItemContent(unit_type="Drum Unit", model="Brother DR3115 Drum", quantity=2)
            ]
        )

        # 1. PPLB Dispatch
        self.printer_pplb.simulated_print_history.clear()
        ok_pplb = self.printer_pplb.dispatch_outer_box_label(box_data)
        self.assertTrue(ok_pplb)
        last_pplb = self.printer_pplb.simulated_print_history[-1]
        self.assertEqual(last_pplb["job_info"]["emulation"], "PPLB")
        self.assertEqual(last_pplb["job_info"]["copies"], 2)

        # Virtual Argox PPLB check
        info = self.virtual_argox.process_pplb(last_pplb["data"].encode("utf-8"))
        self.assertEqual(info["quantity"], 2)
        texts = [t["text"] for t in info["text_elements"]]
        self.assertIn("BOX 1 OF 3", texts)
        self.assertIn("6x Toner Unit (HP CB540A Black)", texts)
        self.assertIn("QUOTE: Q-2026-9901", texts)

        # 2. PPLA Dispatch
        self.printer_ppla.simulated_print_history.clear()
        ok_ppla = self.printer_ppla.dispatch_outer_box_label(box_data)
        self.assertTrue(ok_ppla)
        last_ppla = self.printer_ppla.simulated_print_history[-1]
        self.assertEqual(last_ppla["job_info"]["emulation"], "PPLA")
        ppla_str = last_ppla["data"]
        self.assertTrue(ppla_str.startswith("\x02L\r"))
        self.assertIn("Q0002", ppla_str)
        self.assertTrue(ppla_str.endswith("E\r"))

    # =========================================================================
    # 3. Concurrency & Race Condition Stress
    # =========================================================================

    def test_concurrency_100_parallel_conversions(self):
        """Stress test: 100 concurrent conversions across threads to detect state corruption."""
        templates = [
            str(self.label_dir / "CB540A_BLACK.prd"),
            str(self.label_dir / "CANON_716_BLACK.prd"),
            str(self.label_dir / "CARTRIDGE_TEMPLATE_PARAMETRIC.prd"),
            str(self.label_dir / "OUTER_BOX_TEMPLATE.prd")
        ]

        def worker_task(task_id: int):
            tpl = templates[task_id % len(templates)]
            qty = (task_id % 50) + 1
            emu = "PPLA" if (task_id % 2 == 0) else "PPLB"
            vars_dict = {
                "ITEM_NAME": f"CONCURRENT_ITEM_{task_id}",
                "BARCODE": f"BC{task_id:05d}",
                "QUANTITY": qty,
                "NOTE": f"ThreadWorker-{task_id}",
                "CURRENT_BOX": (task_id % 5) + 1,
                "TOTAL_BOXES": 5,
                "BOX_ITEMS": f"{qty}x Model-{task_id}",
                "QUOTE_NUMBER": f"Q-{task_id:04d}",
                "WEIGHT_KG": f"{qty * 0.5:.1f}",
                "COPIES": qty
            }

            if emu == "PPLB":
                output = convert_bartender_to_pplb(tpl, vars_dict)
                self.assertIsInstance(output, bytes)
                text = output.decode("utf-8")
                self.assertIn(f"P{qty}", text)
                if "CARTRIDGE_TEMPLATE_PARAMETRIC" in tpl:
                    self.assertIn(f"CONCURRENT_ITEM_{task_id}", text)
                    self.assertIn(f"BC{task_id:05d}", text)
            else:
                output = convert_bartender_to_ppla(tpl, vars_dict)
                self.assertIsInstance(output, bytes)
                text = output.decode("utf-8")
                self.assertTrue(text.startswith("\x02L"))
                self.assertIn(f"Q{qty:04d}", text)
                self.assertTrue(text.endswith("E\r"))

            return task_id, len(output)

        results = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(worker_task, i) for i in range(100)]
            for fut in as_completed(futures):
                results.append(fut.result())

        self.assertEqual(len(results), 100, "All 100 concurrent conversions must complete successfully")

    def test_concurrency_100_parallel_printer_dispatches(self):
        """Stress test: 100 concurrent dispatches to printer engine in simulation mode."""
        engine = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True, emulation="PPLB")

        def dispatch_task(task_id: int):
            qty = (task_id % 20) + 1
            if task_id % 2 == 0:
                success = engine.dispatch_cartridge_label(
                    "CB540A_BLACK.prd",
                    quantity=qty,
                    variables={"QUANTITY": qty}
                )
            else:
                box = OuterBoxSheet(
                    quote_number=f"Q-CONCUR-{task_id}",
                    current_box=1,
                    total_boxes=2,
                    weight_kg="10.0",
                    copies_per_box=qty,
                    box_contents=[BoxItemContent(unit_type="Toner Unit", model=f"Model-{task_id}", quantity=qty)]
                )
                success = engine.dispatch_outer_box_label(box)
            return task_id, success

        results = []
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(dispatch_task, i) for i in range(100)]
            for fut in as_completed(futures):
                results.append(fut.result())

        self.assertEqual(len(results), 100)
        self.assertTrue(all(r[1] for r in results), "All 100 dispatches must report success")
        self.assertEqual(len(engine.simulated_print_history), 100, "History must record exactly 100 jobs")

    def test_2d_barcode_emission_fidelity_pplb_and_ppla(self):
        """Verify QR code and DataMatrix 2D barcode emitters for both PPLB and PPLA."""
        ast = LabelAST(width_dots=800, height_dots=400, copies=1)
        ast.add_barcode_2d(x=50, y=50, content="HTTPS://ARGOX.COM/QR", subtype="QR", model=2, cell_size=5, ecc="H")
        ast.add_barcode_2d(x=400, y=50, content="DM-SERIAL-9988", subtype="DATAMATRIX", cols=24, rows=24)

        # 1. PPLB Output
        pplb_bytes = PPLBEmitter.emit(ast)
        pplb_str = pplb_bytes.decode("utf-8")
        self.assertIn('b50,50,Q,s2,m5,rH,"HTTPS://ARGOX.COM/QR"', pplb_str)
        self.assertIn('b400,50,D,c24,r24,"DM-SERIAL-9988"', pplb_str)

        # Virtual Argox Printer should not crash on 'b' commands
        info = self.virtual_argox.process_pplb(pplb_bytes)
        self.assertEqual(info["quantity"], 1)

        # 2. PPLA Output
        ppla_bytes = PPLAEmitter.emit(ast)
        ppla_str = ppla_bytes.decode("utf-8")
        # QR in PPLA: W1Q<ecc><cell_size><yyyy><xxxx><data>
        # b2d_size = 5 * 25 = 125, y_ppla = 400 - 50 - 125 = 225
        self.assertIn("W1QH502250050HTTPS://ARGOX.COM/QR", ppla_str)
        # DataMatrix in PPLA: W1D<rows:02d><cols:02d><yyyy><xxxx><data>
        # cell_size=4 (default) * 25 = 100, y_ppla = 400 - 50 - 100 = 250
        self.assertIn("W1D242402500400DM-SERIAL-9988", ppla_str)

    def test_ppla_coordinate_clamping_on_overflow(self):
        """Verify PPLA coordinate calculation never produces negative Y coordinates if element overflows."""
        ast = LabelAST(width_dots=800, height_dots=400, copies=1)
        # Element y=390 with font height=24: 400 - 390 - 24 = -14 -> must clamp to 0
        ast.add_text(x=50, y=390, content="BOTTOM_EDGE", font=4)
        ppla_str = PPLAEmitter.emit(ast).decode("utf-8")
        self.assertIn("00000000050BOTTOM_EDGE", ppla_str)

    def test_parameterized_template_nested_braces(self):
        """Verify template with double or nested braces does not corrupt parser."""
        template = 'A50,50,0,3,1,1,N,"{{ITEM_NAME}}"\nP1\n'
        res = substitute_placeholders(template, {"ITEM_NAME": "Toner"})
        # Should substitute {ITEM_NAME} -> Toner, resulting in "{Toner}"
        self.assertIn('"{Toner}"', res)

    def test_concurrency_200_mixed_payload_stress(self):
        """Extreme concurrency: 200 parallel tasks with mixed BTXML, JSON AST, and template conversions."""
        btxml_sample = """<XMLScript Version="2.0">
            <Command Name="Job">
                <Print>
                    <RecordSet>
                        <Record>
                            <Field Name="ITEM_NAME">BTXML_CONCUR_ITEM</Field>
                            <Field Name="BARCODE">BTXML_BC</Field>
                            <Field Name="QUANTITY">3</Field>
                        </Record>
                    </RecordSet>
                </Print>
            </Command>
        </XMLScript>"""

        json_ast_sample = '{"width_dots": 800, "height_dots": 400, "copies": 2, "elements": [{"type": "text", "x": 50, "y": 30, "content": "JSON AST TEST"}]}'

        def mixed_task(task_id: int):
            mode = task_id % 4
            if mode == 0:
                out = convert_bartender_to_pplb(btxml_sample)
                self.assertIn(b"BTXML_CONCUR_ITEM", out)
            elif mode == 1:
                out = convert_bartender_to_ppla(btxml_sample)
                self.assertTrue(out.startswith(b"\x02L"))
            elif mode == 2:
                out = convert_bartender_to_pplb(json_ast_sample)
                self.assertIn(b"JSON AST TEST", out)
            else:
                out = convert_bartender_to_ppla(json_ast_sample)
                self.assertTrue(out.startswith(b"\x02L"))
            return task_id

        results = []
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(mixed_task, i) for i in range(200)]
            for fut in as_completed(futures):
                results.append(fut.result())

        self.assertEqual(len(results), 200, "All 200 mixed concurrent conversions must succeed")


if __name__ == "__main__":
    unittest.main()
