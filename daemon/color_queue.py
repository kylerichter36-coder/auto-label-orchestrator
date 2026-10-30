import logging
from typing import Dict, List, Optional, Any
from pydantic import BaseModel
from daemon.classifier import CartridgeItem, OuterBoxSheet

logger = logging.getLogger("ColorQueue")

class QueuedPrintJob(BaseModel):
    id: str
    quote_number: str
    item_name: str
    description: str
    quantity: int
    color_group: str
    matched_label_file: str
    handwritten_note: Optional[str] = None
    status: str = "PENDING" # PENDING, PRINTED, CANCELLED

class ColorQueueManager:
    """
    Intelligently groups pending label jobs by color to minimize physical roll swaps.
    """
    def __init__(self):
        # Color groups: BLACK, CYAN, MAGENTA, YELLOW, OTHER, OUTER_BOX
        self.queues: Dict[str, List[QueuedPrintJob]] = {
            "BLACK": [],
            "CYAN": [],
            "MAGENTA": [],
            "YELLOW": [],
            "OTHER": []
        }
        self.pending_outer_boxes: List[OuterBoxSheet] = []
        self.job_counter: int = 0
        self.persistence_mgr: Optional[object] = None

    def set_persistence_manager(self, persistence_mgr: Optional[object]):
        """Attaches a persistence manager for automatic queue state persistence."""
        self.persistence_mgr = persistence_mgr

    def _persist(self):
        """Triggers non-volatile state persistence if a persistence manager is attached."""
        if self.persistence_mgr is not None and hasattr(self.persistence_mgr, "save_active_queues"):
            try:
                self.persistence_mgr.save_active_queues(self)
            except Exception as e:
                logger.warning(f"Error persisting queue state: {e}")

    def add_cartridge_item(self, quote_num: str, item: CartridgeItem):
        self.job_counter += 1
        job_id = f"JOB-{self.job_counter:04d}"
        
        color = item.color_group.upper()
        if color not in self.queues:
            color = "OTHER"

        filename = item.matched_label_file or f"{item.item_name}.prn"

        job = QueuedPrintJob(
            id=job_id,
            quote_number=quote_num,
            item_name=item.item_name,
            description=item.description or "",
            quantity=item.quantity,
            color_group=color,
            matched_label_file=filename,
            handwritten_note=item.handwritten_note,
            status="PENDING"
        )
        self.queues[color].append(job)
        logger.info(f"Queued {job.quantity}x {job.item_name} in {color} roll queue (Job ID: {job_id})")
        self._persist()

    def add_outer_box(self, box_data: OuterBoxSheet):
        self.pending_outer_boxes.append(box_data)
        logger.info(f"Queued Outer Box: Quote {box_data.quote_number} (Box {box_data.current_box}/{box_data.total_boxes})")
        self._persist()

    def get_summary(self) -> Dict[str, int]:
        """Returns total label counts per color group."""
        summary = {color: sum(j.quantity for j in jobs if j.status == "PENDING") for color, jobs in self.queues.items()}
        summary["OUTER_BOX"] = len([b for b in self.pending_outer_boxes])
        return summary

    def get_jobs_for_color(self, color_group: str) -> List[QueuedPrintJob]:
        color = color_group.upper()
        return [j for j in self.queues.get(color, []) if j.status == "PENDING"]

    def mark_color_printed(self, color_group: str):
        color = color_group.upper()
        if color in self.queues:
            for j in self.queues[color]:
                if j.status == "PENDING":
                    j.status = "PRINTED"
            logger.info(f"Marked all {color} jobs as PRINTED.")
            self._persist()

    def clear_all(self):
        for color in self.queues:
            self.queues[color].clear()
        self.pending_outer_boxes.clear()
        self._persist()

    def to_dict(self) -> Dict[str, Any]:
        """Serializes current queue state to dictionary."""
        return {
            "queues": {
                color: [j.model_dump() for j in jobs]
                for color, jobs in self.queues.items()
            },
            "pending_outer_boxes": [
                b.model_dump() for b in self.pending_outer_boxes
            ],
            "job_counter": self.job_counter
        }

    def load_from_dict(self, data: Dict[str, Any]):
        """Restores queue state from dictionary."""
        if not isinstance(data, dict):
            return
        raw_queues = data.get("queues", {})
        for color, jobs in raw_queues.items():
            color_upper = color.upper()
            if color_upper not in self.queues:
                self.queues[color_upper] = []
            else:
                self.queues[color_upper].clear()
            for j in jobs:
                if isinstance(j, dict):
                    self.queues[color_upper].append(QueuedPrintJob.model_validate(j))
                else:
                    self.queues[color_upper].append(j)

        raw_boxes = data.get("pending_outer_boxes", data.get("outer_boxes", []))
        self.pending_outer_boxes.clear()
        for b in raw_boxes:
            if isinstance(b, dict):
                self.pending_outer_boxes.append(OuterBoxSheet.model_validate(b))
            else:
                self.pending_outer_boxes.append(b)

        if "job_counter" in data:
            self.job_counter = int(data["job_counter"])
