import os
import sys
import json
import time
import errno
import shutil
import tempfile
import unittest
import threading
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from daemon.persistence import (
    PersistenceManager,
    get_persistence_manager,
    save_active_queues,
    load_active_queues,
    log_transaction,
    query_transactions,
    create_sdcard_directory_topology,
    prune_snapshots
)
from daemon.color_queue import ColorQueueManager, QueuedPrintJob
from daemon.classifier import CartridgeItem, OuterBoxSheet, BoxItemContent


class TestSDCardDetectionAndFallback(unittest.TestCase):
    """Verifies physical SD card mount detection, topology creation, and fallback mechanisms."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sd_mock = Path(self.temp_dir) / "mnt_sdcard"
        self.fallback_mock = Path(self.temp_dir) / "fallback_storage"
        self.sd_mock.mkdir(parents=True, exist_ok=True)
        self.fallback_mock.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sdcard_present_and_writable(self):
        """Verify storage engine detects writable SD card mount and designates it as storage root."""
        pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_mock)
        self.assertTrue(pm.is_sdcard_mounted())
        self.assertEqual(pm.get_storage_root(), self.sd_mock)
        self.assertEqual(pm.get_queues_file_path(), self.sd_mock / "active_queues.json")

    def test_sdcard_absent_fallback_to_local(self):
        """Verify storage engine falls back gracefully to local storage when SD card is unmounted."""
        nonexistent_sd = Path(self.temp_dir) / "nonexistent_sd"
        pm = PersistenceManager(sd_mount=nonexistent_sd, fallback_dir=self.fallback_mock)
        self.assertFalse(pm.is_sdcard_mounted())
        self.assertEqual(pm.get_storage_root(), self.fallback_mock)
        self.assertEqual(pm.get_queues_file_path(), self.fallback_mock / "active_queues.json")

    def test_sdcard_read_only_erofs_fallback(self):
        """Verify simulated EROFS (Read-only filesystem) triggers fallback to local non-volatile storage."""
        pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_mock)
        
        # Simulate EROFS when attempting to open files on the SD mount
        real_open = open
        def mock_open_erofs(file, *args, **kwargs):
            if str(self.sd_mock) in str(file):
                raise OSError(errno.EROFS, "Read-only file system", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_erofs):
            self.assertFalse(pm.is_sdcard_mounted())
            self.assertEqual(pm.get_storage_root(), self.fallback_mock)

    def test_directory_topology_creation(self):
        """Verify all required subdirectories are created on active storage."""
        pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_mock)
        pm.ensure_topology(self.sd_mock)

        expected_dirs = [
            self.sd_mock / "snapshots",
            self.sd_mock / "audit_logs",
            self.sd_mock / "audit_logs" / "scans",
            self.sd_mock / "audit_logs" / "prints",
            self.sd_mock / "templates_archive",
            self.sd_mock / "templates_archive" / "golden_master",
            self.sd_mock / "templates_archive" / "custom_user_templates",
            self.sd_mock / "rollback_recovery"
        ]
        for d in expected_dirs:
            self.assertTrue(d.exists(), f"Required directory {d} was not created")
            self.assertTrue(d.is_dir())


class TestAtomicQueuePersistence(unittest.TestCase):
    """Verifies atomic writes, schema integrity, and lossless serialization of queue state."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_dir = Path(self.temp_dir) / "storage"
        self.fallback_dir = Path(self.temp_dir) / "fallback"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.fallback_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.storage_dir, fallback_dir=self.fallback_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_atomic_write_creates_valid_file_and_cleans_tmp(self):
        """Verify .tmp file is cleanly replaced and active_queues.json contains valid JSON."""
        queue = ColorQueueManager()
        queue.add_cartridge_item("QT-101", CartridgeItem(item_name="CB540A", color_group="BLACK", quantity=10))

        success = self.pm.save_active_queues(queue)
        self.assertTrue(success)

        target_file = self.pm.get_queues_file_path()
        tmp_file = target_file.parent / f".{target_file.name}.tmp"

        self.assertTrue(target_file.exists())
        self.assertFalse(tmp_file.exists(), "Temporary file must not remain after successful replace")

        with open(target_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["version"], 1)
        self.assertIn("BLACK", data["queues"])
        self.assertEqual(len(data["queues"]["BLACK"]), 1)
        self.assertEqual(data["queues"]["BLACK"][0]["item_name"], "CB540A")

    def test_queue_state_serialization_fidelity(self):
        """Verify multi-color jobs and outer boxes are losslessly restored into a new queue manager."""
        original_queue = ColorQueueManager()
        original_queue.add_cartridge_item("QT-201", CartridgeItem(item_name="CB540A", color_group="BLACK", quantity=15))
        original_queue.add_cartridge_item("QT-201", CartridgeItem(item_name="CB541A", color_group="CYAN", quantity=8))
        original_queue.add_cartridge_item("QT-201", CartridgeItem(item_name="CB542A", color_group="YELLOW", quantity=12))

        box = OuterBoxSheet(
            quote_number="QT-201",
            weight_kg="14.2",
            current_box=1,
            total_boxes=2,
            copies_per_box=2,
            box_contents=[
                BoxItemContent(quantity=15, unit_type="Toner Unit", model="CB540A Black"),
                BoxItemContent(quantity=8, unit_type="Toner Unit", model="CB541A Cyan")
            ]
        )
        original_queue.add_outer_box(box)

        # Save state
        self.pm.save_active_queues(original_queue)

        # Load into new queue manager
        new_queue = ColorQueueManager()
        self.pm.load_active_queues(new_queue)

        # Verify cartridge jobs
        self.assertEqual(len(new_queue.queues["BLACK"]), 1)
        self.assertEqual(new_queue.queues["BLACK"][0].item_name, "CB540A")
        self.assertEqual(new_queue.queues["BLACK"][0].quantity, 15)

        self.assertEqual(len(new_queue.queues["CYAN"]), 1)
        self.assertEqual(new_queue.queues["CYAN"][0].quantity, 8)

        self.assertEqual(len(new_queue.queues["YELLOW"]), 1)
        self.assertEqual(new_queue.queues["YELLOW"][0].quantity, 12)

        # Verify outer box
        self.assertEqual(len(new_queue.pending_outer_boxes), 1)
        restored_box = new_queue.pending_outer_boxes[0]
        self.assertEqual(restored_box.quote_number, "QT-201")
        self.assertEqual(restored_box.weight_kg, "14.2")
        self.assertEqual(len(restored_box.box_contents), 2)
        self.assertEqual(new_queue.job_counter, original_queue.job_counter)

    def test_automatic_queue_persistence_hook(self):
        """Verify ColorQueueManager automatically invokes persistence when attached."""
        queue = ColorQueueManager()
        queue.set_persistence_manager(self.pm)

        # Add item - should trigger save
        queue.add_cartridge_item("QT-303", CartridgeItem(item_name="DR3115", color_group="OTHER", quantity=3))
        
        target_file = self.pm.get_queues_file_path()
        self.assertTrue(target_file.exists())
        with open(target_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["queues"]["OTHER"]), 1)
        self.assertEqual(data["queues"]["OTHER"][0]["item_name"], "DR3115")


class TestPowerLossRecovery(unittest.TestCase):
    """Verifies that abrupt power interruptions do not corrupt state or discard work."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_dir = Path(self.temp_dir) / "storage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.storage_dir, fallback_dir=self.storage_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_interrupted_write_temp_file_ignored(self):
        """Verify truncated .tmp file left by sudden power cut does not corrupt master target."""
        master_file = self.storage_dir / "active_queues.json"
        valid_state = {
            "version": 1,
            "timestamp": time.time(),
            "job_counter": 5,
            "queues": {
                "BLACK": [
                    {
                        "id": "JOB-0001",
                        "quote_number": "QT-999",
                        "item_name": "CE285A",
                        "description": "HP 85A",
                        "quantity": 25,
                        "color_group": "BLACK",
                        "matched_label_file": "CE285A.prn",
                        "status": "PENDING"
                    }
                ],
                "CYAN": [],
                "MAGENTA": [],
                "YELLOW": [],
                "OTHER": []
            },
            "outer_boxes": []
        }
        master_file.write_text(json.dumps(valid_state), encoding="utf-8")

        # Simulate interrupted power loss writing partial temp file
        tmp_file = self.storage_dir / f".{master_file.name}.tmp"
        tmp_file.write_text("PARTIAL_TRUNCATED_JSON_DATA_CORRUPT", encoding="utf-8")

        # Recovery should cleanly load valid master state
        recovered_queue = ColorQueueManager()
        self.pm.load_active_queues(recovered_queue)

        self.assertEqual(len(recovered_queue.queues["BLACK"]), 1)
        self.assertEqual(recovered_queue.queues["BLACK"][0].item_name, "CE285A")
        self.assertEqual(recovered_queue.queues["BLACK"][0].quantity, 25)

    def test_corrupt_master_file_handled_gracefully(self):
        """Verify corrupted master file returns initialized empty state without raising unhandled exception."""
        master_file = self.storage_dir / "active_queues.json"
        master_file.write_text("TOTAL_GARBAGE_NOT_JSON", encoding="utf-8")

        recovered_queue = ColorQueueManager()
        result = self.pm.load_active_queues(recovered_queue)

        self.assertEqual(result["version"], 1)
        self.assertEqual(len(recovered_queue.queues["BLACK"]), 0)


class TestTransactionAuditLoggingAndQueries(unittest.TestCase):
    """Verifies structured JSONL transaction append logging and SQLite query capabilities."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_dir = Path(self.temp_dir) / "storage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.storage_dir, fallback_dir=self.storage_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_transaction_logging_produces_valid_jsonl(self):
        """Verify audit transactions produce strictly valid newline-delimited JSON records."""
        events = [
            ("SCAN_RECEIVED", {"filename": "scan_001.pdf", "size_bytes": 1048576}, None),
            ("OCR_RESULT", {"items_found": 3, "confidence": 0.98}, "QT-8801"),
            ("BARTENDER_CONVERSION", {"format": "PRD", "dots_scaled": 203}, "QT-8801"),
            ("PRINT_DISPATCH", {"model": "CB540A", "quantity": 10}, "QT-8801")
        ]

        for event, details, q_num in events:
            rec = self.pm.log_transaction(event, details, quote_number=q_num)
            self.assertTrue(rec["transaction_id"].startswith("TX-"))
            self.assertEqual(rec["event"], event)

        # Inspect JSONL file
        audit_dir = self.pm.get_audit_dir()
        jsonl_files = list(audit_dir.glob("transactions_*.jsonl"))
        self.assertEqual(len(jsonl_files), 1)

        with open(jsonl_files[0], "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]

        self.assertEqual(len(lines), 4)
        for line in lines:
            parsed = json.loads(line)
            self.assertIn("transaction_id", parsed)
            self.assertIn("iso_timestamp", parsed)
            self.assertIn("details", parsed)

    def test_transaction_query_filtering(self):
        """Verify query_transactions filters by event type and quote number accurately."""
        self.pm.log_transaction("SCAN_RECEIVED", {"file": "doc1.pdf"}, quote_number="QT-1")
        self.pm.log_transaction("PRINT_DISPATCH", {"model": "HP85A", "qty": 10}, quote_number="QT-1")
        self.pm.log_transaction("PRINT_DISPATCH", {"model": "CANON716", "qty": 5}, quote_number="QT-2")

        # Query by quote
        qt1_records = self.pm.query_transactions(quote_number="QT-1")
        self.assertEqual(len(qt1_records), 2)

        # Query by event type
        dispatch_records = self.pm.query_transactions(event_type="PRINT_DISPATCH")
        self.assertEqual(len(dispatch_records), 2)

        # Query by both
        qt2_dispatch = self.pm.query_transactions(event_type="PRINT_DISPATCH", quote_number="QT-2")
        self.assertEqual(len(qt2_dispatch), 1)
        self.assertEqual(qt2_dispatch[0]["details"]["model"], "CANON716")

    def test_thread_safety_concurrent_logging(self):
        """Verify thread-safety when multiple worker threads concurrently append transactions."""
        thread_count = 10
        writes_per_thread = 5

        def worker_task(worker_id: int):
            for i in range(writes_per_thread):
                self.pm.log_transaction(
                    "CONCURRENT_TEST",
                    {"worker": worker_id, "index": i},
                    quote_number=f"QT-W{worker_id}"
                )

        threads = [threading.Thread(target=worker_task, args=(w,)) for w in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify all records exist
        all_records = self.pm.query_transactions(event_type="CONCURRENT_TEST", limit=200)
        self.assertEqual(len(all_records), thread_count * writes_per_thread)


class TestSnapshotPruning(unittest.TestCase):
    """Verifies snapshot retention policy limits archive growth to exactly 5 newest archives."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.snapshot_dir = Path(self.temp_dir) / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=Path(self.temp_dir), fallback_dir=Path(self.temp_dir))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_prune_snapshots_keeps_latest_five(self):
        """Verify creating 8 snapshots and pruning leaves exactly the 5 newest archives."""
        files = []
        for i in range(8):
            f = self.snapshot_dir / f"snapshot_v1.0.{i}_test.tar.gz"
            f.write_text(f"dummy_content_{i}")
            files.append(f)
            time.sleep(0.01)

        deleted = self.pm.prune_snapshots(self.snapshot_dir, keep_count=5)
        self.assertEqual(len(deleted), 3)

        remaining = list(self.snapshot_dir.glob("snapshot_*.tar.gz"))
        self.assertEqual(len(remaining), 5)

        # Confirm the oldest 3 were deleted
        for old in files[:3]:
            self.assertNotIn(old, remaining)
        # Confirm the newest 5 remain
        for new in files[3:]:
            self.assertIn(new, remaining)


class TestStorageSetupScript(unittest.TestCase):
    """Verifies scripts/setup_sdcard_storage.sh validation, flags, and dry-run safety."""

    def setUp(self):
        self.script_path = PROJECT_ROOT / "scripts" / "setup_sdcard_storage.sh"

    def test_script_exists_and_is_executable(self):
        """Verify setup_sdcard_storage.sh exists and starts with proper bash shebang."""
        self.assertTrue(self.script_path.exists(), "scripts/setup_sdcard_storage.sh must exist")
        content = self.script_path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", content)

    def test_script_fstab_mount_options_specification(self):
        """Verify script includes required resilient mount options: defaults,noatime,commit=60,errors=remount-ro,nofail."""
        content = self.script_path.read_text(encoding="utf-8")
        self.assertIn("defaults,noatime,commit=60,errors=remount-ro,nofail", content)
        self.assertIn("LABEL_BACKUP", content)
        self.assertIn("/mnt/sdcard_backup", content)

    def test_script_excludes_root_and_emmc(self):
        """Verify script explicitly detects and excludes root block device."""
        content = self.script_path.read_text(encoding="utf-8")
        self.assertIn("ROOT_DISK", content)
        self.assertIn("findmnt", content)

    def test_script_dry_run_execution(self):
        """Verify script executes successfully with --check flag without modifying system."""
        # Locate bash interpreter
        bash_exe = "bash"
        if os.name == "nt":
            git_bash = Path("Y:/Git/bin/bash.exe")
            if git_bash.exists():
                bash_exe = str(git_bash)

        res = subprocess.run(
            [bash_exe, str(self.script_path), "--check"],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0, f"Script dry-run failed with code {res.returncode}: {res.stderr}")
        self.assertIn("DRY-RUN / CHECK MODE", res.stdout)
        self.assertIn("Topology Ready", res.stdout)


class TestSystemdHardening(unittest.TestCase):
    """Verifies systemd/autolabel.service unit hardening and sandbox paths."""

    def setUp(self):
        self.service_path = PROJECT_ROOT / "systemd" / "autolabel.service"

    def test_service_contains_wants_network_online(self):
        """Verify systemd unit specifies Wants=network-online.target under [Unit]."""
        content = self.service_path.read_text(encoding="utf-8")
        self.assertIn("Wants=network-online.target", content)

    def test_service_contains_read_write_paths(self):
        """Verify systemd unit allows writing to both /opt/label_system and /mnt/sdcard_backup."""
        content = self.service_path.read_text(encoding="utf-8")
        self.assertIn("ReadWritePaths=/opt/label_system /mnt/sdcard_backup", content)

    def test_service_preserves_resource_limits(self):
        """Verify 350MB memory ceiling and auto-restart remain active."""
        content = self.service_path.read_text(encoding="utf-8")
        self.assertIn("MemoryMax=350M", content)
        self.assertIn("Restart=always", content)
        self.assertIn("RestartSec=5s", content)
        self.assertIn("ProtectSystem=full", content)


class TestZeroEmojiCompliance(unittest.TestCase):
    """Enforces strict zero emoji policy across all M3 deliverables."""

    def test_zero_emojis_in_source_and_scripts(self):
        """Verify strictly zero emojis exist in persistence.py, setup script, and systemd service."""
        import re
        emoji_pattern = re.compile(
            r"[\U0001F600-\U0001F64F"
            r"\U0001F300-\U0001F5FF"
            r"\U0001F680-\U0001F6FF"
            r"\U0001F700-\U0001F77F"
            r"\U0001F780-\U0001F7FF"
            r"\U0001F800-\U0001F8FF"
            r"\U0001F900-\U0001F9FF"
            r"\U0001FA00-\U0001FA6F"
            r"\U0001FA70-\U0001FAFF"
            r"\U00002702-\U000027B0"
            r"\U000024C2-\U0001F251]"
        )

        files_to_check = [
            PROJECT_ROOT / "daemon" / "persistence.py",
            PROJECT_ROOT / "daemon" / "color_queue.py",
            PROJECT_ROOT / "daemon" / "main.py",
            PROJECT_ROOT / "daemon" / "web_ui.py",
            PROJECT_ROOT / "daemon" / "config.py",
            PROJECT_ROOT / "scripts" / "setup_sdcard_storage.sh",
            PROJECT_ROOT / "systemd" / "autolabel.service",
            PROJECT_ROOT / "tests" / "test_persistence.py"
        ]

        for path in files_to_check:
            self.assertTrue(path.exists(), f"File {path} must exist")
            content = path.read_text(encoding="utf-8")
            matches = emoji_pattern.findall(content)
            self.assertEqual(len(matches), 0, f"Found emojis {matches} in {path.name}")


if __name__ == "__main__":
    unittest.main()
