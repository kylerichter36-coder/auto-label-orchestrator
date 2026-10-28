import logging
from typing import Dict, List, Optional
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

    def add_outer_box(self, box_data: OuterBoxSheet):
        self.pending_outer_boxes.append(box_data)
        logger.info(f"Queued Outer Box: Quote {box_data.quote_number} (Box {box_data.current_box}/{box_data.total_boxes})")

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

    def clear_all(self):
        for color in self.queues:
            self.queues[color].clear()
        self.pending_outer_boxes.clear()
