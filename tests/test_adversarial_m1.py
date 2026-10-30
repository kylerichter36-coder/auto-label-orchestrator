"""
Adversarial Stress Test Suite for Milestone 1:
- daemon/bartender_converter.py
- daemon/printer_engine.py

Stress-testing focus areas:
1. Coordinate Extremes & Boundary Stress
2. String Sanitization & Escape Attack Vectors
3. Quantity Injection Stress
4. Code 128 / QR / DataMatrix Payload Limits
5. Printer Engine Integration & Edge-Case Robustness
"""

import sys
import unittest
from pathlib import Path
from typing import Dict, Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from daemon.bartender_converter import (
    CoordinateEngine,
    sanitize_text,
    sanitize_barcode_data,
    substitute_placeholders,
    inject_quantity,
    LabelAST,
    TextElement,
    Barcode1DElement,
    Barcode2DElement,
    LineElement,
    BoxElement,
    PPLBEmitter,
    PPLAEmitter,
    render_intermediate_ast,
    convert_bartender_to_pplb,
    convert_bartender_to_ppla,
    parse_pplb_to_ast,
    parse_btxml,
    CARTRIDGE_WIDTH_DOTS,
    CARTRIDGE_HEIGHT_DOTS,
    OUTER_BOX_WIDTH_DOTS,
    OUTER_BOX_HEIGHT_DOTS,
    MAX_PRINTHEAD_WIDTH_DOTS
)
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.classifier import OuterBoxSheet, BoxItemContent
from simulator.mock_argox import VirtualArgoxPrinter


class TestCoordinateAndBoundaryStress(unittest.TestCase):
    """Stress tests for coordinates, scaling, and boundary validation."""

    def test_coordinate_extremes_zero(self):
        """Coordinate (0, 0) should be accepted and correctly emitted."""
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_text(x=0, y=0, content="TOP_LEFT")
        ast.add_barcode_1d(x=0, y=50, content="ZERO-128")
        ast.add_barcode_2d(x=0, y=150, content="ZERO-QR", subtype="QR")
        ast.add_line(x=0, y=250, width=500, height=4)
        ast.add_box(x=0, y=300, width=400, height=80, thickness=2)

        # Bounds validation should pass
        warnings = ast.validate_bounds(strict=False)
        self.assertEqual(len(warnings), 0)

        # PPLB emission
        pplb = PPLBEmitter.emit(ast).decode("utf-8")
        self.assertIn('A0,0,0,3,1,1,N,"TOP_LEFT"', pplb)
        self.assertIn('B0,50,0,1,2,5,60,B,"ZERO-128"', pplb)
        self.assertIn('b0,150,Q,s2,m4,rM,"ZERO-QR"', pplb)
        self.assertIn("LO0,250,500,4", pplb)
        self.assertIn("X0,300,2,400,80", pplb)

        # PPLA emission
        ppla = PPLAEmitter.emit(ast).decode("utf-8")
        self.assertIn("\x02L", ppla)
        self.assertIn("TOP_LEFT", ppla)

    def test_coordinate_extremes_exact_boundaries(self):
        """Coordinates exactly at cartridge (800, 400) and outer box (800, 1200) limits."""
        # Exact cartridge limits
        valid_cart, msg_cart = CoordinateEngine.validate_bounds(
            x=800, y=400, width=0, height=0,
            max_width=CARTRIDGE_WIDTH_DOTS, max_height=CARTRIDGE_HEIGHT_DOTS
        )
        self.assertTrue(valid_cart, f"Exact cartridge boundary failed: {msg_cart}")

        # Exact outer box limits
        valid_box, msg_box = CoordinateEngine.validate_bounds(
            x=800, y=1200, width=0, height=0,
            max_width=OUTER_BOX_WIDTH_DOTS, max_height=OUTER_BOX_HEIGHT_DOTS
        )
        self.assertTrue(valid_box, f"Exact outer box boundary failed: {msg_box}")

    def test_coordinate_exceeding_boundaries(self):
        """Coordinates exceeding boundaries should fail validation."""
        # 1 dot beyond cartridge width
        valid, msg = CoordinateEngine.validate_bounds(x=801, y=200, max_width=800, max_height=400)
        self.assertFalse(valid)
        self.assertIn("exceeds max width", msg)

        # 1 dot beyond printhead physical max width (832)
        valid_head, msg_head = CoordinateEngine.validate_bounds(
            x=833, y=100, max_width=MAX_PRINTHEAD_WIDTH_DOTS, max_height=1200
        )
        self.assertFalse(valid_head)
        self.assertIn("exceeds max width", msg_head)

        # 1 dot beyond outer box height
        valid_h, msg_h = CoordinateEngine.validate_bounds(x=100, y=1201, max_width=800, max_height=1200)
        self.assertFalse(valid_h)
        self.assertIn("exceeds max height", msg_h)

    def test_negative_coordinates(self):
        """Negative coordinates must be rejected by CoordinateEngine."""
        valid_neg_x, msg_neg_x = CoordinateEngine.validate_bounds(x=-1, y=50)
        self.assertFalse(valid_neg_x)
        self.assertIn("cannot be negative", msg_neg_x)

        valid_neg_y, msg_neg_y = CoordinateEngine.validate_bounds(x=50, y=-1)
        self.assertFalse(valid_neg_y)
        self.assertIn("cannot be negative", msg_neg_y)

        valid_both_neg, msg_both = CoordinateEngine.validate_bounds(x=-50, y=-50)
        self.assertFalse(valid_both_neg)
        self.assertIn("cannot be negative", msg_both)

    def test_strict_vs_nonstrict_ast_boundary_validation(self):
        """Test behavior of LabelAST.validate_bounds with strict=True vs strict=False."""
        ast = LabelAST(width_dots=800, height_dots=400)
        # Add box extending beyond label width (700 + 200 = 900 > 800)
        ast.add_box(x=200, y=50, width=700, height=100, thickness=2)

        # Non-strict should return warnings list
        warnings = ast.validate_bounds(strict=False)
        self.assertGreater(len(warnings), 0)
        self.assertIn("exceeds max width", warnings[0])

        # Strict should raise ValueError
        with self.assertRaises(ValueError):
            ast.validate_bounds(strict=True)

    def test_floating_point_coordinates(self):
        """
        Adversarial: Floating point coordinate inputs.
        Investigates whether emitters crash or produce malformed protocol streams
        when float coordinates are provided.
        """
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_text(x=10.5, y=20.7, content="FLOAT_COORD")

        # PPLB emits raw values into format string
        pplb_bytes = PPLBEmitter.emit(ast)
        self.assertIsInstance(pplb_bytes, bytes)
        pplb_str = pplb_bytes.decode("utf-8")
        # Record whether float formatting occurs
        has_float_in_pplb = "10.5" in pplb_str

        # PPLA emitter with float coordinates
        # Coordinates must be cast to int(round(...)) so emission succeeds without ValueError
        ppla_bytes = PPLAEmitter.emit(ast)
        self.assertIsInstance(ppla_bytes, bytes)
        ppla_str = ppla_bytes.decode("utf-8")
        self.assertTrue(ppla_str.startswith("\x02L") or "L\r" in ppla_str)

    def test_text_and_barcode_element_bounds_calculation(self):
        """
        Adversarial: TextElement and Barcode1DElement do not define width attributes.
        Investigate whether validate_bounds detects when long text or barcode overflows width.
        """
        ast = LabelAST(width_dots=800, height_dots=400)
        # Text placed at x=750 with 50 characters (clearly overflows 800 dots)
        ast.add_text(x=750, y=50, content="VERY LONG TEXT EXPANDING WAY PAST THE PRINTHEAD WIDTH")
        # Barcode placed at x=700 with wide content
        ast.add_barcode_1d(x=700, y=150, content="LONG-BARCODE-PAYLOAD-123456789")

        warnings = ast.validate_bounds(strict=False)
        # Because TextElement and Barcode1DElement have width=0 / no width attribute,
        # validate_bounds only checks x <= 800!
        # Both elements have x <= 800, so zero warnings are emitted!
        text_overflow_detected = len(warnings) > 0
        # Empirically record this limitation:
        self.assertFalse(
            text_overflow_detected,
            "Empirical finding: AST validate_bounds relies on element.width which is absent for TextElement and Barcode1DElement"
        )


class TestStringSanitizationAndEscapeVectors(unittest.TestCase):
    """Stress tests for injection attacks, escaping, control characters, and non-ASCII."""

    def test_double_quotes_and_single_quotes(self):
        """Double quotes must be replaced with single quotes; single quotes must be preserved."""
        raw = 'Double "quote" and single \'quote\''
        sanitized = sanitize_text(raw)
        self.assertNotIn('"', sanitized)
        self.assertEqual(sanitized.count("'"), 4)
        self.assertEqual(sanitized, "Double 'quote' and single 'quote'")

    def test_backslashes_and_slashes(self):
        """Backslashes must be normalized to forward slashes to prevent escape sequences."""
        raw = r"C:\Labels\Cartridge\CB540A.prd"
        sanitized = sanitize_text(raw)
        self.assertNotIn("\\", sanitized)
        self.assertEqual(sanitized, "C:/Labels/Cartridge/CB540A.prd")

    def test_crlf_newline_flattening(self):
        """Line breaks (\r\n, \r, \n) must be flattened to spaces to prevent line breaking."""
        raw = "Line1\r\nLine2\nLine3\rLine4"
        sanitized = sanitize_text(raw)
        self.assertNotIn("\r", sanitized)
        self.assertNotIn("\n", sanitized)
        self.assertEqual(sanitized, "Line1  Line2 Line3 Line4")

    def test_command_injection_attack_vectors(self):
        """
        Adversarial: Attempts to inject PPLB commands via user input strings.
        e.g. terminating the quote and injecting a new command: '"; P99; "' or '\r\nP99\r\n'
        """
        attack_vector = 'SAFE"; P999\nA50,50,0,3,1,1,N,"INJECTED'
        sanitized = sanitize_text(attack_vector)
        # Double quote eliminated, newline eliminated
        self.assertNotIn('"', sanitized)
        self.assertNotIn("\n", sanitized)
        self.assertNotIn("\r", sanitized)

        # Now test inside an AST element and PPLB stream
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_text(50, 50, content=attack_vector)
        pplb = PPLBEmitter.emit(ast).decode("utf-8")
        lines = [l.strip() for l in pplb.split("\n") if l.strip()]

        # Verify that P999 was NOT injected as an independent command
        standalone_p999 = any(l == "P999" for l in lines)
        self.assertFalse(standalone_p999, "Command injection succeeded: P999 became a standalone command!")

        # Verify exactly one P command exists (P1)
        p_commands = [l for l in lines if l.startswith("P") and not l.startswith("PART")]
        self.assertEqual(p_commands, ["P1"])

    def test_null_bytes_and_control_characters(self):
        """Null bytes (0x00), Bell (0x07), Escape (0x1b), DEL (0x7f) must be stripped."""
        raw = "Start\x00Middle\x07Alert\x1bEscape\x7fEnd"
        sanitized = sanitize_text(raw)
        self.assertNotIn("\x00", sanitized)
        self.assertNotIn("\x07", sanitized)
        self.assertNotIn("\x1b", sanitized)
        self.assertNotIn("\x7f", sanitized)
        self.assertEqual(sanitized, "StartMiddleAlertEscapeEnd")

    def test_none_and_non_string_types(self):
        """None, integers, floats, dicts passed to sanitization functions."""
        self.assertEqual(sanitize_text(None), "")
        self.assertEqual(sanitize_barcode_data(None), "")
        self.assertEqual(sanitize_text(12345), "12345")
        self.assertEqual(sanitize_barcode_data(98765), "98765")
        self.assertEqual(sanitize_text(45.67), "45.67")

    def test_non_ascii_utf8_handling(self):
        """
        Adversarial: Non-ASCII characters (e.g. accents 'é', umlauts 'ü', currency '€', CJK).
        Investigates how sanitize_text and emitters process multi-byte UTF-8.
        """
        raw_accent = "Cartouche Magenta Modèle Spécial 25€"
        sanitized = sanitize_text(raw_accent)
        # ord(ch) >= 32, so multi-byte UTF-8 is preserved in string
        self.assertEqual(sanitized, raw_accent)

        ast = LabelAST()
        ast.add_text(50, 50, content=raw_accent)
        pplb_bytes = PPLBEmitter.emit(ast)
        # Should cleanly encode to UTF-8 without crashing
        self.assertIsInstance(pplb_bytes, bytes)
        decoded = pplb_bytes.decode("utf-8")
        self.assertIn("Spécial 25€", decoded)

    def test_recursive_placeholder_substitution(self):
        """
        Adversarial: Template variables containing secondary placeholder names.
        e.g. {"ITEM": "{NOTE}", "NOTE": "EVIL_VALUE"}
        """
        template = 'A50,30,0,4,1,1,N,"{ITEM}"\nA50,85,0,3,1,1,N,"{NOTE}"\n'
        variables = {
            "ITEM": "{NOTE}",
            "NOTE": "RESOLVED_NOTE"
        }
        res = substitute_placeholders(template, variables)
        # If dictionary iteration resolves ITEM -> {NOTE}, then NOTE -> RESOLVED_NOTE,
        # ITEM becomes RESOLVED_NOTE as well!
        lines = res.strip().split("\n")
        item_line = lines[0]
        note_line = lines[1]
        self.assertIn("RESOLVED_NOTE", note_line)
        # Document empirical behavior of secondary substitution
        secondary_substituted = "RESOLVED_NOTE" in item_line
        self.assertTrue(
            secondary_substituted,
            "Empirical finding: substitute_placeholders exhibits secondary substitution if var value matches another key"
        )


class TestQuantityInjectionStress(unittest.TestCase):
    """Stress tests for dynamic quantity injection in PPLB and PPLA."""

    def test_quantity_values_zero_one_large(self):
        """Verify quantity injection with 0, 1, and large numbers (99999)."""
        sample_pplb = "N\nq800\nQ400,24\nP1\n"

        # Quantity 1
        q1 = inject_quantity(sample_pplb, 1, emulation="PPLB").decode("utf-8")
        self.assertTrue(q1.endswith("P1\n"))

        # Quantity 0
        q0 = inject_quantity(sample_pplb, 0, emulation="PPLB").decode("utf-8")
        self.assertTrue(q0.endswith("P0\n"))

        # Quantity 99999
        q_large = inject_quantity(sample_pplb, 99999, emulation="PPLB").decode("utf-8")
        self.assertTrue(q_large.endswith("P99999\n"))

    def test_negative_quantity_injection(self):
        """
        Adversarial: Negative quantity values.
        Investigate how inject_quantity handles negative numbers.
        """
        sample_pplb = "N\nq800\nP1\n"
        q_neg = inject_quantity(sample_pplb, -5, emulation="PPLB").decode("utf-8")
        self.assertTrue(q_neg.endswith("P-5\n"))

        # Sequential call after negative quantity:
        # re.match(r'^P\d+', stripped) will FAIL to match P-5 because '-' is not a digit!
        # It will then append P10, resulting in two P commands!
        q_seq = inject_quantity(q_neg, 10, emulation="PPLB").decode("utf-8")
        self.assertIn("P-5\n", q_seq)
        self.assertIn("P10\n", q_seq)

    def test_templates_with_multiple_p_letters(self):
        """
        Critical Requirement: Verify that text like 'HP LaserJet Pro' or barcode 'PART-P9'
        is NOT corrupted by inject_quantity.
        """
        sample_pplb = (
            "N\n"
            "q800\n"
            "Q400,24\n"
            "D8\n"
            "S2\n"
            'A50,30,0,4,1,1,N,"HP LaserJet Pro Enterprise P3015dn"\n'
            'A50,85,0,3,1,1,N,"PHILIPS POWER PACK - MODEL P"\n'
            'B50,140,0,1,2,5,60,B,"PART-P9999-REV-P1"\n'
            'A50,220,0,2,1,1,N,"PACK 50 PIECES"\n'
            "P1\n"
        )
        res = inject_quantity(sample_pplb, 25, emulation="PPLB").decode("utf-8")

        self.assertIn('"HP LaserJet Pro Enterprise P3015dn"', res)
        self.assertIn('"PHILIPS POWER PACK - MODEL P"', res)
        self.assertIn('"PART-P9999-REV-P1"', res)
        self.assertIn('"PACK 50 PIECES"', res)
        self.assertTrue(res.endswith("P25\n"))
        self.assertNotIn("P1\n", res)

    def test_ppla_quantity_injection(self):
        """Verify PPLA Q<count:04d> rewriting with 0, 1, 42, 99999, and negative."""
        sample_ppla = "\x02L\rD11\r121100002000050Hello\rQ0001\rE\r"

        q42 = inject_quantity(sample_ppla, 42, emulation="PPLA").decode("utf-8")
        self.assertIn("Q0042\rE", q42)

        q0 = inject_quantity(sample_ppla, 0, emulation="PPLA").decode("utf-8")
        self.assertIn("Q0000\rE", q0)

        q_large = inject_quantity(sample_ppla, 99999, emulation="PPLA").decode("utf-8")
        self.assertIn("Q99999\rE", q_large)

        # Negative quantity in PPLA: f"{-5:04d}" produces '-005'
        q_neg = inject_quantity(sample_ppla, -5, emulation="PPLA").decode("utf-8")
        self.assertIn("Q-005\rE", q_neg)

    def test_missing_trailing_copy_command_fallback(self):
        """Templates missing a trailing P command should have P<quantity> appended."""
        incomplete_pplb = "N\nq800\nQ400,24\nA50,50,0,3,1,1,N,\"TEST\"\n"
        res = inject_quantity(incomplete_pplb, 8, emulation="PPLB").decode("utf-8")
        self.assertTrue(res.endswith("P8\n"))


class TestBarcodePayloadLimits(unittest.TestCase):
    """Stress tests for linear (Code 128) and 2D (QR, DataMatrix) payloads."""

    def test_long_string_payload_500_chars(self):
        """Stress test with payload > 500 characters."""
        long_payload = "A" * 600

        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_barcode_1d(50, 50, content=long_payload, barcode_type="1")
        ast.add_barcode_2d(50, 150, content=long_payload, subtype="QR")
        ast.add_barcode_2d(50, 250, content=long_payload, subtype="DATAMATRIX")

        # PPLB Emitter should not crash
        pplb = PPLBEmitter.emit(ast).decode("utf-8")
        self.assertIn(long_payload, pplb)

        # PPLA Emitter should not crash
        ppla = PPLAEmitter.emit(ast).decode("utf-8")
        self.assertIn(long_payload, ppla)

    def test_empty_payloads(self):
        """Adversarial: Empty payloads ("") for 1D and 2D barcodes."""
        ast = LabelAST(width_dots=800, height_dots=400)
        ast.add_barcode_1d(50, 50, content="")
        ast.add_barcode_2d(50, 150, content="", subtype="QR")
        ast.add_barcode_2d(50, 250, content="", subtype="DATAMATRIX")

        pplb = PPLBEmitter.emit(ast).decode("utf-8")
        # Empty string should be formatted inside quotes
        self.assertIn('B50,50,0,1,2,5,60,B,""', pplb)
        self.assertIn('b50,150,Q,s2,m4,rM,""', pplb)
        self.assertIn('b50,250,D,c0,r0,""', pplb)

        # Reverse parser should handle empty barcodes without throwing IndexError
        reverse_ast = parse_pplb_to_ast(pplb)
        self.assertEqual(len(reverse_ast.elements), 3)
        self.assertEqual(reverse_ast.elements[0].content, "")

    def test_special_symbols_and_urls_in_2d_payloads(self):
        """URLs containing query parameters (&, ?, =, %, #)."""
        url = "https://wms.enterprise.internal/trace?sku=HP-CB540A&batch=2026%20B9#loc=A-12"
        ast = LabelAST()
        ast.add_barcode_2d(50, 50, content=url, subtype="QR")

        pplb = PPLBEmitter.emit(ast).decode("utf-8")
        self.assertIn(url, pplb)

    def test_json_payload_quote_replacement_behavior(self):
        """
        Adversarial: JSON payload in QR Code.
        Evaluates whether sanitize_text replaces double quotes with single quotes,
        which invalidates JSON per RFC 8259.
        """
        json_payload = '{"order_id": 9981, "carrier": "DHL", "verified": true}'
        ast = LabelAST()
        ast.add_barcode_2d(50, 50, content=json_payload, subtype="QR")

        pplb = PPLBEmitter.emit(ast).decode("utf-8")
        # sanitize_text replaces " with '
        self.assertNotIn('{"order_id":', pplb)
        self.assertIn("{'order_id':", pplb)
        # Note: Valid JSON requires double quotes per RFC 8259. Single quotes are invalid JSON.
        is_rfc8259_compliant = '"' in ast.elements[0].content
        self.assertFalse(
            is_rfc8259_compliant,
            "Empirical finding: sanitize_text transforms valid RFC 8259 JSON double quotes to single quotes"
        )


class TestPrinterEngineAdversarial(unittest.TestCase):
    """Stress tests for ArgoxPrinterEngine robustness and error conditions."""

    def setUp(self):
        self.sample_labels_dir = PROJECT_ROOT / "templates" / "sample_labels"
        self.engine_sim = ArgoxPrinterEngine(
            label_dir=self.sample_labels_dir,
            simulation_mode=True,
            emulation="PPLB"
        )
        self.engine_offline = ArgoxPrinterEngine(
            device_path="/dev/nonexistent_usb_lp_device_999",
            label_dir=self.sample_labels_dir,
            simulation_mode=False,
            emulation="PPLB"
        )

    def test_resolve_label_file_empty_string_and_whitespace(self):
        """
        Adversarial: Passing empty string "" or whitespace "   " to _resolve_label_file.
        Must return None rather than matching first candidate file.
        """
        res_empty = self.engine_sim._resolve_label_file("")
        res_spaces = self.engine_sim._resolve_label_file("   ")

        self.assertIsNone(res_empty, "_resolve_label_file('') must return None")
        self.assertIsNone(res_spaces, "_resolve_label_file('   ') must return None")

    def test_resolve_label_file_none_input(self):
        """
        Adversarial: Passing None to _resolve_label_file.
        Must cleanly return None without raising AttributeError.
        """
        res = self.engine_sim._resolve_label_file(None)
        self.assertIsNone(res, "_resolve_label_file(None) must return None")

    def test_resolve_label_file_directory_traversal(self):
        """Directory traversal attacks should return None."""
        res = self.engine_sim._resolve_label_file("../../../etc/passwd")
        self.assertIsNone(res)

        res2 = self.engine_sim._resolve_label_file("..\\..\\secret.prd")
        self.assertIsNone(res2)

    def test_dispatch_cartridge_label_missing_file(self):
        """Attempting to print a non-existent template returns False."""
        success = self.engine_sim.dispatch_cartridge_label("NON_EXISTENT_LABEL_999.prd", quantity=1)
        self.assertFalse(success)

    def test_dispatch_outer_box_label_none_weight(self):
        """
        Adversarial: OuterBoxSheet with weight_kg=None.
        Must default weight cleanly to 0.0 KG without crashing.
        """
        box = OuterBoxSheet.model_construct(
            quote_number="QT-9900",
            weight_kg=None,  # None instead of string
            current_box=1,
            total_boxes=1,
            copies_per_box=1,
            box_contents=[]
        )
        success = self.engine_sim.dispatch_outer_box_label(box)
        self.assertTrue(success, "dispatch_outer_box_label should succeed when weight_kg is None")

    def test_offline_printer_without_simulation_mode(self):
        """
        Zero Fake Readings Standard: When physical printer node does not exist
        and simulation_mode=False, dispatch must return False and NOT claim success.
        """
        self.assertFalse(self.engine_offline.is_connected())

        # Attempt cartridge dispatch
        success_cart = self.engine_offline.dispatch_cartridge_label("CB540A_BLACK.prd", quantity=1)
        self.assertFalse(success_cart, "Offline printer must fail dispatch, not pretend success!")

        # Attempt test label
        status = self.engine_offline.send_test_label()
        self.assertEqual(status["status"], "offline")
        self.assertFalse(status["connected"])
        self.assertIn("does not exist", status["message"])


if __name__ == "__main__":
    unittest.main()
