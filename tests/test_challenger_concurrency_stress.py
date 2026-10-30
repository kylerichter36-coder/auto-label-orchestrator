import os
import json
import time
import shutil
import tempfile
import unittest
import asyncio
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from daemon.config import config
from daemon.web_ui import WebDashboard
from daemon.color_queue import ColorQueueManager
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.persistence import PersistenceManager


class TestChallengerConcurrencyStress(unittest.IsolatedAsyncioTestCase):
    """
    Adversarial stress harness for WebDashboard update concurrency and state machine.
    Challenger: challenger_m4_1_iter2
    """

    async def asyncSetUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="adv_stress_test_"))
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

    async def test_extreme_concurrency_burst_50(self):
        """
        Adversarial test: 50 simultaneous POST /api/update/apply requests.
        Verify that exactly 1 request is accepted (HTTP 200) and 49 are rejected (HTTP 409).
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            async def send_apply():
                return await client.post(
                    "/api/update/apply",
                    json={"simulate_success": True, "wait": False}
                )

            responses = await asyncio.gather(*[send_apply() for _ in range(50)])
            status_codes = [r.status for r in responses]

            num_accepted = status_codes.count(200)
            num_conflicts = status_codes.count(409)

            print(f"\n[STRESS 50 RESULT] Accepted (200) = {num_accepted}, Rejected (409) = {num_conflicts}")
            self.assertEqual(
                num_accepted, 1,
                f"STRESS FAIL: Exactly 1 update request must be accepted, got {num_accepted}"
            )
            self.assertEqual(
                num_conflicts, 49,
                f"STRESS FAIL: Exactly 49 update requests must be rejected with 409, got {num_conflicts}"
            )

    async def test_cross_endpoint_concurrency_burst_40(self):
        """
        Adversarial test: 20 requests to /api/update/apply and 20 to /api/setup/update/apply simultaneously.
        Verify strict mutual exclusion across alias endpoints under heavy load.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            reqs = []
            for i in range(40):
                path = "/api/update/apply" if i % 2 == 0 else "/api/setup/update/apply"
                reqs.append(client.post(path, json={"simulate_success": True, "wait": False}))

            responses = await asyncio.gather(*reqs)
            status_codes = [r.status for r in responses]

            num_accepted = status_codes.count(200)
            num_conflicts = status_codes.count(409)

            print(f"\n[STRESS CROSS 40 RESULT] Accepted = {num_accepted}, Rejected = {num_conflicts}")
            self.assertEqual(
                num_accepted, 1,
                f"STRESS FAIL: Exactly 1 cross-endpoint request must be accepted, got {num_accepted}"
            )
            self.assertEqual(
                num_conflicts, 39,
                f"STRESS FAIL: Exactly 39 cross-endpoint requests must be rejected with 409, got {num_conflicts}"
            )

    async def test_sequential_update_cycles_no_deadlock(self):
        """
        Adversarial test: Execute sequential update cycles.
        Verify lock releases cleanly and subsequent updates are accepted without deadlock.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            for cycle in range(3):
                # Verify lock is not held prior to invocation
                self.assertFalse(self.dashboard._update_lock.locked())
                self.assertFalse(self.dashboard._update_in_progress)

                resp = await client.post(
                    "/api/update/apply",
                    json={"simulate_success": True, "wait": True}
                )
                self.assertEqual(resp.status, 200, f"Cycle {cycle} failed to receive 200 OK")
                data = await resp.json()
                self.assertEqual(data["status"], "success", f"Cycle {cycle} status unexpected: {data}")

                # After completion, verify lock is released
                self.assertFalse(self.dashboard._update_lock.locked(), f"Cycle {cycle} left lock acquired!")
                self.assertFalse(self.dashboard._update_in_progress, f"Cycle {cycle} left update_in_progress True!")

    async def test_concurrent_update_and_rapid_status_polling(self):
        """
        Adversarial test: Rapid concurrent status polling while update is dispatched.
        Verify that status endpoints do not deadlock with the update lock.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            async def poll_status(path):
                return await client.get(path)

            async def trigger_update():
                return await client.post("/api/update/apply", json={"simulate_success": True, "wait": False})

            poll_tasks = [poll_status("/api/update/status" if i % 2 == 0 else "/api/setup/update/status") for i in range(30)]
            all_tasks = [trigger_update()] + poll_tasks

            results = await asyncio.gather(*all_tasks)
            update_resp = results[0]
            poll_resps = results[1:]

            self.assertEqual(update_resp.status, 200)
            for p in poll_resps:
                self.assertEqual(p.status, 200, "Status endpoint returned non-200 during concurrent update")
                body = await p.json()
                self.assertIn("current_commit", body)

    async def test_error_resets_lock_cleanly(self):
        """
        Adversarial test: Verify that if an update fails (e.g. simulate_failure),
        the lock and in_progress flags are cleanly reset and a subsequent update succeeds.
        """
        async with TestClient(TestServer(self.dashboard.app)) as client:
            # 1. Trigger failure with wait=True
            resp_fail = await client.post(
                "/api/update/apply",
                json={"simulate_failure": True, "wait": True}
            )
            self.assertEqual(resp_fail.status, 200)
            data_fail = await resp_fail.json()
            self.assertEqual(data_fail["status"], "error")

            # Verify lock released
            self.assertFalse(self.dashboard._update_lock.locked())
            self.assertFalse(self.dashboard._update_in_progress)

            # 2. Trigger success immediately after failure
            resp_succ = await client.post(
                "/api/update/apply",
                json={"simulate_success": True, "wait": True}
            )
            self.assertEqual(resp_succ.status, 200)
            data_succ = await resp_succ.json()
            self.assertEqual(data_succ["status"], "success")

            self.assertFalse(self.dashboard._update_lock.locked())
            self.assertFalse(self.dashboard._update_in_progress)


if __name__ == "__main__":
    unittest.main()
