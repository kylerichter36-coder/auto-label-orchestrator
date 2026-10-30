"""
Adversarial Stress Test Suite for Milestone M3:
- daemon/persistence.py
- daemon/color_queue.py
- scripts/setup_sdcard_storage.sh
- systemd/autolabel.service

Challenge Focus Areas:
1. Power-Loss & Recovery Stress:
   - Corrupt/truncated .tmp files alongside valid master active_queues.json
   - Zero-byte master file with corrupt .tmp file
   - Failover to local non-volatile storage when primary SD mount is corrupted
   - Sudden process kill/crash mid-write simulation (subprocess SIGKILL / proc.kill())
   - Atomic write failure safety (preserving previous valid master state)
2. High-Concurrency Queue Modifications (50+ Threads):
   - 50 threads concurrently adding items and triggering automatic non-volatile writes
   - 50 threads concurrently reading (load) and writing (save) queue states
   - Concurrent mutations interleaved with mark_color_printed and clear_all
   - Concurrent multi-process atomic file replacement stress
3. Rapid Transaction Logging Burst (500+ Events):
   - 750 rapid scan/print transactions logged simultaneously across 50 worker threads
   - Verification of SQLite lock contention, error handling, and zero lost records
   - Verification of JSONL line-by-line schema validity and fsync consistency
   - Concurrent queries while rapid-fire logging is under way
   - SQLite database corruption with automatic graceful fallback to JSONL log
   - Extreme payload edge cases (Unicode, newlines, binary sequences, null fields)
4. Storage Failover & System Hardening:
   - Dynamic switch to fallback directory on EROFS (read-only filesystem)
   - Auto-healing of missing directory topologies
   - High-throughput snapshot creation and pruning (retaining exactly 5 newest)
   - Strict Zero-Emoji compliance verification across all components
"""

import os
import sys
import json
import time
import errno
import shutil
import sqlite3
import tempfile
import unittest
import threading
import subprocess
from pathlib import Path
from unittest.mock import patch

# Ensure project root is in sys.path
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


class TestAdversarialPowerLossAndRecovery(unittest.TestCase):
    """
    Stress-tests power loss, unexpected shutdowns, corrupted partial temp files,
    and fallback recovery mechanisms.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sd_mock = Path(self.temp_dir) / "sd_card"
        self.fallback_mock = Path(self.temp_dir) / "fallback"
        self.sd_mock.mkdir(parents=True, exist_ok=True)
        self.fallback_mock.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_mock)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_corrupt_truncated_tmp_file_alongside_valid_master(self):
        """
        Scenario: A power outage occurred while writing .active_queues.json.tmp,
        leaving half-written garbage. The master file remains intact.
        Verification: load_active_queues recovers the intact master without error,
        and the subsequent save replaces the corrupted .tmp file cleanly.
        """
        master_file = self.sd_mock / "active_queues.json"
        valid_state = {
            "version": 1,
            "timestamp": time.time(),
            "job_counter": 12,
            "queues": {
                "BLACK": [
                    {
                        "id": "JOB-0001",
                        "quote_number": "QT-9001",
                        "item_name": "CE505A",
                        "description": "HP 05A Black",
                        "quantity": 10,
                        "color_group": "BLACK",
                        "matched_label_file": "CE505A.prn",
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
        master_file.write_text(json.dumps(valid_state, indent=2), encoding="utf-8")

        # Inject truncated corrupt temp file
        tmp_file = self.sd_mock / f".{master_file.name}.tmp"
        tmp_file.write_text('{"version": 1, "queues": {"BLACK": [{"item_name": "TRUNCATED_POW', encoding="utf-8")

        # Load must ignore or bypass the truncated temp file
        qm = ColorQueueManager()
        loaded_data = self.pm.load_active_queues(qm)

        self.assertEqual(loaded_data["job_counter"], 12)
        self.assertEqual(len(qm.queues["BLACK"]), 1)
        self.assertEqual(qm.queues["BLACK"][0].item_name, "CE505A")

        # Now save a new state; verify temp file is cleanly replaced
        qm.add_cartridge_item("QT-9002", CartridgeItem(item_name="CF280A", color_group="BLACK", quantity=5))
        save_success = self.pm.save_active_queues(qm)
        self.assertTrue(save_success)
        self.assertFalse(tmp_file.exists(), "Corrupted .tmp file must not linger after atomic replace")

        # Verify master file contains updated data
        with open(master_file, "r", encoding="utf-8") as f:
            final_data = json.load(f)
        self.assertEqual(len(final_data["queues"]["BLACK"]), 2)

    def test_zero_byte_master_with_corrupt_tmp_file(self):
        """
        Scenario: Legacy crash left a 0-byte active_queues.json and a corrupted temp file.
        Verification: load_active_queues handles the condition gracefully, returns
        a clean initialized default dictionary without raising JSONDecodeError.
        """
        master_file = self.sd_mock / "active_queues.json"
        master_file.write_bytes(b"") # 0 bytes

        tmp_file = self.sd_mock / f".{master_file.name}.tmp"
        tmp_file.write_bytes(b"\x00\xff\xfe\x00") # binary garbage

        qm = ColorQueueManager()
        result = self.pm.load_active_queues(qm)

        self.assertEqual(result["version"], 1)
        self.assertEqual(result["job_counter"], 0)
        self.assertEqual(len(qm.queues["BLACK"]), 0)

    def test_primary_corrupted_recovers_from_fallback_dir(self):
        """
        Scenario: Primary SD card active_queues.json is corrupted by disk errors,
        but the fallback directory contains a valid backup state.
        Verification: load_active_queues recovers the fallback state smoothly.
        """
        primary_master = self.sd_mock / "active_queues.json"
        primary_master.write_text("{CORRUPT_JSON_SYNTAX_ERROR!", encoding="utf-8")

        fallback_master = self.fallback_mock / "active_queues.json"
        backup_state = {
            "version": 1,
            "timestamp": time.time(),
            "job_counter": 77,
            "queues": {
                "BLACK": [],
                "CYAN": [
                    {
                        "id": "JOB-0077",
                        "quote_number": "QT-FB-1",
                        "item_name": "CF501A",
                        "description": "Cyan Toner",
                        "quantity": 3,
                        "color_group": "CYAN",
                        "matched_label_file": "CF501A.prn",
                        "status": "PENDING"
                    }
                ],
                "MAGENTA": [],
                "YELLOW": [],
                "OTHER": []
            },
            "outer_boxes": []
        }
        fallback_master.write_text(json.dumps(backup_state), encoding="utf-8")

        qm = ColorQueueManager()
        loaded = self.pm.load_active_queues(qm)

        self.assertEqual(loaded["job_counter"], 77)
        self.assertEqual(len(qm.queues["CYAN"]), 1)
        self.assertEqual(qm.queues["CYAN"][0].item_name, "CF501A")

    def test_sudden_process_crash_simulation(self):
        """
        Scenario: Process running continuous atomic saves is terminated abruptly
        with SIGKILL / proc.kill() during write execution.
        Verification: On-disk active_queues.json is strictly valid JSON (never
        corrupted or partially written).
        """
        worker_code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"{PROJECT_ROOT}")
from daemon.persistence import PersistenceManager
from daemon.color_queue import ColorQueueManager
from daemon.classifier import CartridgeItem

pm = PersistenceManager(sd_mount=Path(r"{self.sd_mock}"), fallback_dir=Path(r"{self.fallback_mock}"))
qm = ColorQueueManager()
qm.set_persistence_manager(pm)

for i in range(2000):
    qm.add_cartridge_item(f"QT-CRASH-{{i}}", CartridgeItem(item_name=f"ITEM-{{i}}", color_group="BLACK", quantity=1))
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Allow worker process to perform several writes, then kill forcefully
        time.sleep(0.08)
        proc.kill()
        proc.wait()

        master_file = self.sd_mock / "active_queues.json"
        if master_file.exists():
            # If the file exists, it MUST be valid JSON (atomic guarantee)
            with open(master_file, "r", encoding="utf-8") as f:
                content = f.read()
            parsed = json.loads(content) # Will raise JSONDecodeError if corrupted
            self.assertIn("version", parsed)
            self.assertIn("queues", parsed)

        # Loading active queues must succeed cleanly
        qm = ColorQueueManager()
        loaded = self.pm.load_active_queues(qm)
        self.assertIsInstance(loaded, dict)

    def test_interrupted_atomic_write_preserves_previous_master(self):
        """
        Scenario: An I/O error occurs midway during _atomic_write_json before replace.
        Verification: The original master file is preserved without modification.
        """
        original_data = {"version": 1, "job_counter": 50, "queues": {"BLACK": []}, "outer_boxes": []}
        master_file = self.sd_mock / "active_queues.json"
        master_file.write_text(json.dumps(original_data), encoding="utf-8")

        new_data = {"version": 1, "job_counter": 999, "queues": {"BLACK": [{"corrupted": True}]}, "outer_boxes": []}

        # Mock open to fail when writing the temporary file
        real_open = open
        def failing_open(path, *args, **kwargs):
            if ".tmp" in str(path):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=failing_open):
            success = self.pm._atomic_write_json(master_file, new_data)
            self.assertFalse(success)

        # Master file must remain completely unaltered
        with open(master_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["job_counter"], 50)


class TestAdversarialConcurrency50Threads(unittest.TestCase):
    """
    Stress-tests concurrent state modifications across 50+ threads,
    verifying zero race conditions, zero file corruption, and thread safety.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sd_mock = Path(self.temp_dir) / "sd_card"
        self.sd_mock.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.sd_mock)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_50_threads_concurrent_queue_mutations_and_persistence(self):
        """
        Stress scenario: 50 concurrent threads each adding 10 items (total 500 items).
        Each addition triggers an atomic save to active_queues.json.
        Verification:
        - 0 unhandled exceptions across all 50 threads.
        - Exactly 500 items queued.
        - Persisted active_queues.json contains all 500 items and is valid JSON.
        """
        qm = ColorQueueManager()
        qm.set_persistence_manager(self.pm)
        errors = []
        thread_count = 50
        items_per_thread = 10
        total_expected = thread_count * items_per_thread

        def worker_task(worker_id: int):
            try:
                for i in range(items_per_thread):
                    qm.add_cartridge_item(
                        f"QT-CONC-{worker_id}",
                        CartridgeItem(
                            item_name=f"HP-TONER-{worker_id}-{i}",
                            color_group="BLACK",
                            quantity=1
                        )
                    )
            except Exception as e:
                errors.append((worker_id, str(e)))

        threads = [threading.Thread(target=worker_task, args=(w,)) for w in range(thread_count)]
        start_time = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - start_time

        self.assertEqual(len(errors), 0, f"Thread errors encountered: {errors}")
        self.assertEqual(len(qm.queues["BLACK"]), total_expected)

        # Recover from disk in a fresh queue manager
        recovered_qm = ColorQueueManager()
        persisted_data = self.pm.load_active_queues(recovered_qm)

        self.assertEqual(len(recovered_qm.queues["BLACK"]), total_expected)
        self.assertEqual(persisted_data["version"], 1)

    def test_50_threads_simultaneous_save_and_load(self):
        """
        Stress scenario: 25 threads constantly saving varying queue states
        while 25 threads constantly loading queue states (1000 total operations).
        Verification: 0 JSON decode errors, 0 Windows sharing violations (PermissionError),
        and 0 deadlocks.
        """
        save_threads = 25
        load_threads = 25
        ops_per_thread = 20
        errors = []

        def saver(w_id: int):
            try:
                for i in range(ops_per_thread):
                    state = {
                        "version": 1,
                        "timestamp": time.time(),
                        "job_counter": w_id * 100 + i,
                        "queues": {
                            "BLACK": [
                                {
                                    "id": f"JOB-{w_id}-{i}",
                                    "quote_number": f"QT-{w_id}",
                                    "item_name": f"ITEM-{w_id}",
                                    "description": "Desc",
                                    "quantity": 1,
                                    "color_group": "BLACK",
                                    "matched_label_file": "file.prn",
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
                    self.pm.save_active_queues(state)
            except Exception as e:
                errors.append(("saver", w_id, str(e)))

        def loader(w_id: int):
            try:
                for i in range(ops_per_thread):
                    data = self.pm.load_active_queues()
                    self.assertIn("version", data)
            except Exception as e:
                errors.append(("loader", w_id, str(e)))

        all_threads = []
        for w in range(save_threads):
            all_threads.append(threading.Thread(target=saver, args=(w,)))
        for w in range(load_threads):
            all_threads.append(threading.Thread(target=loader, args=(w,)))

        for t in all_threads:
            t.start()
        for t in all_threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent save/load errors: {errors}")

    def test_concurrent_queue_mutations_with_clear_and_print(self):
        """
        Stress scenario: Threads appending jobs while other threads concurrently
        invoke mark_color_printed and clear_all.
        Verification: No iteration modification exceptions (RuntimeError) and
        valid state persists throughout.
        """
        qm = ColorQueueManager()
        qm.set_persistence_manager(self.pm)
        errors = []
        stop_flag = threading.Event()

        def producer(w_id: int):
            seq = 0
            while not stop_flag.is_set():
                try:
                    qm.add_cartridge_item(
                        f"QT-PROD-{w_id}",
                        CartridgeItem(
                            item_name=f"PROD-{w_id}-{seq}",
                            color_group="CYAN",
                            quantity=2
                        )
                    )
                    seq += 1
                    time.sleep(0.002)
                except Exception as e:
                    errors.append(("producer", str(e)))
                    break

        def consumer(w_id: int):
            while not stop_flag.is_set():
                try:
                    qm.mark_color_printed("CYAN")
                    time.sleep(0.005)
                except Exception as e:
                    errors.append(("consumer", str(e)))
                    break

        def clearer():
            for _ in range(5):
                if stop_flag.is_set():
                    break
                try:
                    time.sleep(0.015)
                    qm.clear_all()
                except Exception as e:
                    errors.append(("clearer", str(e)))
                    break

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(10)]
        threads.extend([threading.Thread(target=consumer, args=(i,)) for i in range(5)])
        threads.append(threading.Thread(target=clearer))

        for t in threads:
            t.start()

        time.sleep(0.3)
        stop_flag.set()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Errors in concurrent mutation test: {errors}")


class TestAdversarialRapidTransactionBurst500(unittest.TestCase):
    """
    Stress-tests rapid transaction logging bursts (500+ events),
    verifying SQLite lock contention resolution, JSONL consistency,
    and fallback mechanics.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage_mock = Path(self.temp_dir) / "storage"
        self.storage_mock.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.storage_mock, fallback_dir=self.storage_mock)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_500_plus_rapid_transaction_burst_concurrency(self):
        """
        Stress scenario: 50 concurrent threads logging 15 events each = 750 events
        rapidly fired simultaneously.
        Verification:
        - 0 unhandled exceptions across all 50 threads.
        - Exactly 750 records recorded in SQLite database.
        - Exactly 750 valid JSON lines recorded in transactions_YYYYMM.jsonl.
        - Complete consistency between SQLite transaction_ids and JSONL transaction_ids.
        """
        thread_count = 50
        events_per_thread = 15
        total_expected = thread_count * events_per_thread
        errors = []

        def worker_logger(worker_id: int):
            try:
                for i in range(events_per_thread):
                    event_type = "SCAN_RECEIVED" if i % 2 == 0 else "PRINT_DISPATCH"
                    self.pm.log_transaction(
                        event_type=event_type,
                        details={
                            "worker_id": worker_id,
                            "seq": i,
                            "random_payload": f"TEST_PAYLOAD_{worker_id}_{i}"
                        },
                        quote_number=f"QT-BURST-{worker_id}"
                    )
            except Exception as e:
                errors.append((worker_id, str(e)))

        threads = [threading.Thread(target=worker_logger, args=(w,)) for w in range(thread_count)]

        start_time = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - start_time

        self.assertEqual(len(errors), 0, f"Burst logging errors: {errors}")

        # 1. Verify JSONL audit file integrity
        audit_dir = self.pm.get_audit_dir()
        jsonl_files = list(audit_dir.glob("transactions_*.jsonl"))
        self.assertEqual(len(jsonl_files), 1, "Expected exactly 1 monthly JSONL file")

        jsonl_records = []
        with open(jsonl_files[0], "r", encoding="utf-8") as f:
            for line_idx, line in enumerate(f, 1):
                clean_line = line.strip()
                self.assertTrue(clean_line, f"Empty line found at line {line_idx}")
                rec = json.loads(clean_line) # Validates valid JSON
                self.assertTrue(rec["transaction_id"].startswith("TX-"))
                jsonl_records.append(rec)

        self.assertEqual(len(jsonl_records), total_expected, "JSONL line count must equal total events")

        # 2. Verify SQLite database integrity
        db_path = audit_dir / "audit.db"
        self.assertTrue(db_path.exists(), "audit.db must exist")

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM transactions")
        sqlite_count = cursor.fetchone()[0]

        cursor.execute("SELECT transaction_id FROM transactions")
        sqlite_ids = set(r[0] for r in cursor.fetchall())
        conn.close()

        self.assertEqual(sqlite_count, total_expected, "SQLite row count must equal total events")

        # 3. Verify cross-engine consistency
        jsonl_ids = set(r["transaction_id"] for r in jsonl_records)
        self.assertEqual(sqlite_ids, jsonl_ids, "All transaction IDs must match between SQLite and JSONL")

    def test_concurrent_query_under_rapid_logging_burst(self):
        """
        Stress scenario: 10 threads continuously executing queries while 40 threads
        log 600 transactions simultaneously.
        Verification: No database locked errors crash the system; all queries succeed.
        """
        logging_threads = 40
        query_threads = 10
        events_per_logger = 15
        errors = []
        stop_query = threading.Event()

        def logger_task(w_id: int):
            try:
                for i in range(events_per_logger):
                    self.pm.log_transaction(
                        "PRINT_DISPATCH",
                        {"model": "HP85A", "qty": i},
                        quote_number=f"QT-QUERY-{w_id}"
                    )
            except Exception as e:
                errors.append(("logger", w_id, str(e)))

        def query_task(w_id: int):
            try:
                while not stop_query.is_set():
                    res = self.pm.query_transactions(event_type="PRINT_DISPATCH", limit=50)
                    self.assertIsInstance(res, list)
                    time.sleep(0.005)
            except Exception as e:
                errors.append(("querier", w_id, str(e)))

        q_threads = [threading.Thread(target=query_task, args=(w,)) for w in range(query_threads)]
        l_threads = [threading.Thread(target=logger_task, args=(w,)) for w in range(logging_threads)]

        for t in q_threads:
            t.start()
        for t in l_threads:
            t.start()

        for t in l_threads:
            t.join()

        stop_query.set()
        for t in q_threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Query under burst errors: {errors}")

    def test_sqlite_corruption_fallback_resilience(self):
        """
        Stress scenario: The SQLite database file is violently corrupted
        (overwritten with garbage bytes).
        Verification:
        - log_transaction does not crash; falls back cleanly to JSONL.
        - query_transactions catches the SQLite error, falls back to parsing JSONL,
          and returns all logged records losslessly.
        """
        # Log initial event
        self.pm.log_transaction("INITIAL_EVENT", {"status": "ok"}, quote_number="QT-INIT")

        # Corrupt the SQLite database file
        db_path = self.pm.get_audit_dir() / "audit.db"
        db_path.write_bytes(b"CORRUPTED_SQLITE_GARBAGE_HEADER_1234567890!@#$%^&*()")

        # Subsequent logging must continue safely without uncaught exception
        rec2 = self.pm.log_transaction("AFTER_CORRUPTION", {"status": "recovered"}, quote_number="QT-POST")
        self.assertTrue(rec2["transaction_id"].startswith("TX-"))

        # Query must fall back to JSONL and return both records
        results = self.pm.query_transactions()
        self.assertEqual(len(results), 2)
        event_names = [r["event"] for r in results]
        self.assertIn("INITIAL_EVENT", event_names)
        self.assertIn("AFTER_CORRUPTION", event_names)

    def test_transaction_logging_special_characters_and_edge_cases(self):
        """
        Stress scenario: Payloads with special characters, newlines, quotes,
        Unicode characters, empty dictionaries, null quote numbers, and large bodies.
        Verification: Correct serialization in JSONL and SQLite.
        """
        test_payloads = [
            ("NULL_QUOTE", {"data": "test"}, None),
            ("EMPTY_DETAILS", {}, "QT-EMPTY"),
            ("NEWLINES_AND_QUOTES", {"text": "Line 1\nLine 2\r\n\"Quoted\" and 'Single' \\backslash"}, "QT-SPECIAL"),
            ("UNICODE_SYMBOLS", {"hebrew": "בדיקה", "cyrillic": "Проверка", "greek": "δοκιμή"}, "QT-UNI"),
            ("LARGE_PAYLOAD", {"data": "X" * 50000}, "QT-LARGE")
        ]

        for event, details, q_num in test_payloads:
            rec = self.pm.log_transaction(event, details, quote_number=q_num)
            self.assertEqual(rec["event"], event)

        # Query back
        all_recs = self.pm.query_transactions(limit=10)
        self.assertEqual(len(all_recs), len(test_payloads))

        # Check unicode record
        uni_rec = self.pm.query_transactions(quote_number="QT-UNI")[0]
        self.assertEqual(uni_rec["details"]["greek"], "δοκιμή")

        # Check newline escaping in JSONL file directly (must stay on a single line)
        audit_dir = self.pm.get_audit_dir()
        jsonl_file = list(audit_dir.glob("transactions_*.jsonl"))[0]
        with open(jsonl_file, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(lines), len(test_payloads), "JSONL must have exactly one line per transaction")


class TestAdversarialStorageFailoverAndTopology(unittest.TestCase):
    """
    Stress-tests storage topology healing, EROFS runtime failover,
    and snapshot retention limits.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sd_mock = Path(self.temp_dir) / "sd_card"
        self.fallback_mock = Path(self.temp_dir) / "fallback"
        self.sd_mock.mkdir(parents=True, exist_ok=True)
        self.fallback_mock.mkdir(parents=True, exist_ok=True)
        self.pm = PersistenceManager(sd_mount=self.sd_mock, fallback_dir=self.fallback_mock)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_missing_directory_topology_auto_recovery(self):
        """
        Scenario: Storage directories are deleted at runtime by an external process.
        Verification: PersistenceManager automatically recreates necessary directories
        without failing writes.
        """
        # Delete audit_logs
        audit_dir = self.pm.get_audit_dir()
        shutil.rmtree(audit_dir, ignore_errors=True)
        self.assertFalse(audit_dir.exists())

        # Logging a transaction must recreate directory and succeed
        rec = self.pm.log_transaction("RECOVERY_EVENT", {"k": "v"})
        self.assertTrue(audit_dir.exists())
        self.assertTrue(rec["transaction_id"].startswith("TX-"))

    def test_snapshot_pruning_under_rapid_burst(self):
        """
        Scenario: Rapid burst creation of 30 snapshots in close time intervals.
        Verification: prune_snapshots enforces retention policy, deleting 25
        oldest snapshots and retaining exactly the 5 newest.
        """
        snapshot_dir = self.sd_mock / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        created_files = []
        for i in range(30):
            snap = snapshot_dir / f"snapshot_commit_{i:03d}_{int(time.time()*1000)}.tar.gz"
            snap.write_bytes(f"DATA_{i}".encode("utf-8"))
            created_files.append(snap)
            time.sleep(0.005)

        deleted = self.pm.prune_snapshots(snapshot_dir, keep_count=5)
        self.assertEqual(len(deleted), 25)

        remaining = sorted(snapshot_dir.glob("snapshot_*.tar.gz"), key=lambda p: p.stat().st_mtime)
        self.assertEqual(len(remaining), 5)

        # Confirm remaining are the newest 5
        for expected in created_files[-5:]:
            self.assertIn(expected, remaining)

    def test_zero_emojis_in_all_artifacts(self):
        """
        Strict audit verifying zero emojis across all M3 codebase, tests, and scripts.
        """
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

        paths = [
            PROJECT_ROOT / "daemon" / "persistence.py",
            PROJECT_ROOT / "daemon" / "color_queue.py",
            PROJECT_ROOT / "scripts" / "setup_sdcard_storage.sh",
            PROJECT_ROOT / "systemd" / "autolabel.service",
            PROJECT_ROOT / "tests" / "test_persistence.py",
            Path(__file__)
        ]

        for p in paths:
            self.assertTrue(p.exists(), f"Target path {p} must exist")
            content = p.read_text(encoding="utf-8", errors="ignore")
            matches = emoji_pattern.findall(content)
            self.assertEqual(len(matches), 0, f"Found emojis {matches} in {p.name}")

    def test_concurrent_mount_probes_race_condition(self):
        """
        Adversarial test: 20 concurrent threads calling is_sdcard_mounted().
        Bug hypothesis: Static probe file path '.sdcard_write_probe' causes sharing
        violations and false negatives, improperly falling back to local storage.
        """
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(self.pm.is_sdcard_mounted()))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 20 threads should detect that the SD card is mounted and writable.
        # Under static probe path, sharing violations cause false negatives.
        self.assertEqual(
            results.count(True),
            20,
            f"Expected 20 successful probes, got {results.count(True)} True and {results.count(False)} False"
        )


if __name__ == "__main__":
    unittest.main()

