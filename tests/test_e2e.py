import unittest
import time
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daemon.printer_engine import ArgoxPrinterEngine
from daemon.classifier import OuterBoxSheet, BoxItemContent, CartridgeItem
from daemon.color_queue import ColorQueueManager
from daemon.gemini_pool import GeminiKeyPoolManager, KeyState
from simulator.mock_argox import VirtualArgoxPrinter

class TestAutoLabelEngine(unittest.TestCase):
    def setUp(self):
        self.label_dir = Path(__file__).resolve().parent.parent / "templates" / "sample_labels"
        self.printer = ArgoxPrinterEngine(label_dir=self.label_dir, simulation_mode=True)
        self.virtual_argox = VirtualArgoxPrinter()

    def test_pplb_quantity_injection_batch(self):
        """Verify pre-made .prn files have P1 dynamically replaced with requested batch quantity (e.g. 30)."""
        success = self.printer.dispatch_cartridge_label("Dell_HCF_283x.prn", quantity=30)
        self.assertTrue(success)

        last_job = self.printer.simulated_print_history[-1]
        self.assertEqual(last_job["job_info"]["quantity"], 30)
        
        # Verify Virtual Argox parsed P30
        label_info = self.virtual_argox.process_pplb(last_job["data"].encode("utf-8"))
        self.assertEqual(label_info["quantity"], 30)
        self.assertTrue(any("DELL HCF 283X" in t["text"] for t in label_info["text_elements"]))

    def test_outer_box_dynamic_generation_p2(self):
        """Verify outer box label dynamically formats Toner & Drum lines and sets P2 (front & back)."""
        box = OuterBoxSheet(
            quote_number="QT-98124",
            weight_kg="12.4",
            current_box=1,
            total_boxes=3,
            copies_per_box=2,
            box_contents=[
                BoxItemContent(quantity=5, unit_type="Toner Unit", model="CB540A Black"),
                BoxItemContent(quantity=2, unit_type="Drum Unit", model="DR3115")
            ]
        )

        success = self.printer.dispatch_outer_box_label(box)
        self.assertTrue(success)

        last_job = self.printer.simulated_print_history[-1]
        self.assertEqual(last_job["job_info"]["copies"], 2)

        label_info = self.virtual_argox.process_pplb(last_job["data"].encode("utf-8"))
        self.assertEqual(label_info["quantity"], 2)
        
        # Check text lines
        texts = [t["text"] for t in label_info["text_elements"]]
        self.assertIn("BOX 1 OF 3", texts)
        self.assertIn("5x Toner Unit (CB540A Black)", texts)
        self.assertIn("2x Drum Unit (DR3115)", texts)
        self.assertIn("QUOTE: QT-98124", texts)
        self.assertIn("WEIGHT: 12.4 KG", texts)

    def test_color_queue_grouping(self):
        """Verify queue manager groups cartridge labels by color."""
        queue = ColorQueueManager()
        
        # Add 3 black items, 1 cyan item
        queue.add_cartridge_item("QT-101", CartridgeItem(item_name="CB540A", color_group="BLACK", quantity=10))
        queue.add_cartridge_item("QT-101", CartridgeItem(item_name="Dell 283x", color_group="BLACK", quantity=20))
        queue.add_cartridge_item("QT-101", CartridgeItem(item_name="CB541A", color_group="CYAN", quantity=5))

        summary = queue.get_summary()
        self.assertEqual(summary["BLACK"], 30)
        self.assertEqual(summary["CYAN"], 5)
        self.assertEqual(summary["MAGENTA"], 0)

        # Mark black printed
        queue.mark_color_printed("BLACK")
        summary_after = queue.get_summary()
        self.assertEqual(summary_after["BLACK"], 0)
        self.assertEqual(summary_after["CYAN"], 5)

    def test_gemini_key_pool_failover(self):
        """Verify key pool marks 429 rate-limited keys as exhausted and fails over to backup key."""
        pool = GeminiKeyPoolManager(keys=["KEY_PRIMARY_TEST", "KEY_BACKUP_TEST"])
        self.assertEqual(len(pool.keys), 2)

        # Simulate primary key hitting 429
        primary_key = pool.keys[0]
        primary_key.mark_exhausted(cooldown_seconds=10.0)

        # Next available key should be backup
        next_key = pool._get_next_available_key()
        self.assertEqual(next_key.key, "KEY_BACKUP_TEST")

if __name__ == "__main__":
    unittest.main()
