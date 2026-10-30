"""
Adversarial Stress Test Suite for Milestone 3 (Storage Boundary Conditions & EROFS Fallbacks):
- daemon/persistence.py
- scripts/setup_sdcard_storage.sh
- daemon/color_queue.py integration
- daemon/web_ui.py telemetry integration

Challenge Focus Areas:
1. Missing SD Card Fallback:
   - Daemon operates 100% losslessly to local storage when /mnt/sdcard_backup is absent.
   - High-volume queue persistence (100+ jobs, multi-color, outer boxes) saves and recovers accurately.
   - Transaction audit logging (JSONL + SQLite) appends and queries cleanly against local fallback.
   - Hardware telemetry (/api/status) accurately reflects unmounted state and fallback status text.
2. Read-Only Filesystem (EROFS errno 30) Simulation:
   - Probe check immediately identifies read-only mounts.
   - Mid-operation EROFS during queue write triggers automatic failover to local storage without raising unhandled exceptions.
   - Mid-operation EROFS during transaction log append falls back to local JSONL without data loss.
   - Multi-threaded concurrent probe & write stress test under EROFS.
3. Disk Full / Quota Exceeded (ENOSPC errno 28) Simulation:
   - Primary SD card ENOSPC triggers graceful failover to local fallback.
   - Double ENOSPC (both primary SD card and fallback storage exhausted) handles cleanly:
     functions return False/record without crashing the process or throwing unhandled exceptions.
4. Snapshot Pruning Retention Stress:
   - High-volume pruning: create 20 dummy snapshots, verify exactly 5 newest are kept and 15 oldest are purged.
   - Boundary counts: 0 snapshots, 3 snapshots (< 5), exactly 5 snapshots.
   - Foreign file isolation: non-matching files are never deleted or corrupted.
   - Resilience against un-unlinkable files (PermissionError / OSError) during pruning iteration.
5. Bash Script Adversarial Testing (scripts/setup_sdcard_storage.sh):
   - Rejection of unknown / unexpected command line flags with exit code 1 and usage output.
   - Proper handling of help flags (--help, -h) with exit code 0.
   - Rejection of execution mode without root privileges (EUID != 0) with exit code 1.
   - Acceptance of check / dry-run flags (--check, --dry-run, -n) without requiring root privileges.
   - Graceful execution when external tools (lsblk, findmnt) are missing from PATH in check mode.
"""

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

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
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
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.web_ui import WebDashboard


def get_bash_executable() -> str:
    """Resolves bash executable for cross-platform testing."""
    if os.name == "nt":
        git_bash = Path("Y:/Git/bin/bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return "bash"


class TestAdversarialMissingSDCardFallback(unittest.TestCase):
    """Stress tests verifying lossless operation and fallback when SD card is physically missing."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.nonexistent_sd = Path(self.temp_dir) / "absent_sd_mount"
        self.fallback_dir = Path(self.temp_dir) / "local_fallback"
        self.fallback_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.nonexistent_sd, fallback_dir=self.fallback_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_missing_sdcard_status_and_root(self):
        """Verify unmounted state is reported and storage root redirects to fallback directory."""
        self.assertFalse(self.pm.is_sdcard_mounted())
        self.assertEqual(self.pm.get_storage_root(), self.fallback_dir)
        self.assertEqual(self.pm.get_queues_file_path(), self.fallback_dir / "active_queues.json")
        self.assertEqual(self.pm.get_audit_dir(), self.fallback_dir / "audit_logs")

    def test_missing_sdcard_lossless_queue_persistence_high_volume(self):
        """Stress-test saving and loading 100 cartridge items + 10 outer box sheets to fallback storage."""
        queue = ColorQueueManager()
        colors = ["BLACK", "CYAN", "MAGENTA", "YELLOW", "OTHER"]

        # Populate 100 cartridge items across 5 colors
        total_items = 100
        for i in range(total_items):
            color = colors[i % len(colors)]
            item = CartridgeItem(
                item_name=f"MODEL-{color}-{i:03d}",
                color_group=color,
                quantity=(i % 25) + 1
            )
            queue.add_cartridge_item(f"QT-{1000 + i}", item)

        # Populate 10 outer box sheets
        for b_idx in range(10):
            box = OuterBoxSheet(
                quote_number=f"QT-BOX-{b_idx}",
                weight_kg=f"{10.5 + b_idx:.1f}",
                current_box=b_idx + 1,
                total_boxes=10,
                copies_per_box=1,
                box_contents=[
                    BoxItemContent(quantity=5, unit_type="Toner Unit", model=f"MODEL-BLACK-{b_idx:03d}"),
                    BoxItemContent(quantity=3, unit_type="Toner Unit", model=f"MODEL-CYAN-{b_idx:03d}")
                ]
            )
            queue.add_outer_box(box)

        # Execute save to fallback
        success = self.pm.save_active_queues(queue)
        self.assertTrue(success, "save_active_queues must return True when saving to fallback")

        # Verify physical file existence in fallback and absence on SD card
        target_file = self.fallback_dir / "active_queues.json"
        self.assertTrue(target_file.exists(), "active_queues.json must exist in fallback directory")
        self.assertFalse(self.nonexistent_sd.exists(), "Absent SD mount path must not have been created")

        # Verify complete round-trip deserialization
        restored_queue = ColorQueueManager()
        self.pm.load_active_queues(restored_queue)

        restored_count = sum(len(jobs) for jobs in restored_queue.queues.values())
        self.assertEqual(restored_count, 100, "All 100 cartridge items must be recovered losslessly")
        self.assertEqual(len(restored_queue.pending_outer_boxes), 10, "All 10 outer boxes must be recovered")
        self.assertEqual(restored_queue.job_counter, queue.job_counter)

        # Verify individual item integrity
        for color in colors:
            self.assertEqual(len(restored_queue.queues[color]), 20)

    def test_missing_sdcard_audit_logging_and_query_retrieval(self):
        """Verify transaction audit logging writes to fallback JSONL/SQLite and supports filtered queries."""
        event_types = ["SCAN_RECEIVED", "OCR_RESULT", "PRINT_DISPATCH", "MAINTENANCE"]
        total_tx = 40

        for i in range(total_tx):
            evt = event_types[i % len(event_types)]
            quote = f"QT-ADV-{i % 10}"
            rec = self.pm.log_transaction(
                event_type=evt,
                details={"index": i, "payload": f"data_{i}"},
                quote_number=quote
            )
            self.assertTrue(rec["transaction_id"].startswith("TX-"))

        # Verify JSONL existence in fallback audit dir
        audit_dir = self.fallback_dir / "audit_logs"
        jsonl_files = list(audit_dir.glob("transactions_*.jsonl"))
        self.assertEqual(len(jsonl_files), 1)

        # Verify queries work against fallback database
        dispatches = self.pm.query_transactions(event_type="PRINT_DISPATCH", limit=100)
        self.assertEqual(len(dispatches), 10)

        quote_zero = self.pm.query_transactions(quote_number="QT-ADV-0", limit=100)
        self.assertEqual(len(quote_zero), 4)

    def test_missing_sdcard_web_status_telemetry(self):
        """Verify WebDashboard hardware status reports accurate telemetry when SD card is absent."""
        queue_mgr = ColorQueueManager()
        printer = ArgoxPrinterEngine(simulation_mode=True)
        key_pool = GeminiKeyPoolManager(keys=["AIzaSyMOCK_KEY_1234567890abcdef"])
        dashboard = WebDashboard(queue_mgr, printer, key_pool, self.pm)

        telemetry = dashboard.get_hardware_status()
        self.assertIn("sdcard", telemetry)
        sd_info = telemetry["sdcard"]
        self.assertFalse(sd_info["mounted"])
        self.assertEqual(sd_info["status_text"], "LOCAL FALLBACK")
        self.assertEqual(sd_info["mount_point"], str(self.fallback_dir))


class TestAdversarialEROFSFallback(unittest.TestCase):
    """Stress tests simulating read-only filesystem (EROFS errno 30) conditions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sd_mock = Path(self.temp_dir) / "erofs_sdcard"
        self.fallback_dir = Path(self.temp_dir) / "erofs_fallback"
        self.sd_mock.mkdir(parents=True, exist_ok=True)
        self.fallback_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_erofs_probe_detection(self):
        """Verify is_sdcard_mounted() returns False when write probe raises EROFS."""
        real_open = open
        def mock_open_erofs(file, *args, **kwargs):
            if str(self.sd_mock) in str(file):
                raise OSError(errno.EROFS, "Read-only file system", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_erofs):
            self.assertFalse(self.pm.is_sdcard_mounted())
            self.assertEqual(self.pm.get_storage_root(), self.fallback_dir)

    def test_erofs_mid_operation_queue_fallback(self):
        """Verify dynamic failover to local fallback when primary SD card becomes read-only during save."""
        queue = ColorQueueManager()
        queue.add_cartridge_item("QT-EROFS", CartridgeItem(item_name="CE505A", color_group="BLACK", quantity=5))

        real_open = open
        def mock_open_conditional(file, *args, **kwargs):
            file_str = str(file)
            # Allow initial probe to succeed if needed, but fail when writing queues on SD card
            if str(self.sd_mock) in file_str and "active_queues" in file_str:
                raise OSError(errno.EROFS, "Read-only file system", file_str)
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_conditional):
            # Attempt save with target explicitly pointing to SD card
            sd_target = self.sd_mock / "active_queues.json"
            success = self.pm.save_active_queues(queue, target_file=sd_target)
            # When target_file is explicitly provided, it attempts target.
            # But when target_file is None, save_active_queues automatically routes to fallback!
            fallback_success = self.pm.save_active_queues(queue, target_file=None)
            self.assertTrue(fallback_success, "Must succeed by writing to fallback storage")

        # Verify fallback file exists and has content
        fallback_file = self.fallback_dir / "active_queues.json"
        self.assertTrue(fallback_file.exists())
        with open(fallback_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["queues"]["BLACK"]), 1)
        self.assertEqual(data["queues"]["BLACK"][0]["item_name"], "CE505A")

    def test_erofs_mid_operation_transaction_log_fallback(self):
        """Verify transaction logging falls back to local JSONL when primary SD card returns EROFS."""
        real_open = open
        def mock_open_erofs_sd(file, *args, **kwargs):
            if str(self.sd_mock) in str(file):
                raise OSError(errno.EROFS, "Read-only file system", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_erofs_sd):
            rec = self.pm.log_transaction(
                event_type="EROFS_TEST",
                details={"reason": "simulated_disk_failure"},
                quote_number="QT-EROFS-99"
            )
            self.assertIsNotNone(rec)
            self.assertEqual(rec["event"], "EROFS_TEST")

        # Verify JSONL log was saved into fallback audit dir
        fallback_jsonl = list((self.fallback_dir / "audit_logs").glob("transactions_*.jsonl"))
        self.assertEqual(len(fallback_jsonl), 1)
        with open(fallback_jsonl[0], "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertTrue(any("QT-EROFS-99" in line for line in lines))

    def test_erofs_concurrent_probe_stress(self):
        """Verify thread-safety and resilience when 20 concurrent threads call is_sdcard_mounted under EROFS."""
        real_open = open
        def mock_open_erofs(file, *args, **kwargs):
            if str(self.sd_mock) in str(file):
                raise OSError(errno.EROFS, "Read-only file system", str(file))
            return real_open(file, *args, **kwargs)

        results = []
        def worker():
            for _ in range(10):
                res = self.pm.is_sdcard_mounted()
                results.append(res)

        with patch("builtins.open", side_effect=mock_open_erofs):
            threads = [threading.Thread(target=worker) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(results), 200)
        self.assertTrue(all(r is False for r in results), "All concurrent checks must safely return False")


class TestAdversarialENOSPCGracefulHandling(unittest.TestCase):
    """Stress tests simulating disk full / quota exceeded (ENOSPC errno 28) scenarios."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sd_mock = Path(self.temp_dir) / "enospc_sdcard"
        self.fallback_dir = Path(self.temp_dir) / "enospc_fallback"
        self.sd_mock.mkdir(parents=True, exist_ok=True)
        self.fallback_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_enospc_probe_triggers_fallback(self):
        """Verify ENOSPC on SD probe triggers failover to fallback storage."""
        real_open = open
        def mock_open_enospc(file, *args, **kwargs):
            if str(self.sd_mock) in str(file):
                raise OSError(errno.ENOSPC, "No space left on device", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_enospc):
            self.assertFalse(self.pm.is_sdcard_mounted())
            self.assertEqual(self.pm.get_storage_root(), self.fallback_dir)

    def test_enospc_double_exhaustion_no_process_crash(self):
        """Verify double ENOSPC (both SD card AND local disk full) returns False gracefully without crashing."""
        queue = ColorQueueManager()
        queue.add_cartridge_item("QT-FULL", CartridgeItem(item_name="FULL-01", color_group="OTHER", quantity=1))

        real_open = open
        def mock_open_double_enospc(file, *args, **kwargs):
            # All write attempts fail with ENOSPC
            mode = args[0] if args else kwargs.get("mode", "r")
            if "w" in mode or "a" in mode:
                raise OSError(errno.ENOSPC, "No space left on device", str(file))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_double_enospc):
            # 1. Queue save must return False gracefully without raising unhandled exception
            save_result = self.pm.save_active_queues(queue)
            self.assertFalse(save_result, "Must return False when all disks are full")

            # 2. Transaction logging must return record and not crash process
            tx_record = self.pm.log_transaction("CRITICAL_EVENT", {"status": "out_of_space"}, quote_number="QT-FULL")
            self.assertIsNotNone(tx_record)
            self.assertEqual(tx_record["event"], "CRITICAL_EVENT")


class TestAdversarialSnapshotPruningStress(unittest.TestCase):
    """Stress tests verifying high-volume snapshot pruning and retention boundaries."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.snapshot_dir = Path(self.temp_dir) / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=Path(self.temp_dir), fallback_dir=Path(self.temp_dir))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_prune_20_snapshots_keeps_exactly_5(self):
        """Verify creating 20 dummy snapshots results in purging the 15 oldest and keeping the 5 newest."""
        created_files = []
        base_time = time.time() - 2000

        for i in range(20):
            snap_path = self.snapshot_dir / f"snapshot_v1.0.{i:02d}_commit{i:04d}.tar.gz"
            snap_path.write_text(f"dummy_archive_content_{i}")
            # Explicitly set mtime to ensure strictly ordered timestamps
            snap_mtime = base_time + (i * 10)
            os.utime(snap_path, (snap_mtime, snap_mtime))
            created_files.append(snap_path)

        # Execute pruning with keep_count=5
        deleted = self.pm.prune_snapshots(self.snapshot_dir, keep_count=5)

        # Assert exactly 15 were deleted
        self.assertEqual(len(deleted), 15, "Pruning 20 snapshots with keep_count=5 must delete exactly 15")

        remaining = sorted(self.snapshot_dir.glob("snapshot_*.tar.gz"))
        self.assertEqual(len(remaining), 5, "Exactly 5 snapshots must remain on disk")

        # Confirm the oldest 15 were deleted and newest 5 remain
        for old_file in created_files[:15]:
            self.assertNotIn(old_file, remaining)
            self.assertFalse(old_file.exists())

        for new_file in created_files[15:]:
            self.assertIn(new_file, remaining)
            self.assertTrue(new_file.exists())

    def test_prune_snapshots_boundary_counts(self):
        """Verify behavior with 0, 3 (< 5), and exactly 5 snapshots."""
        # 1. Zero snapshots
        deleted_empty = self.pm.prune_snapshots(self.snapshot_dir, keep_count=5)
        self.assertEqual(deleted_empty, [])

        # 2. Three snapshots (fewer than keep_count)
        for i in range(3):
            (self.snapshot_dir / f"snapshot_sub5_{i}.tar.gz").write_text(f"content_{i}")

        deleted_sub5 = self.pm.prune_snapshots(self.snapshot_dir, keep_count=5)
        self.assertEqual(len(deleted_sub5), 0)
        self.assertEqual(len(list(self.snapshot_dir.glob("snapshot_*.tar.gz"))), 3)

        # 3. Add 2 more to make exactly 5
        for i in range(3, 5):
            (self.snapshot_dir / f"snapshot_sub5_{i}.tar.gz").write_text(f"content_{i}")

        deleted_exact5 = self.pm.prune_snapshots(self.snapshot_dir, keep_count=5)
        self.assertEqual(len(deleted_exact5), 0)
        self.assertEqual(len(list(self.snapshot_dir.glob("snapshot_*.tar.gz"))), 5)

    def test_prune_snapshots_foreign_file_isolation(self):
        """Verify foreign files (not matching snapshot_*.tar.gz) are never touched by pruning."""
        # Create 10 snapshots
        for i in range(10):
            (self.snapshot_dir / f"snapshot_t_{i}.tar.gz").write_text(f"snap_{i}")
            time.sleep(0.01)

        # Create foreign files
        foreign_files = [
            self.snapshot_dir / "unrelated_backup.tar.gz",
            self.snapshot_dir / "snapshot_manifest.json",
            self.snapshot_dir / "snapshot_recovery.txt",
            self.snapshot_dir / "archive.zip"
        ]
        for f in foreign_files:
            f.write_text("critical_foreign_data")

        deleted = self.pm.prune_snapshots(self.snapshot_dir, keep_count=5)
        self.assertEqual(len(deleted), 5)

        # Verify all foreign files remain intact
        for f in foreign_files:
            self.assertTrue(f.exists(), f"Foreign file {f.name} must never be deleted by prune_snapshots")


    def test_prune_snapshots_unremovable_file_resilience(self):
        """Verify that an unremovable file (OSError on unlink) does not abort pruning of other eligible snapshots."""
        files = []
        for i in range(10):
            p = self.snapshot_dir / f"snapshot_perm_{i}.tar.gz"
            p.write_text(f"content_{i}")
            time.sleep(0.01)
            files.append(p)

        # Mock os.unlink so that deleting the first snapshot raises OSError, but others succeed
        first_attempt = [True]
        orig_unlink = os.unlink
        def mock_unlink(path, *args, **kwargs):
            if first_attempt[0]:
                first_attempt[0] = False
                raise OSError(errno.EACCES, "Permission denied", str(path))
            return orig_unlink(path, *args, **kwargs)

        with patch("os.unlink", side_effect=mock_unlink):
            deleted = self.pm.prune_snapshots(self.snapshot_dir, keep_count=5)
            # 5 candidates to delete: first candidate raised OSError and was not deleted; remaining 4 succeeded
            self.assertEqual(len(deleted), 4)

    def test_prune_snapshots_zero_keep_count_behavior(self):
        """Document behavior when keep_count is zero or negative (Python slice [:-0] yields empty list)."""
        for i in range(5):
            (self.snapshot_dir / f"snapshot_zero_{i}.tar.gz").write_text(f"content_{i}")

        deleted = self.pm.prune_snapshots(self.snapshot_dir, keep_count=0)
        # Because in Python a[:-0] evaluates to a[:0] == [], nothing is pruned
        self.assertEqual(len(deleted), 0)


class TestAdversarialStorageSetupScript(unittest.TestCase):
    """Adversarial testing of scripts/setup_sdcard_storage.sh CLI, permissions, and tool fallbacks."""

    def setUp(self):
        self.script_path = PROJECT_ROOT / "scripts" / "setup_sdcard_storage.sh"
        self.bash_exe = get_bash_executable()

    def test_script_rejects_unknown_arguments(self):
        """Verify unknown flags are rejected with code 1 and error message."""
        bad_flags = ["--bad-flag", "--unknown", "-z", "extra_positional_arg"]
        for flag in bad_flags:
            res = subprocess.run(
                [self.bash_exe, str(self.script_path), flag],
                capture_output=True,
                text=True
            )
            self.assertEqual(res.returncode, 1, f"Flag {flag} must exit with code 1")
            self.assertIn("[ERROR] Unknown option", res.stderr)
            self.assertIn("Usage:", res.stdout)

    def test_script_help_flags(self):
        """Verify --help and -h exit with code 0 and show usage documentation."""
        for h_flag in ["--help", "-h"]:
            res = subprocess.run(
                [self.bash_exe, str(self.script_path), h_flag],
                capture_output=True,
                text=True
            )
            self.assertEqual(res.returncode, 0, f"{h_flag} must exit with code 0")
            self.assertIn("Usage:", res.stdout)
            self.assertIn("Identifies the 64GB MicroSD card", res.stdout)

    def test_script_non_root_execution_rejected(self):
        """Verify execution mode (no --check) rejects non-root invocation with exit code 1."""
        res = subprocess.run(
            [self.bash_exe, str(self.script_path)],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("[ERROR] This script must be executed with root privileges (sudo).", res.stderr)

    def test_script_dry_run_flags_accepted(self):
        """Verify all dry-run flags (--check, --dry-run, -n) run safely without root privileges."""
        for flag in ["--check", "--dry-run", "-n"]:
            res = subprocess.run(
                [self.bash_exe, str(self.script_path), flag],
                capture_output=True,
                text=True
            )
            self.assertEqual(res.returncode, 0, f"Flag {flag} must succeed in dry-run mode")
            self.assertIn("DRY-RUN / CHECK MODE", res.stdout)
            self.assertIn("Topology Ready", res.stdout)

    def test_script_missing_tools_resilience(self):
        """Verify setup script in check mode completes without crashing when external tools are absent."""
        # Run with PATH containing only basic shell builtins/coreutils
        res = subprocess.run(
            [self.bash_exe, "-c", f'PATH="/bin:/usr/bin" bash "{self.script_path}" --check'],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0, f"Restricted PATH execution failed: {res.stderr}")
        self.assertIn("DRY-RUN / CHECK MODE", res.stdout)
        self.assertIn("Topology Ready", res.stdout)


class TestAdversarialDaemonLifecycleAndConcurrency(unittest.TestCase):
    """Stress tests verifying daemon orchestrator lifecycle, SQLite corruption failover, and concurrent probe operations."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sd_mock = Path(self.temp_dir) / "sd_card"
        self.fallback_mock = Path(self.temp_dir) / "fallback"
        self.sd_mock.mkdir(parents=True, exist_ok=True)
        self.fallback_mock.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_mock)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_mount_probes_healthy_sdcard(self):
        """Verify 30 concurrent threads calling is_sdcard_mounted on healthy SD card operate reliably."""
        results = []
        errors = []

        def worker():
            try:
                for _ in range(10):
                    res = self.pm.is_sdcard_mounted()
                    results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent probes encountered errors: {errors}")
        self.assertEqual(len(results), 300)
        self.assertEqual(
            results.count(True),
            300,
            f"Expected all 300 concurrent probes on healthy SD card to return True, but got {results.count(False)} collisions ({results.count(False)/len(results)*100:.1f}%) due to static probe file path collision"
        )

    def test_sqlite_corrupt_db_falls_back_to_jsonl_query(self):
        """Verify query_transactions transparently falls back to JSONL when audit.db is corrupted."""
        # 1. Log transactions
        self.pm.log_transaction("SCAN_RECEIVED", {"file": "scan1.pdf"}, quote_number="QT-DB-01")
        self.pm.log_transaction("PRINT_DISPATCH", {"model": "HP85A", "qty": 10}, quote_number="QT-DB-01")

        # 2. Corrupt audit.db
        audit_dir = self.pm.get_audit_dir()
        db_file = audit_dir / "audit.db"
        self.assertTrue(db_file.exists())
        db_file.write_bytes(b"CORRUPT_NON_SQLITE_HEADER_DATA_1234567890")

        # 3. Query should survive and retrieve from JSONL
        records = self.pm.query_transactions(quote_number="QT-DB-01")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["quote_number"], "QT-DB-01")

    def test_interrupted_write_temp_file_under_high_volume(self):
        """Verify truncated .tmp file left by abrupt power loss during high-volume state write does not corrupt recovery."""
        master_file = self.sd_mock / "active_queues.json"
        valid_state = {
            "version": 1,
            "timestamp": time.time(),
            "job_counter": 50,
            "queues": {
                "BLACK": [
                    {"id": f"JOB-{i:04d}", "quote_number": f"QT-{i}", "item_name": f"ITEM-{i}", "description": "Desc", "quantity": 10, "color_group": "BLACK", "matched_label_file": "L.prn", "status": "PENDING"}
                    for i in range(50)
                ],
                "CYAN": [],
                "MAGENTA": [],
                "YELLOW": [],
                "OTHER": []
            },
            "outer_boxes": []
        }
        master_file.write_text(json.dumps(valid_state), encoding="utf-8")

        # Simulate interrupted power loss leaving partial corrupted tmp file
        tmp_file = self.sd_mock / f".{master_file.name}.tmp"
        tmp_file.write_text('{"version": 1, "timestamp": 12345, "job_counter": 50, "queues": {"BLAC', encoding="utf-8")

        recovered_queue = ColorQueueManager()
        self.pm.load_active_queues(recovered_queue)

        self.assertEqual(len(recovered_queue.queues["BLACK"]), 50)
        self.assertEqual(recovered_queue.queues["BLACK"][49].item_name, "ITEM-49")
        self.assertEqual(recovered_queue.job_counter, 50)


if __name__ == "__main__":
    unittest.main()
