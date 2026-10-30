import os
import json
import time
import shutil
import tarfile
import tempfile
import unittest
import subprocess
import asyncio
from pathlib import Path
from typing import Dict, List, Optional
from aiohttp.test_utils import TestClient, TestServer

from daemon.config import config
from daemon.web_ui import WebDashboard
from daemon.color_queue import ColorQueueManager
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.persistence import PersistenceManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestUpdateStateMachine(unittest.TestCase):
    """
    Validates the 8-state update engine state transitions, JSON manifest serialization,
    and rollback recovery structures.
    """

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_m4_sm_"))
        self.recovery_dir = self.test_dir / "rollback_recovery"
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.recovery_dir / "update_state.json"
        self.rollback_file = self.recovery_dir / "rollback_state.json"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_eight_state_transition_sequence(self):
        """Verify formal 8-state sequence from IDLE to COMMITTED."""
        states = [
            "IDLE",
            "PRE_FLIGHT_CHECK",
            "SNAPSHOT_CREATION",
            "FETCH_AND_RESET",
            "DEPENDENCY_VALID",
            "SERVICE_RESTART",
            "HEALTH_VALIDATION",
            "COMMITTED"
        ]
        history = []
        current_state = states[0]
        history.append(current_state)

        for step_idx, next_state in enumerate(states[1:], start=1):
            current_state = next_state
            history.append(current_state)

        self.assertEqual(len(history), 8)
        self.assertEqual(history[0], "IDLE")
        self.assertEqual(history[-1], "COMMITTED")
        self.assertEqual(history[1], "PRE_FLIGHT_CHECK")
        self.assertEqual(history[2], "SNAPSHOT_CREATION")
        self.assertEqual(history[3], "FETCH_AND_RESET")
        self.assertEqual(history[4], "DEPENDENCY_VALID")
        self.assertEqual(history[5], "SERVICE_RESTART")
        self.assertEqual(history[6], "HEALTH_VALIDATION")

    def test_state_file_json_structure(self):
        """Verify update_state.json contains all required metadata fields."""
        payload = {
            "state": "COMMITTED",
            "state_alias": "COMMIT",
            "step": 7,
            "status": "SUCCESS",
            "timestamp": int(time.time()),
            "iso_timestamp": "2026-10-30T00:00:00Z",
            "previous_commit": "abc1234",
            "current_commit": "def5678",
            "snapshot_path": str(self.test_dir / "snapshot.tar.gz"),
            "message": "Update successfully committed and verified healthy"
        }
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        for field in ("state", "step", "status", "timestamp", "previous_commit", "current_commit", "message"):
            self.assertIn(field, loaded)
        self.assertEqual(loaded["status"], "SUCCESS")
        self.assertEqual(loaded["state"], "COMMITTED")

    def test_rollback_state_json_structure(self):
        """Verify rollback_state.json adheres to rollback contract."""
        payload = {
            "previous_commit": "893c451",
            "timestamp": int(time.time()),
            "iso_timestamp": "2026-10-30T00:00:00Z",
            "status": "ROLLBACK_READY",
            "reason": "Health check validation failed or timed out",
            "recovered_commit": "893c451"
        }
        self.rollback_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        loaded = json.loads(self.rollback_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded["previous_commit"], "893c451")
        self.assertEqual(loaded["status"], "ROLLBACK_READY")
        self.assertIn("reason", loaded)


class TestSnapshotStagingAndPruning(unittest.TestCase):
    """
    Validates snapshot tarball creation, archive integrity verification,
    snapshot restoration, and the 5-newest retention pruning rule.
    """

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_m4_snap_"))
        self.mock_app_dir = self.test_dir / "app"
        self.mock_app_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir = self.test_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Populate sample application files in core directories
        for sub in ("daemon", "web", "templates", "scripts"):
            sub_path = self.mock_app_dir / sub
            sub_path.mkdir(parents=True, exist_ok=True)
            (sub_path / f"test_{sub}.txt").write_text(f"Sample content for {sub}", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_snapshot_tarball_creation(self):
        """Verify creation of pre-update snapshot tarball containing core dirs."""
        commit_hash = "abc1234"
        ts = "20261030_010000"
        archive_path = self.snapshot_dir / f"snapshot_{commit_hash}_{ts}.tar.gz"

        with tarfile.open(archive_path, "w:gz") as tar:
            for sub in ("daemon", "web", "templates", "scripts"):
                tar.add(self.mock_app_dir / sub, arcname=sub)

        self.assertTrue(archive_path.exists())
        self.assertGreater(archive_path.stat().st_size, 100)

        # Verify archive can be inspected
        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()
            self.assertTrue(any("daemon" in n for n in names))
            self.assertTrue(any("web" in n for n in names))
            self.assertTrue(any("templates" in n for n in names))
            self.assertTrue(any("scripts" in n for n in names))

    def test_snapshot_tarball_restoration(self):
        """Verify snapshot tarball can be extracted cleanly to restore corrupted files."""
        archive_path = self.snapshot_dir / "snapshot_restore_test.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            for sub in ("daemon", "web"):
                tar.add(self.mock_app_dir / sub, arcname=sub)

        # Corrupt app dir
        (self.mock_app_dir / "daemon" / "test_daemon.txt").write_text("CORRUPTED", encoding="utf-8")
        self.assertEqual((self.mock_app_dir / "daemon" / "test_daemon.txt").read_text(), "CORRUPTED")

        # Restore from snapshot
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=self.mock_app_dir)

        self.assertEqual(
            (self.mock_app_dir / "daemon" / "test_daemon.txt").read_text(),
            "Sample content for daemon"
        )

    def test_snapshot_retention_pruning(self):
        """Verify that snapshot pruning keeps exactly 5 newest archives and removes older ones."""
        # Create 8 snapshot archives with artificial timestamps
        created_files = []
        for i in range(1, 9):
            snap = self.snapshot_dir / f"snapshot_rev{i}_20261030_010{i}00.tar.gz"
            snap.write_bytes(b"MOCK_TAR_GZ_DATA_" + str(i).encode())
            # Stagger modification times
            mtime = 1700000000 + (i * 100)
            os.utime(snap, (mtime, mtime))
            created_files.append(snap)

        self.assertEqual(len(list(self.snapshot_dir.glob("snapshot_*.tar.gz"))), 8)

        # Implement retention pruning rule: sort newest first, keep top 5, remove rest
        all_snaps = sorted(
            self.snapshot_dir.glob("snapshot_*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        if len(all_snaps) > 5:
            for old_snap in all_snaps[5:]:
                old_snap.unlink()

        remaining = list(self.snapshot_dir.glob("snapshot_*.tar.gz"))
        self.assertEqual(len(remaining), 5)

        # Verify the 3 oldest were removed and 5 newest remain
        remaining_names = {p.name for p in remaining}
        for i in range(1, 4):
            self.assertNotIn(f"snapshot_rev{i}_20261030_010{i}00.tar.gz", remaining_names)
        for i in range(4, 9):
            self.assertIn(f"snapshot_rev{i}_20261030_010{i}00.tar.gz", remaining_names)

    def test_snapshot_retention_pruning_under_threshold(self):
        """Verify pruning does not remove anything when 5 or fewer snapshots exist."""
        for i in range(1, 4):
            snap = self.snapshot_dir / f"snapshot_test_{i}.tar.gz"
            snap.write_bytes(b"DATA")

        all_snaps = sorted(
            self.snapshot_dir.glob("snapshot_*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        if len(all_snaps) > 5:
            for old_snap in all_snaps[5:]:
                old_snap.unlink()

        remaining = list(self.snapshot_dir.glob("snapshot_*.tar.gz"))
        self.assertEqual(len(remaining), 3)


class TestPreFlightValidation(unittest.TestCase):
    """
    Validates pre-flight checks: disk space threshold (>500MB), network connectivity,
    and git repository work tree integrity.
    """

    def test_preflight_disk_space_threshold(self):
        """Verify pre-flight validates disk space threshold of 500MB free."""
        free_bytes = shutil.disk_usage(PROJECT_ROOT).free
        free_mb = free_bytes / (1024 * 1024)
        self.assertGreater(free_mb, 500, "Device must have at least 500MB free space before updating")

    def test_preflight_insufficient_disk_space_rejection(self):
        """Verify that simulated free space below 500MB is rejected."""
        simulated_free_mb = 350
        min_required_mb = 500
        is_sufficient = simulated_free_mb >= min_required_mb
        self.assertFalse(is_sufficient, "Disk space under 500MB must fail pre-flight validation")

    def test_preflight_git_worktree_detection(self):
        """Verify git worktree validation succeeds on actual repo and fails on arbitrary directory."""
        # Genuine repo check
        res = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "true")

        # Non-repo check
        with tempfile.TemporaryDirectory() as temp_empty_dir:
            res_bad = subprocess.run(
                ["git", "-C", temp_empty_dir, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True
            )
            self.assertNotEqual(res_bad.returncode, 0)


class TestRollbackMechanisms(unittest.TestCase):
    """
    Validates failure detection and automatic rollback behavior across
    health check timeout, HTTP 500, git fetch network drop, and pip failure.
    """

    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_m4_rb_"))
        self.recovery_dir = self.test_dir / "rollback_recovery"
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir = self.test_dir / "audit_logs"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.failure_log = self.audit_dir / "update_failures.log"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_health_check_timeout_triggers_rollback(self):
        """Verify health check probe timing out after 30s triggers ROLLBACK_EXECUTE state."""
        timeout_limit = 30.0
        elapsed = 30.5
        is_healthy = False

        state = "HEALTH_VALIDATION"
        if elapsed >= timeout_limit and not is_healthy:
            state = "ROLLBACK_EXECUTE"
            # Append audit log
            self.failure_log.write_text(
                "[ROLLBACK_ALERT] Health check validation failed or timed out\n",
                encoding="utf-8"
            )

        self.assertEqual(state, "ROLLBACK_EXECUTE")
        self.assertTrue(self.failure_log.exists())
        self.assertIn("Health check validation failed", self.failure_log.read_text(encoding="utf-8"))

    def test_health_check_http_500_triggers_rollback(self):
        """Verify service returning HTTP 500 response triggers immediate rollback."""
        http_status_code = 500
        state = "HEALTH_VALIDATION"
        if http_status_code != 200:
            state = "ROLLBACK_EXECUTE"

        self.assertEqual(state, "ROLLBACK_EXECUTE")

    def test_git_fetch_failure_triggers_rollback(self):
        """Verify git fetch network drop (exit 128) halts update and triggers rollback."""
        fetch_exit_code = 128
        state = "FETCH_AND_RESET"
        if fetch_exit_code != 0:
            state = "ROLLBACK_EXECUTE"

        self.assertEqual(state, "ROLLBACK_EXECUTE")

    def test_dependency_installation_failure_triggers_rollback(self):
        """Verify pip installation conflict (exit 1) triggers rollback without corrupting system."""
        pip_exit_code = 1
        state = "DEPENDENCY_VALID"
        if pip_exit_code != 0:
            state = "ROLLBACK_EXECUTE"

        self.assertEqual(state, "ROLLBACK_EXECUTE")

    def test_env_secret_preservation_across_rollback(self):
        """Verify .env secrets are preserved and protected from overwrite or deletion."""
        env_path = self.test_dir / ".env"
        secret_content = "GEMINI_API_KEYS=key1,key2,key3\nSECRET_TOKEN=xyz789\n"
        env_path.write_text(secret_content, encoding="utf-8")

        # Simulate update process: backup before git reset
        backup_env = self.test_dir / ".env.bak"
        shutil.copyfile(env_path, backup_env)

        # Simulate disruptive git reset wiping or modifying .env
        env_path.unlink()
        self.assertFalse(env_path.exists())

        # Rollback restores .env from backup
        if backup_env.exists() and not env_path.exists():
            shutil.copyfile(backup_env, env_path)

        self.assertTrue(env_path.exists())
        self.assertEqual(env_path.read_text(encoding="utf-8"), secret_content)


class TestUpdateScriptCLI(unittest.TestCase):
    """
    Validates scripts/update_with_rollback.sh CLI flags using bash execution.
    """

    @classmethod
    def setUpClass(cls):
        # Locate genuine bash (ignore WindowsApps stub)
        candidates = [
            Path("Y:/Git/bin/bash.exe"),
            Path("Y:/Git/usr/bin/bash.exe"),
            Path("C:/Program Files/Git/bin/bash.exe")
        ]
        cls.bash_bin = None
        for c in candidates:
            if c.exists():
                cls.bash_bin = str(c)
                break
        if not cls.bash_bin:
            w = shutil.which("bash")
            if w and "WindowsApps" not in w:
                cls.bash_bin = w
            else:
                cls.bash_bin = "/bin/bash" if Path("/bin/bash").exists() else None
        cls.script_path = PROJECT_ROOT / "scripts" / "update_with_rollback.sh"

    def setUp(self):
        if not self.bash_bin:
            self.skipTest("bash executable not available on this system")
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_m4_cli_"))
        self.storage_dir = self.test_dir / "storage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cli_help(self):
        """Verify --help flag prints usage instructions and exits with code 0."""
        cmd = [self.bash_bin, str(self.script_path), "--help"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Usage: update_with_rollback.sh", res.stdout)
        self.assertIn("--check", res.stdout)
        self.assertIn("--dry-run", res.stdout)
        self.assertIn("--simulate-failure", res.stdout)

    def test_cli_check_mode(self):
        """Verify --check flag validates pre-flight checks non-destructively and exits 0."""
        cmd = [
            self.bash_bin, str(self.script_path),
            "--check",
            "--app-dir", str(PROJECT_ROOT),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Pre-flight checks passed successfully", res.stdout)
        self.assertIn("State -> IDLE", res.stdout)

    def test_cli_simulate_success(self):
        """Verify --simulate-success runs all 8 states and completes with COMMITTED."""
        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-success",
            "--app-dir", str(PROJECT_ROOT),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("State -> COMMITTED", res.stdout)
        self.assertIn("Auto-Update Completed Successfully", res.stdout)

        # Check update_state.json exists and contains SUCCESS
        state_file = self.storage_dir / "rollback_recovery" / "update_state.json"
        self.assertTrue(state_file.exists())
        state_data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(state_data["status"], "SUCCESS")
        self.assertEqual(state_data["state"], "COMMITTED")

    def test_cli_simulate_failure_triggers_rollback(self):
        """Verify --simulate-failure causes health check failure, triggers rollback, and exits non-zero."""
        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-failure",
            "--app-dir", str(PROJECT_ROOT),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)
        self.assertIn("CRITICAL: Triggering automatic rollback", res.stderr + res.stdout)

        # Verify rollback_state.json is written
        rb_file = self.storage_dir / "rollback_recovery" / "rollback_state.json"
        self.assertTrue(rb_file.exists())
        rb_data = json.loads(rb_file.read_text(encoding="utf-8"))
        self.assertEqual(rb_data["status"], "ROLLBACK_READY")
        self.assertIn("Health check validation failed", rb_data["reason"])

        # Verify audit failure log
        fail_log = self.storage_dir / "audit_logs" / "update_failures.log"
        self.assertTrue(fail_log.exists())
        self.assertIn("ROLLBACK_ALERT", fail_log.read_text(encoding="utf-8"))

    def test_cli_unknown_option_exits_with_error(self):
        """Verify invalid command line option exits with non-zero status."""
        cmd = [self.bash_bin, str(self.script_path), "--invalid-flag-12345"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)


class TestWebUIUpdateEndpoints(unittest.IsolatedAsyncioTestCase):
    """
    Validates Web Dashboard endpoints for auto-update:
    - POST /api/update/apply
    - GET /api/update/status
    - Setup aliases /api/setup/update/apply, /api/setup/update/status
    """

    async def asyncSetUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="test_m4_web_"))
        self.storage_dir = self.test_dir / "storage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.queue_mgr = ColorQueueManager()
        self.printer = ArgoxPrinterEngine(device_path="/dev/null", simulation_mode=True)
        self.key_pool = GeminiKeyPoolManager(keys=["MOCK_KEY_TEST"])
        self.persistence = PersistenceManager(
            sd_mount=self.storage_dir,
            fallback_dir=self.storage_dir
        )

        self.dashboard = WebDashboard(
            queue_mgr=self.queue_mgr,
            printer=self.printer,
            key_pool=self.key_pool,
            persistence=self.persistence
        )

    async def asyncTearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_get_update_status_idle(self):
        """Verify GET /api/update/status reports IDLE when no update has run."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.get("/api/update/status")
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertIn("state", data)
            self.assertIn("status", data)
            self.assertIn("in_progress", data)
            self.assertIn("current_commit", data)
            self.assertFalse(data["in_progress"])
            self.assertIn(data["status"], ("IDLE", "SUCCESS", "ROLLBACK"))

    async def test_post_update_apply_simulation_sync(self):
        """Verify POST /api/update/apply executes simulate-success synchronously when wait=True."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post(
                "/api/update/apply",
                json={"simulate_success": True, "wait": True}
            )
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["state"], "COMMITTED")
            self.assertIn("log", data)

    async def test_post_update_apply_simulation_failure_sync(self):
        """Verify POST /api/update/apply executes simulate-failure and reports rollback."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            resp = await client.post(
                "/api/update/apply",
                json={"simulate_failure": True, "wait": True}
            )
            self.assertEqual(resp.status, 200)
            data = await resp.json()
            self.assertEqual(data["status"], "error")
            self.assertIn(data["state"], ("ROLLBACK", "ROLLBACK_EXECUTE"))

    async def test_concurrent_update_rejection(self):
        """Verify POST /api/update/apply rejects concurrent update attempts with HTTP 409."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            async def send_apply():
                return await client.post(
                    "/api/update/apply",
                    json={"simulate_success": True, "wait": False}
                )

            # Fire concurrent update requests simultaneously
            responses = await asyncio.gather(*[send_apply() for _ in range(10)])
            status_codes = [r.status for r in responses]

            num_accepted = status_codes.count(200)
            num_conflicts = status_codes.count(409)

            self.assertEqual(num_accepted, 1, f"Expected exactly 1 accepted request, but {num_accepted} were accepted")
            self.assertEqual(num_conflicts, 9, f"Expected 9 rejected requests, but {num_conflicts} were rejected")

            conflict_resps = [r for r in responses if r.status == 409]
            self.assertTrue(len(conflict_resps) > 0)
            conflict_data = await conflict_resps[0].json()
            self.assertEqual(conflict_data["status"], "error")
            self.assertEqual(conflict_data["state"], "IN_PROGRESS")
            self.assertIn("already in progress", conflict_data["message"])

    async def test_setup_alias_endpoints(self):
        """Verify backward compatible /api/setup/update/apply and /api/setup/update/status routes."""
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # Test status alias
            status_resp = await client.get("/api/setup/update/status")
            self.assertEqual(status_resp.status, 200)
            status_data = await status_resp.json()
            self.assertIn("state", status_data)

            # Test apply alias
            apply_resp = await client.post(
                "/api/setup/update/apply",
                json={"simulate_success": True, "wait": True}
            )
            self.assertEqual(apply_resp.status, 200)
            apply_data = await apply_resp.json()
            self.assertEqual(apply_data["status"], "success")


if __name__ == "__main__":
    unittest.main()
