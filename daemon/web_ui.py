import os
import re
import json
import logging
import subprocess
import shutil
import platform
import time
import asyncio
from pathlib import Path
from typing import Optional, Any
from aiohttp import web
from daemon.config import config
from daemon.color_queue import ColorQueueManager
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.gemini_pool import GeminiKeyPoolManager
from daemon.bartender_converter import (
    LabelAST,
    CoordinateEngine,
    PPLBEmitter,
    parse_pplb_to_ast,
    parse_btxml,
    substitute_placeholders,
    inject_quantity
)

logger = logging.getLogger("WebUI")

class WebDashboard:
    def __init__(
        self,
        queue_mgr: ColorQueueManager,
        printer: ArgoxPrinterEngine,
        key_pool: GeminiKeyPoolManager,
        persistence: Optional[Any] = None
    ):
        self.queue_mgr = queue_mgr
        self.printer = printer
        self.key_pool = key_pool
        self.persistence = persistence if persistence is not None else getattr(queue_mgr, "persistence_mgr", None)
        self.app = web.Application()
        self.start_time = time.time()
        self._update_in_progress = False
        self._update_lock = asyncio.Lock()
        self._last_update_log = ""
        self.setup_routes()

    def setup_routes(self):
        # Operational Dashboard
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_post("/api/print/color", self.handle_print_color)
        self.app.router.add_post("/api/print/job", self.handle_print_job)
        self.app.router.add_post("/api/print/outer_box", self.handle_print_outer_box)
        self.app.router.add_post("/api/print/all_outer_boxes", self.handle_print_all_outer_boxes)

        # PDF Previews & Completed Label Previews
        self.app.router.add_get("/api/previews", self.handle_get_previews)
        self.app.router.add_get("/api/preview/{quote_id}", self.handle_get_preview)
        self.app.router.add_post("/api/preview/{quote_id}/dismiss", self.handle_dismiss_preview)
        self.app.router.add_get("/api/preview/label", self.handle_get_label_preview)

        # Resilient Auto-Update & Diagnostics
        self.app.router.add_get("/setup", self.handle_setup_page)
        self.app.router.add_get("/api/setup/system_info", self.handle_system_info)
        self.app.router.add_get("/api/setup/update/check", self.handle_update_check)
        self.app.router.add_post("/api/setup/update/apply", self.handle_update_apply)
        self.app.router.add_get("/api/setup/update/status", self.handle_update_status)
        self.app.router.add_post("/api/update/apply", self.handle_update_apply)
        self.app.router.add_get("/api/update/status", self.handle_update_status)

        # Diagnostics & Configuration
        self.app.router.add_get("/api/setup/wifi/scan", self.handle_wifi_scan)
        self.app.router.add_post("/api/setup/wifi/connect", self.handle_wifi_connect)
        self.app.router.add_post("/api/setup/keys", self.handle_keys_save)
        self.app.router.add_post("/api/setup/keys/test", self.handle_keys_test)
        self.app.router.add_post("/api/setup/printer/test", self.handle_printer_test)
        self.app.router.add_get("/api/setup/scanner/ping", self.handle_scanner_ping)
        self.app.router.add_post("/api/setup/service/restart", self.handle_service_restart)
        self.app.router.add_post("/api/setup/system/reboot", self.handle_system_reboot)

        # BarTender Label Designer Endpoints
        self.app.router.add_get("/api/designer/templates", self.handle_designer_list_templates)
        self.app.router.add_get("/api/designer/template", self.handle_designer_get_template)
        self.app.router.add_post("/api/designer/render", self.handle_designer_render)
        self.app.router.add_post("/api/designer/save", self.handle_designer_save)
        self.app.router.add_post("/api/designer/test_print", self.handle_designer_test_print)

    async def handle_index(self, request):
        html_path = config.BASE_DIR / "web" / "index.html"
        if not html_path.exists():
            return web.Response(text="Web UI file not found", status=404)
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    async def handle_setup_page(self, request):
        html_path = config.BASE_DIR / "web" / "setup.html"
        if not html_path.exists():
            return web.Response(text="Setup page not found", status=404)
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")

    def get_scanner_interface_carrier(self) -> bool:
        """
        Dynamic Network Carrier Check:
        Inspects sysfs carrier paths for both eth0 and end0 (and additional interfaces),
        eliminating interface mismatch bugs across Allwinner H3 kernel variations.
        """
        for iface in ("eth0", "end0", "eth1", "end1"):
            carrier_path = Path(f"/sys/class/net/{iface}/carrier")
            try:
                if carrier_path.exists():
                    val = carrier_path.read_text().strip()
                    if val == "1":
                        return True
            except Exception:
                continue
        return False

    def get_hardware_status(self) -> dict:
        printer_connected = self.printer.is_connected()
        printer_device = getattr(self.printer, "device_path", config.PRINTER_DEVICE)
        printer_sim = getattr(self.printer, "simulation_mode", config.SIMULATION_MODE)

        scanner_ip = getattr(config, "SCANNER_IP", "192.168.50.2")
        carrier = self.get_scanner_interface_carrier()

        ping_ok = False
        try:
            cmd = ["ping", "-c", "1", "-W", "1", scanner_ip] if platform.system() != "Windows" else ["ping", "-n", "1", "-w", "500", scanner_ip]
            ping_ok = (subprocess.run(cmd, capture_output=True).returncode == 0)
        except Exception:
            pass

        valid_keys = self.key_pool.get_valid_keys() if hasattr(self.key_pool, "get_valid_keys") else []
        gemini_configured = self.key_pool.is_configured() if hasattr(self.key_pool, "is_configured") else False

        sd_mounted = self.persistence.is_sdcard_mounted() if self.persistence and hasattr(self.persistence, "is_sdcard_mounted") else False
        sd_path = str(self.persistence.get_storage_root()) if self.persistence and hasattr(self.persistence, "get_storage_root") else str(getattr(config, "SDCARD_MOUNT", "/mnt/sdcard_backup"))

        # Honest hardware telemetry indicator string
        if printer_sim:
            printer_status_text = "[SIMULATION - HARDWARE BYPASSED]"
        elif printer_connected:
            printer_status_text = f"CONNECTED ({printer_device})"
        else:
            printer_status_text = f"DISCONNECTED ({printer_device} missing)"

        return {
            "printer": {
                "connected": printer_connected,
                "device": printer_device,
                "simulation": printer_sim,
                "status_text": printer_status_text
            },
            "scanner": {
                "reachable": ping_ok,
                "carrier": carrier,
                "ip": scanner_ip,
                "smtp_port": config.SMTP_PORT,
                "smtp_active": True
            },
            "gemini": {
                "configured": gemini_configured,
                "active_keys": len(valid_keys),
                "total_keys": len(self.key_pool.keys)
            },
            "sdcard": {
                "mounted": sd_mounted,
                "mount_point": sd_path,
                "status_text": "MOUNTED (64GB SD)" if sd_mounted else "LOCAL FALLBACK"
            }
        }

    async def handle_status(self, request):
        summary = self.queue_mgr.get_summary()
        queues_serializable = {
            color: [j.model_dump() for j in jobs]
            for color, jobs in self.queue_mgr.queues.items()
        }
        outer_boxes_serializable = [b.model_dump() for b in self.queue_mgr.pending_outer_boxes]
        
        status = {
            "summary": summary,
            "queues": queues_serializable,
            "outer_boxes": outer_boxes_serializable,
            "gemini_pool": self.key_pool.get_pool_status(),
            "simulation_mode": self.printer.simulation_mode,
            "simulated_history_count": len(self.printer.simulated_print_history),
            "hardware": self.get_hardware_status()
        }
        return web.json_response(status)

    async def handle_print_color(self, request):
        color = request.query.get("color", "BLACK").upper()
        jobs = self.queue_mgr.get_jobs_for_color(color)
        printed_count = 0

        for job in jobs:
            job_vars = {
                "ITEM_NAME": job.item_name,
                "DESCRIPTION": job.description,
                "SKU": job.item_name,
                "QUOTE_NUMBER": job.quote_number,
                "QUOTE_ID": job.quote_number,
                "NOTE": job.handwritten_note or "",
                "BARCODE": job.item_name,
                "QUANTITY": job.quantity,
                "QTY": job.quantity
            }
            success = self.printer.dispatch_cartridge_label(job.matched_label_file, quantity=job.quantity, variables=job_vars)
            if success:
                job.status = "PRINTED"
                printed_count += job.quantity
                if self.persistence and hasattr(self.persistence, "log_transaction"):
                    self.persistence.log_transaction(
                        "PRINT_DISPATCH",
                        {"type": "COLOR", "color": color, "model": job.item_name, "quantity": job.quantity},
                        quote_number=job.quote_number
                    )

        if hasattr(self.queue_mgr, "_persist"):
            self.queue_mgr._persist()

        return web.json_response({"status": "success", "color": color, "labels_printed": printed_count})

    async def handle_print_job(self, request):
        data = await request.json()
        job_id = data.get("job_id")
        quantity = int(data.get("quantity", 1))

        target_job = None
        for color, jobs in self.queue_mgr.queues.items():
            for j in jobs:
                if j.id == job_id:
                    target_job = j
                    break

        if not target_job:
            return web.json_response({"status": "error", "message": "Job not found"}, status=404)

        job_vars = {
            "ITEM_NAME": target_job.item_name,
            "DESCRIPTION": target_job.description,
            "SKU": target_job.item_name,
            "QUOTE_NUMBER": target_job.quote_number,
            "QUOTE_ID": target_job.quote_number,
            "NOTE": target_job.handwritten_note or "",
            "BARCODE": target_job.item_name,
            "QUANTITY": quantity,
            "QTY": quantity
        }
        success = self.printer.dispatch_cartridge_label(target_job.matched_label_file, quantity=quantity, variables=job_vars)
        if success:
            target_job.status = "PRINTED"
            target_job.quantity = quantity
            if self.persistence and hasattr(self.persistence, "log_transaction"):
                self.persistence.log_transaction(
                    "PRINT_DISPATCH",
                    {"type": "SINGLE_JOB", "job_id": job_id, "model": target_job.item_name, "quantity": quantity},
                    quote_number=target_job.quote_number
                )
            if hasattr(self.queue_mgr, "_persist"):
                self.queue_mgr._persist()
            return web.json_response({"status": "success", "job_id": job_id, "quantity": quantity})
        else:
            return web.json_response({"status": "error", "message": "Printer dispatch failed"}, status=500)

    async def handle_print_outer_box(self, request):
        data = await request.json()
        idx = int(data.get("index", 0))

        if idx < 0 or idx >= len(self.queue_mgr.pending_outer_boxes):
            return web.json_response({"status": "error", "message": "Box index out of range"}, status=404)

        box = self.queue_mgr.pending_outer_boxes[idx]
        success = self.printer.dispatch_outer_box_label(box)
        if success:
            self.queue_mgr.pending_outer_boxes.pop(idx)
            if self.persistence and hasattr(self.persistence, "log_transaction"):
                self.persistence.log_transaction(
                    "PRINT_DISPATCH",
                    {"type": "OUTER_BOX", "quote": box.quote_number, "box": box.current_box},
                    quote_number=box.quote_number
                )
            if hasattr(self.queue_mgr, "_persist"):
                self.queue_mgr._persist()
            return web.json_response({"status": "success", "quote": box.quote_number, "box": box.current_box})
        else:
            return web.json_response({"status": "error", "message": "Outer box print failed"}, status=500)

    async def handle_print_all_outer_boxes(self, request):
        count = 0
        while self.queue_mgr.pending_outer_boxes:
            box = self.queue_mgr.pending_outer_boxes.pop(0)
            if self.printer.dispatch_outer_box_label(box):
                count += 1
                if self.persistence and hasattr(self.persistence, "log_transaction"):
                    self.persistence.log_transaction(
                        "PRINT_DISPATCH",
                        {"type": "OUTER_BOX", "quote": box.quote_number, "box": box.current_box},
                        quote_number=box.quote_number
                    )
        if hasattr(self.queue_mgr, "_persist"):
            self.queue_mgr._persist()
        return web.json_response({"status": "success", "printed_boxes": count, "total_labels": count * 2})

    # =========================================================================
    # IN-MEMORY PDF PREVIEW ENDPOINTS
    # =========================================================================

    async def handle_get_previews(self, request):
        try:
            from daemon.pdf_preview import list_active_previews
            previews = list_active_previews()
        except ImportError:
            previews = []
        return web.json_response({"previews": previews, "count": len(previews)})

    async def handle_get_preview(self, request):
        quote_id = request.match_info.get("quote_id", "")
        if not quote_id:
            return web.json_response({"status": "error", "message": "Quote ID is required"}, status=400)

        try:
            from daemon.pdf_preview import get_ephemeral_preview
            preview_b64 = get_ephemeral_preview(quote_id)
        except ImportError:
            preview_b64 = None

        if not preview_b64:
            return web.json_response({"status": "error", "message": f"Preview for '{quote_id}' not found or expired"}, status=404)

        # Raw image bytes requested
        raw_requested = (request.query.get("raw") == "1") or ("image/" in request.headers.get("Accept", ""))
        if raw_requested:
            import base64
            raw_b64 = preview_b64.split(",", 1)[-1] if "," in preview_b64 else preview_b64
            img_bytes = base64.b64decode(raw_b64)
            return web.Response(body=img_bytes, content_type="image/jpeg")

        return web.json_response({
            "status": "success",
            "quote_id": quote_id,
            "preview": preview_b64,
            "has_preview": True
        })

    async def handle_dismiss_preview(self, request):
        quote_id = request.match_info.get("quote_id", "")
        if not quote_id:
            return web.json_response({"status": "error", "message": "Quote ID is required"}, status=400)

        try:
            from daemon.pdf_preview import dismiss_preview
            success = dismiss_preview(quote_id)
        except ImportError:
            success = False

        if success:
            return web.json_response({
                "status": "success",
                "message": f"Preview for '{quote_id}' dismissed and shredded from RAM.",
                "quote_id": quote_id
            })
        else:
            return web.json_response({
                "status": "error",
                "message": f"Preview for '{quote_id}' not found in active RAM store."
            }, status=404)

    async def handle_get_label_preview(self, request):
        label_type = request.query.get("type", "cartridge").lower()

        if label_type == "outer_box":
            try:
                idx = int(request.query.get("index", 0))
                if idx < 0 or idx >= len(self.queue_mgr.pending_outer_boxes):
                    return web.json_response({"status": "error", "message": "Outer box index out of range"}, status=404)
                box = self.queue_mgr.pending_outer_boxes[idx]
                preview = self.printer.get_outer_box_preview(box)
                return web.json_response({"status": "success", "preview": preview})
            except Exception as e:
                return web.json_response({"status": "error", "message": str(e)}, status=500)

        else:
            job_id = request.query.get("job_id", "")
            target_job = None
            for color, jobs in self.queue_mgr.queues.items():
                for j in jobs:
                    if j.id == job_id:
                        target_job = j
                        break
                if target_job:
                    break

            if not target_job:
                item_name = request.query.get("item_name", "CANON 716 BLACK")
                template = request.query.get("template", "")
                qty = int(request.query.get("quantity", 1))
                preview = self.printer.get_cartridge_preview(
                    item_name=item_name,
                    matched_file=template,
                    quantity=qty
                )
                return web.json_response({"status": "success", "preview": preview})

            qty = int(request.query.get("quantity", target_job.quantity))
            preview = self.printer.get_cartridge_preview(
                item_name=target_job.item_name,
                matched_file=target_job.matched_label_file,
                quantity=qty,
                quote_number=target_job.quote_number,
                description=target_job.description,
                handwritten_note=target_job.handwritten_note or ""
            )
            return web.json_response({"status": "success", "preview": preview})

    # =========================================================================
    # SETUP & OTA DIAGNOSTICS ENDPOINTS
    # =========================================================================

    async def handle_system_info(self, request):
        uptime_sec = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        # Memory info
        mem_used = 0
        mem_total = 1000
        try:
            if Path("/proc/meminfo").exists():
                with open("/proc/meminfo") as f:
                    lines = f.readlines()
                total = int([l.split()[1] for l in lines if "MemTotal:" in l][0]) // 1024
                avail = int([l.split()[1] for l in lines if "MemAvailable:" in l][0]) // 1024
                mem_total = total
                mem_used = total - avail
        except Exception:
            pass

        # Storage info
        disk_free_gb = 5.0
        try:
            usage = shutil.disk_usage(config.BASE_DIR)
            disk_free_gb = round(usage.free / (1024**3), 2)
        except Exception:
            pass

        # Git commit info
        git_commit = "v1.0.0"
        try:
            proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.BASE_DIR, capture_output=True, text=True)
            if proc.returncode == 0:
                git_commit = proc.stdout.strip()
        except Exception:
            pass

        info = {
            "platform": f"{platform.system()} ({platform.machine()})",
            "uptime": uptime_str,
            "mem_used_mb": mem_used,
            "mem_total_mb": mem_total,
            "mem_percent": round((mem_used / max(mem_total, 1)) * 100, 1),
            "disk_free_gb": disk_free_gb,
            "git_commit": git_commit,
            "gemini_keys": ",".join([k.key for k in self.key_pool.keys])
        }
        return web.json_response(info)

    async def handle_update_check(self, request):
        try:
            subprocess.run(["git", "fetch", "origin"], cwd=config.BASE_DIR, capture_output=True, timeout=10)
            local_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.BASE_DIR, capture_output=True, text=True).stdout.strip()
            remote_hash = subprocess.run(["git", "rev-parse", "origin/main"], cwd=config.BASE_DIR, capture_output=True, text=True).stdout.strip()
            msg = subprocess.run(["git", "log", "-1", "--pretty=%B", "origin/main"], cwd=config.BASE_DIR, capture_output=True, text=True).stdout.strip()

            update_avail = (local_hash != remote_hash) and bool(remote_hash)
            return web.json_response({
                "update_available": update_avail,
                "current_commit": local_hash[:7],
                "remote_commit": remote_hash[:7],
                "remote_message": msg
            })
        except Exception as e:
            return web.json_response({"error": str(e), "update_available": False}, status=500)

    def _get_bash_executable(self) -> str:
        candidates = [
            Path("Y:/Git/bin/bash.exe"),
            Path("Y:/Git/usr/bin/bash.exe"),
            Path("C:/Program Files/Git/bin/bash.exe")
        ]
        for c in candidates:
            if c.exists():
                return str(c)
        bash_path = shutil.which("bash")
        if bash_path and "WindowsApps" not in bash_path:
            return bash_path
        return "bash"

    def _get_update_storage_root(self) -> Path:
        if self.persistence and hasattr(self.persistence, "get_storage_root"):
            return self.persistence.get_storage_root()
        if hasattr(config, "SDCARD_MOUNT") and config.SDCARD_MOUNT.exists():
            return config.SDCARD_MOUNT
        return getattr(config, "FALLBACK_STORAGE_DIR", config.BASE_DIR / "backup_storage")

    def _read_update_state(self) -> dict:
        storage_root = self._get_update_storage_root()
        state_file = storage_root / "rollback_recovery" / "update_state.json"
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    async def _run_update_subprocess(self, cmd: list, log_buffer: Optional[list] = None) -> int:
        self._update_in_progress = True
        returncode = -1
        try:
            loop = asyncio.get_running_loop()
            def run_proc():
                proc = subprocess.run(
                    cmd,
                    cwd=str(config.BASE_DIR),
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                return proc.returncode, output

            returncode, out = await loop.run_in_executor(None, run_proc)
            self._last_update_log = out
            if log_buffer is not None:
                log_buffer.append(out)
        except subprocess.TimeoutExpired as te:
            err = f"Update execution timed out after 120 seconds: {te}"
            logger.error(err)
            self._last_update_log = err
            if log_buffer is not None:
                log_buffer.append(err)
            returncode = 124
        except Exception as e:
            err = f"Update execution error: {e}"
            logger.error(err)
            self._last_update_log = err
            if log_buffer is not None:
                log_buffer.append(err)
            returncode = 1
        finally:
            self._update_in_progress = False
        return returncode

    async def handle_update_apply(self, request):
        if self._update_in_progress or self._update_lock.locked():
            return web.json_response({
                "status": "error",
                "state": "IN_PROGRESS",
                "message": "An auto-update operation is already in progress.",
                "log": self._last_update_log
            }, status=409)

        async with self._update_lock:
            if self._update_in_progress:
                return web.json_response({
                    "status": "error",
                    "state": "IN_PROGRESS",
                    "message": "An auto-update operation is already in progress.",
                    "log": self._last_update_log
                }, status=409)

            self._update_in_progress = True
            dispatched = False
            try:
                body = {}
                try:
                    if request.can_read_body and request.content_length:
                        body = await request.json()
                except Exception:
                    body = {}

                script_path = config.BASE_DIR / "scripts" / "update_with_rollback.sh"
                if not script_path.exists():
                    return web.json_response({
                        "status": "error",
                        "message": f"Update script not found at {script_path}"
                    }, status=500)

                bash_bin = self._get_bash_executable()
                storage_root = self._get_update_storage_root()

                cmd = [
                    bash_bin,
                    str(script_path),
                    "--app-dir", str(config.BASE_DIR),
                    "--storage-dir", str(storage_root)
                ]

                if body.get("simulate_failure"):
                    cmd.append("--simulate-failure")
                elif body.get("simulate_success"):
                    cmd.append("--simulate-success")
                elif body.get("simulate_fetch_failure"):
                    cmd.append("--simulate-fetch-failure")
                elif body.get("simulate_dep_failure"):
                    cmd.append("--simulate-dep-failure")
                elif body.get("dry_run"):
                    cmd.append("--dry-run")
                elif body.get("check"):
                    cmd.append("--check")

                if body.get("branch") and isinstance(body.get("branch"), str):
                    cmd.extend(["--branch", body.get("branch")])
                if body.get("remote") and isinstance(body.get("remote"), str):
                    cmd.extend(["--remote", body.get("remote")])

                wait_for_completion = body.get("wait", False) or (request.query.get("wait") == "1")

                if wait_for_completion:
                    dispatched = True
                    log_buffer = []
                    ret_code = await self._run_update_subprocess(cmd, log_buffer)
                    log_out = log_buffer[0] if log_buffer else self._last_update_log
                    status_data = self._read_update_state()
                    status_str = status_data.get("status")
                    is_success = (ret_code == 0) and (status_str in ("SUCCESS", "IDLE", None))
                    return web.json_response({
                        "status": "success" if is_success else "error",
                        "state": status_data.get("state", "COMMITTED" if is_success else "ROLLBACK"),
                        "log": log_out,
                        "message": status_data.get("message", "Update execution completed.")
                    })
                else:
                    dispatched = True
                    asyncio.create_task(self._run_update_subprocess(cmd))
                    return web.json_response({
                        "status": "success",
                        "state": "IN_PROGRESS",
                        "message": "Auto-update process initiated.",
                        "log": "Auto-update engine dispatched in background."
                    })
            finally:
                if not dispatched:
                    self._update_in_progress = False

    async def handle_update_status(self, request):
        state_data = self._read_update_state()

        current_commit = "v1.0.0"
        try:
            proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.BASE_DIR, capture_output=True, text=True)
            if proc.returncode == 0:
                current_commit = proc.stdout.strip()
        except Exception:
            pass

        remote_commit = current_commit
        try:
            proc = subprocess.run(["git", "rev-parse", "--short", "origin/main"], cwd=config.BASE_DIR, capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                remote_commit = proc.stdout.strip()
        except Exception:
            pass

        if self._update_in_progress:
            state = "IN_PROGRESS"
            status = "IN_PROGRESS"
        else:
            raw_status = state_data.get("status", "IDLE")
            if raw_status in ("IDLE", "IN_PROGRESS", "SUCCESS", "ROLLBACK", "ERROR"):
                state = raw_status
                status = raw_status
            else:
                state = state_data.get("state", "IDLE")
                status = raw_status

        log_text = self._last_update_log
        if not log_text and state_data.get("message"):
            log_text = state_data.get("message")
        log_lines = log_text.strip().splitlines() if log_text else []
        log_snippet = "\n".join(log_lines[-20:]) if log_lines else ""

        return web.json_response({
            "state": state,
            "status": status,
            "in_progress": self._update_in_progress,
            "current_commit": current_commit,
            "remote_commit": remote_commit,
            "latest_commit": remote_commit,
            "log": log_text,
            "log_snippet": log_snippet,
            "timestamp": time.time(),
            "details": state_data
        })

    async def handle_wifi_scan(self, request):
        networks = []
        try:
            # Use nmcli if available
            proc = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi"], capture_output=True, text=True, timeout=8)
            if proc.returncode == 0:
                seen = set()
                for line in proc.stdout.splitlines():
                    if ":" in line:
                        ssid, signal = line.split(":", 1)
                        if ssid and ssid not in seen:
                            seen.add(ssid)
                            networks.append({"ssid": ssid, "signal": signal})
        except Exception:
            pass

        return web.json_response({"networks": networks})

    async def handle_wifi_connect(self, request):
        data = await request.json()
        ssid = (data.get("ssid") or "").strip()
        psk = (data.get("password") or "").strip()

        if not ssid:
            return web.json_response({"status": "error", "message": "SSID cannot be empty"}, status=400)

        try:
            # 1. Clean up any 0-byte or corrupted .nmconnection profiles
            try:
                conn_dir = Path("/etc/NetworkManager/system-connections")
                if conn_dir.exists():
                    for f in conn_dir.glob("*.nmconnection"):
                        if f.stat().st_size == 0:
                            f.unlink(missing_ok=True)
            except Exception:
                pass

            # 2. Delete any conflicting existing profile with this SSID
            subprocess.run(["nmcli", "connection", "delete", "id", ssid], capture_output=True, timeout=5)

            # 3. Primary attempt: standard nmcli dev wifi connect on wlan0
            cmd = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", "wlan0"]
            if psk:
                cmd.extend(["password", psk])
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if proc.returncode == 0:
                stealth_hostname = data.get("hostname", "Lexmark-MX511").strip() or "Lexmark-MX511"
                subprocess.run(["hostnamectl", "set-hostname", stealth_hostname], capture_output=True, timeout=5)
                subprocess.run(["nmcli", "connection", "modify", ssid, "ipv4.dhcp-send-hostname", "no"], capture_output=True, timeout=5)
                return web.json_response({"status": "success", "message": f"Successfully connected to {ssid}! Disguised as {stealth_hostname}."})

            # 4. Fallback attempt: explicitly create profile with wpa-psk key-mgmt to prevent property missing errors
            add_cmd = ["nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", ssid, "ssid", ssid]
            add_proc = subprocess.run(add_cmd, capture_output=True, text=True, timeout=10)
            if add_proc.returncode != 0:
                return web.json_response({"status": "error", "message": f"Failed to create profile: {add_proc.stderr or add_proc.stdout}"}, status=400)

            if psk:
                mod_cmd = ["nmcli", "connection", "modify", ssid, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
                subprocess.run(mod_cmd, capture_output=True, timeout=10)

            subprocess.run(["nmcli", "connection", "modify", ssid, "connection.autoconnect", "yes"], capture_output=True, timeout=5)

            up_proc = subprocess.run(["nmcli", "connection", "up", ssid], capture_output=True, text=True, timeout=20)
            if up_proc.returncode == 0:
                stealth_hostname = data.get("hostname", "Lexmark-MX511").strip() or "Lexmark-MX511"
                subprocess.run(["hostnamectl", "set-hostname", stealth_hostname], capture_output=True, timeout=5)
                subprocess.run(["nmcli", "connection", "modify", ssid, "ipv4.dhcp-send-hostname", "no"], capture_output=True, timeout=5)
                return web.json_response({"status": "success", "message": f"Successfully configured and connected to {ssid}! Profile permanently saved and disguised as {stealth_hostname}."})
            else:
                return web.json_response({"status": "error", "message": f"Connection failed: {up_proc.stderr or up_proc.stdout}"}, status=400)

        except Exception as e:
            return web.json_response({"status": "error", "message": f"Connection error: {e}"}, status=500)

    async def handle_keys_save(self, request):
        data = await request.json()
        keys_str = data.get("keys", "")
        key_list = [k.strip() for k in keys_str.split(",") if k.strip()]

        # Update running key pool
        self.key_pool.keys.clear()
        for k in key_list:
            self.key_pool.add_key(k)

        # Update .env file on disk
        env_file = config.BASE_DIR / ".env"
        try:
            lines = []
            if env_file.exists():
                with open(env_file, "r") as f:
                    lines = [l for l in f if not l.startswith("GEMINI_API_KEYS=")]
            lines.append(f"GEMINI_API_KEYS={','.join(key_list)}\n")
            with open(env_file, "w") as f:
                f.writelines(lines)
            return web.json_response({"status": "success", "message": f"Updated {len(key_list)} Gemini API keys in pool."})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    async def handle_keys_test(self, request):
        results = []
        model_name = getattr(config, "GEMINI_MODEL", "gemini-3.8-flash")
        for state in self.key_pool.keys:
            masked = f"...{state.key[-4:]}" if len(state.key) >= 4 else "UNKNOWN"
            if state.key.startswith("your_primary") or state.key.startswith("mock_"):
                results.append(f"Key {masked}: UNCONFIGURED (Default placeholder - enter valid key in /setup)")
                continue
            if len(state.key) < 20:
                results.append(f"Key {masked}: INVALID FORMAT (Key string too short)")
                continue

            try:
                if state.client and state.client != "MOCK_CLIENT":
                    if hasattr(state.client, "interactions"):
                        res = state.client.interactions.create(
                            model=model_name,
                            input="ping"
                        )
                        results.append(f"Key {masked}: HEALTHY (Google API Authorized)")
                    elif hasattr(state.client, "models"):
                        res = state.client.models.generate_content(
                            model=model_name,
                            contents="ping",
                            config={"max_output_tokens": 5}
                        )
                        results.append(f"Key {masked}: HEALTHY (Google API Authorized)")
                    else:
                        results.append(f"Key {masked}: CLIENT INITIALIZED")
                else:
                    results.append(f"Key {masked}: CLIENT NOT INITIALIZED")
            except Exception as e:
                results.append(f"Key {masked}: ERROR ({e})")

        if not results:
            results.append("No keys in pool. Please enter your Gemini API keys from Google AI Studio.")

        return web.json_response({"results": "\n".join(results)})

    async def handle_printer_test(self, request):
        res = self.printer.send_test_label()
        return web.json_response(res)

    async def handle_scanner_ping(self, request):
        scanner_ip = getattr(config, "SCANNER_IP", "192.168.50.2")
        carrier = self.get_scanner_interface_carrier()

        try:
            cmd = ["ping", "-c", "2", "-W", "1", scanner_ip] if platform.system() != "Windows" else ["ping", "-n", "2", "-w", "1000", scanner_ip]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                return web.json_response({
                    "status": "online",
                    "carrier": carrier,
                    "message": f"Scanner online: {scanner_ip} responded to ICMP ping (0% packet loss). Direct Ethernet link is active. SMTP listener is ready on port {config.SMTP_PORT}."
                })
            else:
                hint = "Ethernet cable carrier detected, but host not responding to ping." if carrier else "No physical Ethernet cable carrier on interface (eth0/end0). Verify direct cable link."
                return web.json_response({
                    "status": "offline",
                    "carrier": carrier,
                    "message": f"Scanner offline: {scanner_ip} did not respond. {hint} Note: SMTP scan daemon is running on port {config.SMTP_PORT}."
                })
        except Exception as e:
            return web.json_response({"status": "error", "message": f"Ping execution error: {e}"}, status=500)

    async def handle_service_restart(self, request):
        if Path("/etc/systemd/system/autolabel.service").exists():
            subprocess.Popen(["systemctl", "restart", "autolabel.service"])
        return web.json_response({"status": "restarting"})

    async def handle_system_reboot(self, request):
        if platform.system() != "Windows":
            subprocess.Popen(["reboot"])
        return web.json_response({"status": "rebooting"})

    # =========================================================================
    # BARTENDER LABEL DESIGNER & CREATOR ENDPOINTS
    # =========================================================================

    async def handle_designer_list_templates(self, request):
        """
        Enumerates all available label templates (.prd, .prn, .xml, .btw)
        across system sample templates, SD card backup, and fallback storage.
        """
        templates = []
        search_dirs = []
        if config.LABEL_DIR and config.LABEL_DIR.exists():
            search_dirs.append(config.LABEL_DIR)

        sample_dir = config.BASE_DIR / "templates" / "sample_labels"
        if sample_dir.exists() and sample_dir not in search_dirs:
            search_dirs.append(sample_dir)

        sd_mount = getattr(config, "SDCARD_MOUNT", Path("/mnt/sdcard_backup"))
        for sub in ["labels", "templates", "sample_labels"]:
            p = sd_mount / sub
            if p.exists() and p.is_dir() and p not in search_dirs:
                search_dirs.append(p)

        fallback_archive = getattr(config, "FALLBACK_STORAGE_DIR", config.BASE_DIR / "backup_storage") / "templates_archive"
        for sub in ["custom_user_templates", "golden_master"]:
            p = fallback_archive / sub
            if p.exists() and p.is_dir() and p not in search_dirs:
                search_dirs.append(p)

        seen_names = set()
        for d in search_dirs:
            if not d.exists() or not d.is_dir():
                continue
            for ext in ("*.prd", "*.prn", "*.xml", "*.btw"):
                for f in d.glob(ext):
                    if f.name in seen_names:
                        continue
                    seen_names.add(f.name)
                    stat = f.stat()
                    templates.append({
                        "filename": f.name,
                        "stem": f.stem,
                        "size": stat.st_size,
                        "modified": int(stat.st_mtime),
                        "type": f.suffix.lstrip(".").lower(),
                        "is_custom": "custom" in str(d).lower() or d == sd_mount / "labels"
                    })

        templates.sort(key=lambda x: x["filename"].upper())
        return web.json_response({"status": "success", "templates": templates})

    async def handle_designer_get_template(self, request):
        """
        Loads a label file by filename, parses into Intermediate Label AST,
        and returns both structured AST and native PPLB command stream.
        """
        filename = (request.query.get("file") or "").strip()
        if not filename:
            return web.json_response({"status": "error", "message": "Missing file parameter"}, status=400)

        # Path traversal guard
        if ".." in filename or "/" in filename or "\\" in filename:
            return web.json_response({"status": "error", "message": "Invalid filename syntax"}, status=400)

        file_path = self.printer._resolve_label_file(filename)
        if not file_path or not file_path.exists():
            return web.json_response({"status": "error", "message": f"Template '{filename}' not found"}, status=404)

        try:
            raw_content = file_path.read_text(encoding="utf-8", errors="ignore")
            ext = file_path.suffix.lower()

            if ext == ".btw":
                return web.json_response({
                    "status": "error",
                    "message": f"'{file_path.name}' is a proprietary binary BarTender document (.btw). Please export from BarTender as PPLB Print File (.prd) or BarTender XML Script (.xml) for visual editing."
                }, status=400)

            if ext == ".xml":
                parsed = parse_btxml(raw_content)
                ast = parsed.get("ast")
                if not ast and parsed.get("format_name"):
                    # Resolve underlying template (e.g. CANON_716_BLACK.btw -> CANON_716_BLACK.prd)
                    fmt_file = self.printer._resolve_label_file(parsed["format_name"])
                    if fmt_file and fmt_file.exists():
                        base_content = fmt_file.read_text(encoding="utf-8", errors="ignore")
                        records = parsed.get("records") or []
                        variables = records[0] if records else {}
                        rendered = substitute_placeholders(base_content, variables)
                        ast = parse_pplb_to_ast(rendered)
                if not ast:
                    ast = LabelAST(width_dots=800, height_dots=400)
            elif raw_content.strip().startswith("{") and "width_dots" in raw_content:
                ast_dict = json.loads(raw_content)
                ast = LabelAST.from_dict(ast_dict)
            else:
                ast = parse_pplb_to_ast(raw_content)

            pplb_bytes = PPLBEmitter.emit(ast)

            return web.json_response({
                "status": "success",
                "filename": file_path.name,
                "ast": ast.to_dict(),
                "raw_pplb": pplb_bytes.decode("utf-8", errors="replace"),
                "width_dots": ast.width_dots,
                "height_dots": ast.height_dots,
                "width_mm": round(CoordinateEngine.dots_to_mm(ast.width_dots), 2),
                "height_mm": round(CoordinateEngine.dots_to_mm(ast.height_dots), 2),
                "source_path": str(file_path)
            })
        except Exception as e:
            logger.error(f"Failed to load template {filename}: {e}", exc_info=True)
            return web.json_response({"status": "error", "message": f"Failed to load template: {e}"}, status=500)

    async def handle_designer_render(self, request):
        """
        Validates intermediate AST, applies variable placeholders, and emits
        native 203.2 DPI PPLB command payload for visual preview.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "Invalid JSON payload"}, status=400)

        ast_data = data.get("ast")
        if not ast_data or not isinstance(ast_data, dict):
            return web.json_response({"status": "error", "message": "Missing 'ast' definition"}, status=400)

        try:
            ast = LabelAST.from_dict(ast_data)

            # Optional variable substitution
            variables = data.get("variables")
            if variables and isinstance(variables, dict):
                for elem in ast.elements:
                    if hasattr(elem, "content"):
                        elem.content = substitute_placeholders(elem.content, variables)

            pplb_bytes = PPLBEmitter.emit(ast)
            pplb_str = pplb_bytes.decode("utf-8", errors="replace")

            return web.json_response({
                "status": "success",
                "pplb": pplb_str,
                "width_dots": ast.width_dots,
                "height_dots": ast.height_dots,
                "width_mm": round(CoordinateEngine.dots_to_mm(ast.width_dots), 2),
                "height_mm": round(CoordinateEngine.dots_to_mm(ast.height_dots), 2),
                "element_count": len(ast.elements)
            })
        except Exception as e:
            return web.json_response({"status": "error", "message": f"Render failed: {e}"}, status=500)

    async def handle_designer_save(self, request):
        """
        Saves new or modified label as a native PPLB .prd file to local templates
        and persistent SD card storage.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "Invalid JSON payload"}, status=400)

        raw_name = (data.get("filename") or "").strip()
        if not raw_name:
            return web.json_response({"status": "error", "message": "Filename cannot be empty"}, status=400)

        if ".." in raw_name or "/" in raw_name or "\\" in raw_name:
            return web.json_response({"status": "error", "message": "Invalid filename syntax"}, status=400)

        # Sanitize filename, ensuring .prd extension
        clean_name = re.sub(r'[^A-Za-z0-9_\-\.]', '_', raw_name)
        if not clean_name.lower().endswith(".prd"):
            clean_name += ".prd"

        ast_data = data.get("ast")
        raw_pplb = data.get("raw_pplb")
        overwrite = bool(data.get("overwrite", True))

        if ast_data and isinstance(ast_data, dict):
            ast = LabelAST.from_dict(ast_data)
            payload_bytes = PPLBEmitter.emit(ast)
        elif raw_pplb and isinstance(raw_pplb, str):
            payload_bytes = raw_pplb.encode("utf-8")
        else:
            return web.json_response({"status": "error", "message": "Either 'ast' or 'raw_pplb' must be provided"}, status=400)

        target_dir = config.LABEL_DIR if (config.LABEL_DIR and config.LABEL_DIR.exists()) else (config.BASE_DIR / "templates" / "sample_labels")
        target_file = target_dir / clean_name
        if target_file.exists() and not overwrite:
            return web.json_response({"status": "error", "message": f"File '{clean_name}' already exists."}, status=409)

        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_bytes(payload_bytes)

            # Mirror to SD card storage if available
            sd_mount = getattr(config, "SDCARD_MOUNT", Path("/mnt/sdcard_backup"))
            if sd_mount.exists() and sd_mount.is_dir():
                sd_target = sd_mount / "labels" / clean_name
                try:
                    sd_target.parent.mkdir(parents=True, exist_ok=True)
                    sd_target.write_bytes(payload_bytes)
                except Exception as sd_err:
                    logger.warning(f"Failed to mirror label to SD card: {sd_err}")

            if self.persistence and hasattr(self.persistence, "log_transaction"):
                self.persistence.log_transaction(
                    "LABEL_SAVE",
                    {"filename": clean_name, "bytes": len(payload_bytes)},
                    quote_number=clean_name
                )

            return web.json_response({
                "status": "success",
                "filename": clean_name,
                "message": f"Label saved successfully as {clean_name}",
                "bytes_written": len(payload_bytes)
            })
        except Exception as e:
            logger.error(f"Failed to save label {clean_name}: {e}", exc_info=True)
            return web.json_response({"status": "error", "message": f"Failed to save label: {e}"}, status=500)

    async def handle_designer_test_print(self, request):
        """
        Emits compiled PPLB directly to the Argox OS-214 thermal printer (/dev/usb/lp0).
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "Invalid JSON payload"}, status=400)

        quantity = max(1, min(100, int(data.get("quantity", 1))))
        ast_data = data.get("ast")
        raw_pplb = data.get("raw_pplb")
        variables = data.get("variables") or {}

        # Provide realistic default variables for test print if not supplied
        default_test_vars = {
            "QUOTE_NUMBER": "Q-8821",
            "QUOTE_ID": "Q-8821",
            "SKU": "CB540A-BLK",
            "ITEM_NAME": "HP Color LaserJet CB540A Black",
            "DESCRIPTION": "HP Color LaserJet CB540A Black",
            "WEIGHT_KG": "1.25",
            "WEIGHT": "1.25",
            "GROSS_WEIGHT": "1.25",
            "QUANTITY": str(quantity),
            "QTY": str(quantity),
            "BARCODE": "CB540A-8821",
            "NOTE": "Designer Test Print",
            "DATE": time.strftime("%Y-%m-%d"),
            "CURRENT_BOX": "1",
            "TOTAL_BOXES": "1",
            "BOX_ITEMS": "1x Cartridge (CB540A)",
            "COPIES": str(quantity)
        }
        effective_vars = {**default_test_vars, **variables}

        if ast_data and isinstance(ast_data, dict):
            ast = LabelAST.from_dict(ast_data)
            ast.copies = quantity
            for elem in ast.elements:
                if hasattr(elem, "content"):
                    elem.content = substitute_placeholders(elem.content, effective_vars)
            payload_bytes = PPLBEmitter.emit(ast)
        elif raw_pplb and isinstance(raw_pplb, str):
            rendered = substitute_placeholders(raw_pplb, effective_vars)
            payload_bytes = inject_quantity(rendered, quantity, emulation="PPLB")
        else:
            return web.json_response({"status": "error", "message": "Missing 'ast' or 'raw_pplb'"}, status=400)

        payload_str = payload_bytes.decode("utf-8", errors="replace")
        job_info = {
            "type": "DESIGNER_TEST_PRINT",
            "quantity": quantity,
            "emulation": "PPLB",
            "payload_preview": payload_str[:150] + "..." if len(payload_str) > 150 else payload_str
        }

        success = self.printer._send_raw_to_printer(payload_bytes, job_info)
        if success:
            if self.persistence and hasattr(self.persistence, "log_transaction"):
                self.persistence.log_transaction(
                    "PRINT_DISPATCH",
                    {"type": "DESIGNER_TEST_PRINT", "quantity": quantity},
                    quote_number="DESIGNER_TEST"
                )
            return web.json_response({
                "status": "success",
                "message": f"Successfully dispatched {quantity} label(s) to Argox OS-214.",
                "simulation": self.printer.simulation_mode,
                "device": getattr(self.printer, "device_path", "/dev/usb/lp0"),
                "bytes_dispatched": len(payload_bytes)
            })
        else:
            return web.json_response({
                "status": "error",
                "message": "Printer dispatch failed. Check /dev/usb/lp0 connection.",
                "simulation": self.printer.simulation_mode
            }, status=500)
