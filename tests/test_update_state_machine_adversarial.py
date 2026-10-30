import os
import json
import time
import shutil
import tarfile
import tempfile
import unittest
import asyncio
import subprocess
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


def get_bash_executable():
    candidates = [
        Path("Y:/Git/bin/bash.exe"),
        Path("Y:/Git/usr/bin/bash.exe"),
        Path("C:/Program Files/Git/bin/bash.exe")
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    w = shutil.which("bash")
    if w and "WindowsApps" not in w:
        return w
    if Path("/bin/bash").exists():
        return "/bin/bash"
    return None


class TestAdversarialStateMachineBash(unittest.TestCase):
    """
    Adversarial Bash State Machine Tests:
    1. Pre-flight disk space failure (< 500MB threshold enforcement)
    2. Simulated network drops / DNS failures / git fetch failure (exit code 128)
    3. Simulated dependency conflict failure (pip exit code 1)
    4. Snapshot creation stress with 15+ snapshots pruning retention to 5
    5. Snapshot tarball structure and corrupted tree recovery
    """

    @classmethod
    def setUpClass(cls):
        cls.bash_bin = get_bash_executable()
        cls.script_path = PROJECT_ROOT / "scripts" / "update_with_rollback.sh"

    def setUp(self):
        if not self.bash_bin:
            self.skipTest("bash executable not available")
        self.test_dir = Path(tempfile.mkdtemp(prefix="adv_sm_test_"))
        self.mock_app_dir = self.test_dir / "app"
        self.mock_app_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir = self.test_dir / "storage"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # Seed mock app dirs
        for sub in ("daemon", "web", "templates", "scripts"):
            sub_path = self.mock_app_dir / sub
            sub_path.mkdir(parents=True, exist_ok=True)
            (sub_path / f"core_{sub}.py").write_text(f"# {sub} component", encoding="utf-8")

        # Initialize mock git repository with a bare remote as origin
        self.bare_repo = self.test_dir / "bare_repo.git"
        subprocess.run(["git", "init", "--bare", str(self.bare_repo)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(self.mock_app_dir)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.mock_app_dir), "config", "user.name", "Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.mock_app_dir), "config", "user.email", "test@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.mock_app_dir), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.mock_app_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.mock_app_dir), "remote", "add", "origin", str(self.bare_repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.mock_app_dir), "push", "-u", "origin", "main"], check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_preflight_disk_space_failure_check_only(self):
        """
        Adverse condition: Disk space threshold is set artificially high (999,999,999 MB).
        Expectation: --check fails with exit code 2 and outputs pre-flight error.
        """
        cmd = [
            self.bash_bin, str(self.script_path),
            "--check",
            "--min-disk-mb", "999999999",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2, f"Expected exit code 2 on preflight check failure, got {res.returncode}")
        self.assertIn("Pre-flight failed: Insufficient disk space", res.stderr + res.stdout)

    def test_preflight_disk_space_failure_full_run_triggers_rollback(self):
        """
        Adverse condition: Disk space failure during full update run.
        Expectation: Triggers automatic rollback, logs failure, exits 1.
        """
        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-success",
            "--min-disk-mb", "999999999",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1, f"Expected exit code 1 on rollback, got {res.returncode}")
        self.assertIn("CRITICAL: Triggering automatic rollback", res.stderr + res.stdout)

        rb_file = self.storage_dir / "rollback_recovery" / "rollback_state.json"
        self.assertTrue(rb_file.exists(), "rollback_state.json must exist after failure")
        rb_data = json.loads(rb_file.read_text(encoding="utf-8"))
        self.assertEqual(rb_data["status"], "ROLLBACK_READY")
        self.assertIn("Pre-flight verification failed", rb_data["reason"])

        audit_file = self.storage_dir / "audit_logs" / "update_failures.log"
        self.assertTrue(audit_file.exists(), "Audit failure log must be written")
        self.assertIn("Pre-flight verification failed", audit_file.read_text(encoding="utf-8"))

    def test_git_fetch_network_drop_simulation(self):
        """
        Adverse condition: Network drops or git remote fails during FETCH_AND_RESET (exit 128).
        Expectation: Script captures failure at State 3, aborts git operations, triggers rollback.
        """
        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-fetch-failure",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("Simulated network drop during git fetch", res.stderr + res.stdout)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stderr + res.stdout)

        rb_file = self.storage_dir / "rollback_recovery" / "rollback_state.json"
        self.assertTrue(rb_file.exists())
        rb_data = json.loads(rb_file.read_text(encoding="utf-8"))
        self.assertEqual(rb_data["status"], "ROLLBACK_READY")
        self.assertIn("Remote git fetch and reset failed", rb_data["reason"])

    def test_dependency_validation_conflict_simulation(self):
        """
        Adverse condition: pip install encounters conflict/error during DEPENDENCY_VALID.
        Expectation: Script captures failure at State 4, triggers rollback.
        """
        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-dep-failure",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("Simulated dependency installation conflict", res.stderr + res.stdout)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stderr + res.stdout)

    def test_snapshot_creation_stress_fifteen_plus_snapshots(self):
        """
        Adverse stress test: 18 historical snapshots exist in storage directory.
        Expectation: Running an update creates a new snapshot and prunes older ones,
        retaining EXACTLY the 5 newest snapshots.
        """
        snapshots_dir = self.storage_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)

        base_time = 1700000000
        for i in range(1, 19):
            snap_path = snapshots_dir / f"snapshot_rev{i:02d}_20261030_{1000+i:04d}.tar.gz"
            # Create valid minimal tar.gz
            with tarfile.open(snap_path, "w:gz") as tar:
                pass
            mtime = base_time + (i * 60)
            os.utime(snap_path, (mtime, mtime))

        self.assertEqual(len(list(snapshots_dir.glob("snapshot_*.tar.gz"))), 18)

        # Run update with --simulate-success
        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-success",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Update failed: {res.stderr + res.stdout}")

        # Post-run: exactly 5 snapshots must remain
        remaining = sorted(snapshots_dir.glob("snapshot_*.tar.gz"), key=lambda p: p.stat().st_mtime)
        self.assertEqual(len(remaining), 5, f"Expected exactly 5 snapshots after pruning 18+, got {len(remaining)}: {[p.name for p in remaining]}")

        # Verify that oldest snapshots (e.g. rev01..rev14) were removed
        remaining_names = [p.name for p in remaining]
        for i in range(1, 14):
            self.assertFalse(any(f"rev{i:02d}" in n for n in remaining_names), f"Old snapshot rev{i:02d} was not pruned!")

    def test_snapshot_tarball_contents_and_restoration(self):
        """
        Verify tarball content integrity and restoration:
        Snapshot tarball must contain daemon, web, templates, and scripts.
        """
        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-success",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

        snapshots_dir = self.storage_dir / "snapshots"
        snaps = list(snapshots_dir.glob("snapshot_*.tar.gz"))
        self.assertGreaterEqual(len(snaps), 1)

        latest_snap = max(snaps, key=lambda p: p.stat().st_mtime)
        with tarfile.open(latest_snap, "r:gz") as tar:
            members = [m.name for m in tar.getmembers()]

        for req_dir in ("daemon", "web", "templates", "scripts"):
            self.assertTrue(
                any(req_dir in m for m in members),
                f"Snapshot missing required component directory: {req_dir}"
            )


    def test_snapshot_pruning_extreme_workload_and_unrelated_files(self):
        """
        Adverse stress test: 25 historical snapshots exist along with non-snapshot files.
        Expectation: Retention pruning removes the 20 oldest snapshots, preserves the 5 newest,
        and never deletes or touches unrelated files.
        """
        snapshots_dir = self.storage_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Create non-snapshot files that must be protected
        unrelated_file = snapshots_dir / "unrelated_backup.tar.gz"
        unrelated_file.write_bytes(b"KEEP_THIS_DATA")
        audit_note = snapshots_dir / "snapshot_notes.txt"
        audit_note.write_text("Critical snapshot metadata note", encoding="utf-8")

        base_time = 1700000000
        for i in range(1, 26):
            snap_path = snapshots_dir / f"snapshot_commit{i:02d}_20261030_{1000+i:04d}.tar.gz"
            with tarfile.open(snap_path, "w:gz") as tar:
                pass
            mtime = base_time + (i * 100)
            os.utime(snap_path, (mtime, mtime))

        self.assertEqual(len(list(snapshots_dir.glob("snapshot_*.tar.gz"))), 25)

        cmd = [
            self.bash_bin, str(self.script_path),
            "--simulate-success",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

        # Exactly 5 snapshots must remain
        remaining = sorted(snapshots_dir.glob("snapshot_*.tar.gz"), key=lambda p: p.stat().st_mtime)
        self.assertEqual(len(remaining), 5, f"Expected 5 snapshots, got {len(remaining)}")

        # Unrelated files must still exist and be intact
        self.assertTrue(unrelated_file.exists(), "Pruning deleted an unrelated file!")
        self.assertEqual(unrelated_file.read_bytes(), b"KEEP_THIS_DATA")
        self.assertTrue(audit_note.exists(), "Pruning deleted non-tarball metadata!")
        self.assertEqual(audit_note.read_text(encoding="utf-8"), "Critical snapshot metadata note")

    def test_preflight_disk_space_boundary_conditions(self):
        """
        Adverse boundary test:
        Verify disk space boundary conditions:
        - When MIN_DISK_MB is smaller than free space (e.g., 100MB), preflight passes.
        - When MIN_DISK_MB is larger than free space, preflight fails with exit code 2.
        """
        # Pass threshold: 50MB required
        cmd_pass = [
            self.bash_bin, str(self.script_path),
            "--check",
            "--min-disk-mb", "50",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res_pass = subprocess.run(cmd_pass, capture_output=True, text=True)
        self.assertEqual(res_pass.returncode, 0)
        self.assertIn("Pre-flight checks passed successfully", res_pass.stdout)

        # Fail threshold: 999,999,999MB required
        cmd_fail = [
            self.bash_bin, str(self.script_path),
            "--check",
            "--min-disk-mb", "999999999",
            "--app-dir", str(self.mock_app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        res_fail = subprocess.run(cmd_fail, capture_output=True, text=True)
        self.assertEqual(res_fail.returncode, 2)


class TestAdversarialConcurrencyWebUI(unittest.IsolatedAsyncioTestCase):
    """
    Adversarial concurrency test:
    Simultaneously fire 20 POST /api/update/apply requests.
    Expectation: Exactly 1 executes while 19 are safely rejected with HTTP 409 Conflict.
    """

    async def asyncSetUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="adv_web_test_"))
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

    async def test_twenty_concurrent_update_requests(self):
        """
        Fire 20 simultaneous POST /api/update/apply requests concurrently.
        Verify that exactly 1 request is accepted (HTTP 200) and 19 are rejected with HTTP 409 Conflict.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            async def send_apply():
                return await client.post(
                    "/api/update/apply",
                    json={"simulate_success": True, "wait": False}
                )

            # Launch all 20 requests simultaneously
            responses = await asyncio.gather(*[send_apply() for _ in range(20)])
            status_codes = [r.status for r in responses]

            num_accepted = status_codes.count(200)
            num_conflicts = status_codes.count(409)

            print(f"\n[CONCURRENCY EMPIRICAL RESULT] 20 concurrent POST /api/update/apply: Accepted (200) = {num_accepted}, Rejected (409) = {num_conflicts}")
            print(f"[CONCURRENCY EMPIRICAL STATUS CODES] {status_codes}")

            self.assertEqual(
                num_accepted, 1,
                f"CRITICAL CONCURRENCY BUG: Exactly 1 update request must be accepted, but {num_accepted} were accepted!"
            )
            self.assertEqual(
                num_conflicts, 19,
                f"CRITICAL CONCURRENCY BUG: Exactly 19 update requests must be rejected with 409, but {num_conflicts} were rejected!"
            )

    async def test_twenty_concurrent_update_requests_sync_wait(self):
        """
        Fire 20 simultaneous POST /api/update/apply requests with wait=True.
        Verify whether race condition occurs when requests wait synchronously.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            async def send_apply():
                return await client.post(
                    "/api/update/apply",
                    json={"simulate_success": True, "wait": True}
                )

            responses = await asyncio.gather(*[send_apply() for _ in range(20)])
            status_codes = [r.status for r in responses]

            num_accepted = status_codes.count(200)
            num_conflicts = status_codes.count(409)

            print(f"\n[SYNC CONCURRENCY EMPIRICAL RESULT] 20 concurrent POST (wait=True): Accepted (200) = {num_accepted}, Rejected (409) = {num_conflicts}")
            print(f"[SYNC CONCURRENCY EMPIRICAL STATUS CODES] {status_codes}")

            self.assertEqual(
                num_accepted, 1,
                f"CRITICAL CONCURRENCY BUG (sync): Exactly 1 update request must be accepted, but {num_accepted} were accepted!"
            )
            self.assertEqual(
                num_conflicts, 19,
                f"CRITICAL CONCURRENCY BUG (sync): Exactly 19 update requests must be rejected with 409, but {num_conflicts} were rejected!"
            )

    async def test_cross_endpoint_concurrency_race(self):
        """
        Fire 10 simultaneous requests to /api/update/apply and 10 to /api/setup/update/apply.
        Verify that cross-endpoint updates cannot bypass concurrency locks.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            reqs = []
            for i in range(20):
                path = "/api/update/apply" if i % 2 == 0 else "/api/setup/update/apply"
                reqs.append(client.post(path, json={"simulate_success": True, "wait": False}))

            responses = await asyncio.gather(*reqs)
            status_codes = [r.status for r in responses]

            num_accepted = status_codes.count(200)
            num_conflicts = status_codes.count(409)

            print(f"\n[CROSS-ENDPOINT CONCURRENCY RESULT] 20 concurrent across alias endpoints: Accepted = {num_accepted}, Rejected = {num_conflicts}")
            self.assertEqual(
                num_accepted, 1,
                f"CRITICAL CONCURRENCY BUG (cross-endpoint): Exactly 1 update request must be accepted, but {num_accepted} were accepted!"
            )
            self.assertEqual(
                num_conflicts, 19,
                f"CRITICAL CONCURRENCY BUG (cross-endpoint): Exactly 19 update requests must be rejected with 409, but {num_conflicts} were rejected!"
            )


if __name__ == "__main__":
    unittest.main()

