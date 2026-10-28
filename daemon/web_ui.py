import os
import json
import logging
from aiohttp import web
from pathlib import Path
from daemon.config import config
from daemon.color_queue import ColorQueueManager
from daemon.printer_engine import ArgoxPrinterEngine
from daemon.gemini_pool import GeminiKeyPoolManager

logger = logging.getLogger("WebUI")

class WebDashboard:
    def __init__(self, queue_mgr: ColorQueueManager, printer: ArgoxPrinterEngine, key_pool: GeminiKeyPoolManager):
        self.queue_mgr = queue_mgr
        self.printer = printer
        self.key_pool = key_pool
        self.app = web.Application()
        self.setup_routes()

    def setup_routes(self):
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_post("/api/print/color", self.handle_print_color)
        self.app.router.add_post("/api/print/job", self.handle_print_job)
        self.app.router.add_post("/api/print/outer_box", self.handle_print_outer_box)
        self.app.router.add_post("/api/print/all_outer_boxes", self.handle_print_all_outer_boxes)

    async def handle_index(self, request):
        html_path = config.BASE_DIR / "web" / "index.html"
        if not html_path.exists():
            return web.Response(text="Web UI file not found", status=404)
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")

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
            "simulated_history_count": len(self.printer.simulated_print_history)
        }
        return web.json_response(status)

    async def handle_print_color(self, request):
        color = request.query.get("color", "BLACK").upper()
        jobs = self.queue_mgr.get_jobs_for_color(color)
        printed_count = 0

        for job in jobs:
            success = self.printer.dispatch_cartridge_label(job.matched_label_file, quantity=job.quantity)
            if success:
                job.status = "PRINTED"
                printed_count += job.quantity

        logger.info(f"Printed batch for {color}: {printed_count} total labels dispatched.")
        return web.json_response({"status": "success", "color": color, "labels_printed": printed_count})

    async def handle_print_job(self, request):
        data = await request.json()
        job_id = data.get("job_id")
        quantity = int(data.get("quantity", 1))

        # Find job across queues
        target_job = None
        for color, jobs in self.queue_mgr.queues.items():
            for j in jobs:
                if j.id == job_id:
                    target_job = j
                    break

        if not target_job:
            return web.json_response({"status": "error", "message": "Job not found"}, status=404)

        success = self.printer.dispatch_cartridge_label(target_job.matched_label_file, quantity=quantity)
        if success:
            target_job.status = "PRINTED"
            target_job.quantity = quantity
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
            return web.json_response({"status": "success", "quote": box.quote_number, "box": box.current_box})
        else:
            return web.json_response({"status": "error", "message": "Outer box print failed"}, status=500)

    async def handle_print_all_outer_boxes(self, request):
        count = 0
        while self.queue_mgr.pending_outer_boxes:
            box = self.queue_mgr.pending_outer_boxes.pop(0)
            if self.printer.dispatch_outer_box_label(box):
                count += 1
        return web.json_response({"status": "success", "printed_boxes": count, "total_labels": count * 2})
