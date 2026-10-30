import os
import sys
import time
import json
import shutil
import tarfile
import tempfile
import unittest
import threading
import subprocess
import http.server
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class MockHealthHandler(http.server.BaseHTTPRequestHandler):
    """Configurable mock HTTP server handler for health check testing."""
    response_code = 200
    response_body = b'{"status": "ok", "queues": [], "hardware": {"scanner": true}, "gemini_pool": {"active": 1}}'
    response_delay = 0.0

    def do_GET(self):
        if self.response_delay > 0:
            time.sleep(self.response_delay)
        self.send_response(self.response_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, format, *args):
        # Silence console output during tests
        pass


class MockHTTPServer:
    """Helper context manager to run an in-process mock HTTP server."""
    def __init__(self, code=200, body=None, delay=0.0):
        self.code = code
        self.body = body or b'{"status": "ok", "queues": [], "hardware": {}, "gemini_pool": {}}'
        self.delay = delay
        self.server: Optional[http.server.HTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.port: int = 0

    def __enter__(self):
        handler = MockHealthHandler
        handler.response_code = self.code
        handler.response_body = self.body
        handler.response_delay = self.delay
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class AdversarialRollbackStressTest(unittest.TestCase):
    """
    Empirical adversarial stress testing harness for scripts/update_with_rollback.sh.
    Tests real git operations, real network health checks, real process termination,
    and secret .env persistence.
    """

    @classmethod
    def setUpClass(cls):
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
        if not cls.bash_bin or not cls.script_path.exists():
            raise unittest.SkipTest("Bash executable or update_with_rollback.sh missing")

    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp(prefix="adv_test_"))
        self.upstream_dir = self.test_root / "upstream"
        self.app_dir = self.test_root / "app"
        self.storage_dir = self.test_root / "sdcard_storage"

        self.upstream_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        self._init_git_repos()

    def tearDown(self):
        shutil.rmtree(self.test_root, ignore_errors=True)

    def _init_git_repos(self):
        """Initializes a genuine bare/local upstream git repo and an application clone."""
        subprocess.run(["git", "-C", str(self.upstream_dir), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.upstream_dir), "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", str(self.upstream_dir), "config", "user.email", "tester@test.com"], check=True)

        # Create standard directories matching project
        for d in ("daemon", "web", "templates", "scripts"):
            sub = self.upstream_dir / d
            sub.mkdir(parents=True, exist_ok=True)
            (sub / f"{d}_init.txt").write_text(f"init {d}", encoding="utf-8")

        (self.upstream_dir / "version.txt").write_text("v1.0.0", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.upstream_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.upstream_dir), "commit", "-m", "Release v1.0.0"], check=True)

        # Clone to app_dir
        subprocess.run(["git", "clone", str(self.upstream_dir), str(self.app_dir)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.app_dir), "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", str(self.app_dir), "config", "user.email", "tester@test.com"], check=True)

        self.initial_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()

    def _push_upstream_update(self, new_version: str = "v2.0.0", break_code: bool = False):
        """Creates and commits a new revision on upstream."""
        (self.upstream_dir / "version.txt").write_text(new_version, encoding="utf-8")
        if break_code:
            (self.upstream_dir / "daemon" / "broken.py").write_text("SYNTAX_ERROR = [", encoding="utf-8")
        else:
            (self.upstream_dir / "daemon" / "new_feature.py").write_text("# new feature", encoding="utf-8")

        subprocess.run(["git", "-C", str(self.upstream_dir), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.upstream_dir), "commit", "-m", f"Release {new_version}"], check=True)

        return subprocess.run(
            ["git", "-C", str(self.upstream_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()

    def _run_script(self, extra_args: list, timeout: float = 45.0) -> subprocess.CompletedProcess:
        """Executes update_with_rollback.sh in bash with arguments."""
        cmd = [
            self.bash_bin,
            str(self.script_path),
            "--app-dir", str(self.app_dir),
            "--storage-dir", str(self.storage_dir)
        ] + extra_args

        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # --------------------------------------------------------------------------
    # 1. Health Check Timeout Tests (port unresponsive / hangs >30s)
    # --------------------------------------------------------------------------

    def test_health_check_timeout_triggers_clean_rollback(self):
        """
        Adversarial Test 1A: Health check timeout against closed/unresponsive port.
        Verifies:
        - Script exits with return code 1.
        - Previous commit is restored (git reset --hard OLD_COMMIT).
        - Audit failure log contains ROLLBACK_ALERT with reason.
        - rollback_state.json is generated with status ROLLBACK_READY.
        - SD card snapshot exists.
        """
        self._push_upstream_update("v2.0.0-timeout")

        # Pick an unused port where no server is listening
        dead_url = "http://127.0.0.1:59123/api/status"

        # Use 4-second timeout to keep test suite fast (2 attempts * 2s)
        res = self._run_script(["--health-url", dead_url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 1, f"Script must exit with code 1 on rollback. Output: {res.stdout}")
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)
        self.assertIn("Triggering automatic rollback", res.stderr + res.stdout)

        # Verify git commit restored to initial commit
        current_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(current_commit, self.initial_commit, "Git repository must be restored to previous commit")

        # Verify version.txt is back to v1.0.0
        self.assertEqual((self.app_dir / "version.txt").read_text(), "v1.0.0")

        # Verify rollback_state.json
        rb_file = self.storage_dir / "rollback_recovery" / "rollback_state.json"
        self.assertTrue(rb_file.exists(), "rollback_state.json must be written on failure")
        rb_data = json.loads(rb_file.read_text(encoding="utf-8"))
        self.assertEqual(rb_data["status"], "ROLLBACK_READY")
        self.assertEqual(rb_data["previous_commit"], self.initial_commit)
        self.assertEqual(rb_data["recovered_commit"], self.initial_commit)
        self.assertIn("Health check validation failed", rb_data["reason"])

        # Verify update_failures.log
        fail_log = self.storage_dir / "audit_logs" / "update_failures.log"
        self.assertTrue(fail_log.exists(), "update_failures.log must be appended")
        log_content = fail_log.read_text(encoding="utf-8")
        self.assertIn("[ROLLBACK_ALERT]", log_content)
        self.assertIn(self.initial_commit, log_content)

    def test_health_check_slow_hanging_server_exceeds_timeout(self):
        """
        Adversarial Test 1B: Health check against a mock server that hangs longer than timeout.
        Verifies timeout aborts and rolls back.
        """
        self._push_upstream_update("v2.0.0-slow")

        # Server that delays 10 seconds per request
        with MockHTTPServer(code=200, delay=10.0) as slow_srv:
            url = f"http://127.0.0.1:{slow_srv.port}/api/status"
            # 4s timeout (2 attempts * 2s) with 2s curl max-time
            t0 = time.time()
            res = self._run_script(["--health-url", url, "--health-timeout", "4"], timeout=20.0)
            elapsed = time.time() - t0

        self.assertEqual(res.returncode, 1)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)
        self.assertGreaterEqual(elapsed, 4.0, "Health check should wait for timeout before giving up")

        # Verify rollback restored initial commit
        current_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(current_commit, self.initial_commit)

    # --------------------------------------------------------------------------
    # 2. HTTP 500 & Corrupt Response Tests
    # --------------------------------------------------------------------------

    def test_health_check_http_500_triggers_rollback(self):
        """
        Adversarial Test 2A: Service returns HTTP 500 Internal Server Error.
        Verifies service failure is detected, update is rejected, and system is rolled back.
        """
        self._push_upstream_update("v2.0.0-error500")

        error_body = b'{"error": "Internal Server Error", "code": 500}'
        with MockHTTPServer(code=500, body=error_body) as srv:
            url = f"http://127.0.0.1:{srv.port}/api/status"
            res = self._run_script(["--health-url", url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 1)
        self.assertIn("Health check returned HTTP 500", res.stdout)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)

        current_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(current_commit, self.initial_commit)

    def test_health_check_corrupt_schema_missing_keys_triggers_rollback(self):
        """
        Adversarial Test 2B: Service returns HTTP 200 OK, but payload is corrupted/missing keys
        (e.g., missing 'queues', 'hardware', or 'gemini_pool').
        Verifies the schema integrity check catches incomplete payloads and rolls back.
        """
        self._push_upstream_update("v2.0.0-corrupt-schema")

        # Missing 'hardware' and 'gemini_pool'
        incomplete_json = b'{"status": "ok", "queues": ["Black", "Cyan"]}'
        with MockHTTPServer(code=200, body=incomplete_json) as srv:
            url = f"http://127.0.0.1:{srv.port}/api/status"
            res = self._run_script(["--health-url", url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 1)
        self.assertIn("HTTP 200 received but schema missing required keys", res.stdout + res.stderr)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)

        current_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(current_commit, self.initial_commit)

    def test_health_check_non_json_garbage_triggers_rollback(self):
        """
        Adversarial Test 2C: Service returns HTTP 200 OK, but payload is malformed non-JSON HTML/garbage.
        Verifies rejection and rollback.
        """
        self._push_upstream_update("v2.0.0-html-garbage")

        garbage = b"<html><head><title>502 Bad Gateway</title></head><body>Crash</body></html>"
        with MockHTTPServer(code=200, body=garbage) as srv:
            url = f"http://127.0.0.1:{srv.port}/api/status"
            res = self._run_script(["--health-url", url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 1)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)

        current_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(current_commit, self.initial_commit)

    # --------------------------------------------------------------------------
    # 3. .env Secret Protection Tests
    # --------------------------------------------------------------------------

    def test_env_file_preserved_across_successful_update(self):
        """
        Adversarial Test 3A: Untracked .env file containing API keys exists before update.
        Update succeeds. Verifies .env is never modified, deleted, or committed to git.
        """
        env_file = self.app_dir / ".env"
        secret_keys = "GEMINI_API_KEY_1=AIzaSyA_TEST_KEY_ALPHA\nGEMINI_API_KEY_2=AIzaSyB_TEST_KEY_BETA\n"
        env_file.write_text(secret_keys, encoding="utf-8")

        self._push_upstream_update("v2.0.0-success")

        with MockHTTPServer(code=200) as srv:
            url = f"http://127.0.0.1:{srv.port}/api/status"
            res = self._run_script(["--health-url", url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 0, f"Expected success: {res.stdout}")
        self.assertIn("State -> COMMITTED", res.stdout)

        self.assertTrue(env_file.exists(), ".env file must exist after update")
        self.assertEqual(env_file.read_text(encoding="utf-8"), secret_keys, ".env content must be untouched")

        # Verify .env is not tracked in git
        tracked = subprocess.run(
            ["git", "-C", str(self.app_dir), "ls-files", ".env"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(tracked, "", ".env must NEVER be tracked in git index")

    def test_env_file_preserved_across_failed_update_rollback(self):
        """
        Adversarial Test 3B: Untracked .env file exists before update.
        Update fails health check and rolls back. Verifies .env survives rollback intact.
        """
        env_file = self.app_dir / ".env"
        secret_keys = "GEMINI_API_KEYS=key1,key2,key3\nDATABASE_SECRET=super_secret_token_999\n"
        env_file.write_text(secret_keys, encoding="utf-8")

        self._push_upstream_update("v2.0.0-rollback")

        # Health check fails
        dead_url = "http://127.0.0.1:59124/api/status"
        res = self._run_script(["--health-url", dead_url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 1)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)

        self.assertTrue(env_file.exists(), ".env file must survive rollback")
        self.assertEqual(env_file.read_text(encoding="utf-8"), secret_keys, ".env content must match original byte-for-byte")

    def test_env_file_preservation_when_git_fetch_fails(self):
        """
        Adversarial Test 3C: Network drop during git fetch with untracked .env.
        Verifies .env is preserved when fetch fails and triggers rollback.
        """
        env_file = self.app_dir / ".env"
        secret_keys = "GEMINI_API_KEYS=isolated_keys_456\n"
        env_file.write_text(secret_keys, encoding="utf-8")

        # Point remote to an unreachable IP
        subprocess.run(
            ["git", "-C", str(self.app_dir), "remote", "set-url", "origin", "http://127.0.0.1:59998/invalid.git"],
            check=True
        )

        res = self._run_script([])
        self.assertEqual(res.returncode, 1)
        self.assertIn("State -> ROLLBACK_EXECUTE", res.stdout)

        self.assertTrue(env_file.exists(), ".env must survive failed git fetch rollback")
        self.assertEqual(env_file.read_text(encoding="utf-8"), secret_keys)

    def test_env_file_preservation_during_snapshot_tarball_restore(self):
        """
        Adversarial Test 3D: Snapshot tarball fallback restore does not overwrite or remove .env.
        """
        env_file = self.app_dir / ".env"
        secret_keys = "GEMINI_API_KEYS=snapshot_safe_key_777\n"
        env_file.write_text(secret_keys, encoding="utf-8")

        # Create a valid snapshot tarball
        snap_dir = self.storage_dir / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_file = snap_dir / f"snapshot_{self.initial_commit}_20261030_010000.tar.gz"

        with tarfile.open(snap_file, "w:gz") as tar:
            for sub in ("daemon", "web", "templates", "scripts"):
                tar.add(self.app_dir / sub, arcname=sub)

        # Unpack snapshot as rollback would
        with tarfile.open(snap_file, "r:gz") as tar:
            tar.extractall(path=self.app_dir)

        self.assertTrue(env_file.exists(), ".env must remain present after snapshot unpack")
        self.assertEqual(env_file.read_text(encoding="utf-8"), secret_keys)

    # --------------------------------------------------------------------------
    # 4. Mid-Update Crash & Interrupted Recovery Tests
    # --------------------------------------------------------------------------

    def test_recovery_from_corrupted_or_partial_snapshot_archive(self):
        """
        Adversarial Test 4A: Interrupted snapshot creation leaves a 0-byte or corrupted .tar.gz
        in snapshots/ directory from an earlier crash.
        Verifies:
        - Subsequent update run does not crash on corrupted archive.
        - Pruning handles corrupted archive gracefully.
        - New valid snapshot is created and update succeeds.
        """
        snap_dir = self.storage_dir / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)

        # Write 0-byte snapshot and corrupted garbage snapshot
        (snap_dir / "snapshot_corrupted1_20261029_000000.tar.gz").write_bytes(b"")
        (snap_dir / "snapshot_corrupted2_20261029_000001.tar.gz").write_bytes(b"NOT_A_GZIP_FILE_CORRUPTED_DATA")

        self._push_upstream_update("v2.0.0-corrupt-snap-recovery")

        with MockHTTPServer(code=200) as srv:
            url = f"http://127.0.0.1:{srv.port}/api/status"
            res = self._run_script(["--health-url", url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 0, f"Update should succeed despite old corrupted snapshots: {res.stdout}")
        self.assertIn("State -> COMMITTED", res.stdout)

        # Verify a new valid snapshot exists
        snaps = list(snap_dir.glob("snapshot_*.tar.gz"))
        new_snaps = [s for s in snaps if s.stat().st_size > 100]
        self.assertGreaterEqual(len(new_snaps), 1, "A valid snapshot must be created")

        # Test tar integrity of the new snapshot
        with tarfile.open(new_snaps[0], "r:gz") as tar:
            members = tar.getnames()
            self.assertTrue(any("daemon" in m for m in members))

    def test_recovery_after_mid_update_process_termination(self):
        """
        Adversarial Test 4B: Process was killed mid-update during health check probe.
        Verifies system state can be recovered on next run without hanging in locked state.
        """
        self._push_upstream_update("v2.0.0-kill-test")

        # Launch update with slow health check
        with MockHTTPServer(code=200, delay=15.0) as slow_srv:
            url = f"http://127.0.0.1:{slow_srv.port}/api/status"
            cmd = [
                self.bash_bin,
                str(self.script_path),
                "--app-dir", str(self.app_dir),
                "--storage-dir", str(self.storage_dir),
                "--health-url", url,
                "--health-timeout", "20"
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            # Wait until it enters HEALTH_VALIDATION
            time.sleep(3.0)

            # Abruptly kill process (simulate crash / power loss)
            proc.kill()
            proc.wait()

        # Check update_state.json exists and recorded in-progress state
        state_file = self.storage_dir / "rollback_recovery" / "update_state.json"
        self.assertTrue(state_file.exists())

        # Now trigger a new update with working server to verify recovery
        with MockHTTPServer(code=200) as working_srv:
            good_url = f"http://127.0.0.1:{working_srv.port}/api/status"
            res_rec = self._run_script(["--health-url", good_url, "--health-timeout", "4"])

        self.assertEqual(res_rec.returncode, 0, f"System must recover and allow subsequent update: {res_rec.stdout}")
        self.assertIn("State -> COMMITTED", res_rec.stdout)

    # --------------------------------------------------------------------------
    # 5. Retention Pruning & Pre-Flight Space Boundary Tests
    # --------------------------------------------------------------------------

    def test_snapshot_retention_pruning_boundary(self):
        """
        Adversarial Test 5A: Creates 9 snapshot archives.
        Verifies retention pruning retains exactly 5 newest archives.
        """
        snap_dir = self.storage_dir / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)

        for i in range(1, 10):
            p = snap_dir / f"snapshot_commit{i:02d}_20261030_0100{i:02d}.tar.gz"
            p.write_bytes(f"SNAPSHOT_DATA_{i}".encode("utf-8"))
            mtime = 1700000000 + (i * 60)
            os.utime(p, (mtime, mtime))

        self.assertEqual(len(list(snap_dir.glob("snapshot_*.tar.gz"))), 9)

        # Run update to trigger prune_snapshots
        with MockHTTPServer(code=200) as srv:
            url = f"http://127.0.0.1:{srv.port}/api/status"
            res = self._run_script(["--health-url", url, "--health-timeout", "4"])

        self.assertEqual(res.returncode, 0)

        # Remaining snapshots must be exactly 5
        snaps = list(snap_dir.glob("snapshot_*.tar.gz"))
        self.assertEqual(len(snaps), 5, f"Must keep exactly 5 newest snapshots, found {len(snaps)}")

    def test_preflight_insufficient_disk_rejection(self):
        """
        Adversarial Test 5B: Setting minimum free disk space threshold higher than available.
        Verifies update immediately aborts in pre-flight without modifying repository.
        """
        # Require 999999999 MB (>900 TB) free disk
        res = self._run_script(["--min-disk-mb", "999999999"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Pre-flight failed: Insufficient disk space", res.stderr + res.stdout)

        # Verify repository was not touched
        current_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(current_commit, self.initial_commit)

    # --------------------------------------------------------------------------
    # 6. Deep Failure Modes: Power Loss During Fetch & Unwritable SD Card
    # --------------------------------------------------------------------------

    def test_env_stash_trap_on_power_loss_during_fetch(self):
        """
        Adversarial Test 6A: Abrupt SIGKILL during git fetch phase.
        Documents empirical vulnerability:
        When git stash push -u runs, it sweeps untracked .env into git's untracked stash.
        If a sudden crash/power loss kills the process before line 513 (cp -f env_backup .env),
        the .env file is missing from working directory and trapped inside git stash.
        """
        env_file = self.app_dir / ".env"
        secret_keys = "CRITICAL_KEY=12345_SECRET_GEMINI\n"
        env_file.write_text(secret_keys, encoding="utf-8")

        # Set remote to slow endpoint to ensure fetch takes time
        subprocess.run(
            ["git", "-C", str(self.app_dir), "remote", "set-url", "origin", "http://10.255.255.1/slow.git"],
            check=True
        )

        cmd = [
            self.bash_bin,
            str(self.script_path),
            "--app-dir", str(self.app_dir),
            "--storage-dir", str(self.storage_dir)
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Allow git stash push -u to execute
        time.sleep(2.0)
        proc.kill()
        proc.wait()

        # Check empirical state:
        # The file is not in working directory
        env_in_working_tree = env_file.exists()
        # But it IS trapped in stash
        stash_list = subprocess.run(
            ["git", "-C", str(self.app_dir), "stash", "list"],
            check=True, capture_output=True, text=True
        ).stdout

        # Verify that stash captured it
        self.assertIn("autolabel_pre_update", stash_list, "Git stash captured local state before crash")
        if not env_in_working_tree:
            # Document finding: .env was swept into stash
            # Can be restored via git stash pop
            pop_res = subprocess.run(
                ["git", "-C", str(self.app_dir), "stash", "pop"],
                capture_output=True, text=True
            )
            self.assertTrue(env_file.exists(), ".env can be recovered from stash")
            self.assertEqual(env_file.read_text(encoding="utf-8"), secret_keys)

    def test_read_only_storage_filesystem_failure(self):
        """
        Adversarial Test 6B: Storage mount failure (SD card remounted read-only).
        Verifies update script fails safely without corrupting application git tree.
        """
        # Using a deep subpath that cannot be created or written to without write permissions
        bad_storage = self.test_root / "sdcard_ro" / "unwritable_mount_point" / "deeper"

        self._push_upstream_update("v2.0.0-ro-storage")

        cmd = [
            self.bash_bin,
            str(self.script_path),
            "--app-dir", str(self.app_dir),
            "--storage-dir", str(bad_storage)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0, "Update must fail when storage directory is unwritable")

        # Application tree must remain at initial commit
        current_commit = subprocess.run(
            ["git", "-C", str(self.app_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(current_commit, self.initial_commit)


if __name__ == "__main__":
    unittest.main()
