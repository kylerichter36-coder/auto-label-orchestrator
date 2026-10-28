import os
import re
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from daemon.config import config
from daemon.classifier import OuterBoxSheet, BoxItemContent

logger = logging.getLogger("PrinterEngine")

class ArgoxPrinterEngine:
    def __init__(self, device_path: str = "/dev/usb/lp0", label_dir: Optional[Path] = None, simulation_mode: bool = False):
        self.device_path = device_path
        self.label_dir = label_dir or config.LABEL_DIR
        self.simulation_mode = simulation_mode
        self.simulated_print_history: List[Dict[str, Any]] = []

    def dispatch_cartridge_label(self, label_filename: str, quantity: int = 1) -> bool:
        """
        Locates pre-made .prn file, injects dynamic P<quantity> batch command,
        and streams to Argox OS-214 via raw USB.
        """
        file_path = self._resolve_label_file(label_filename)
        if not file_path or not file_path.exists():
            logger.error(f"Cannot print: Pre-made label file '{label_filename}' not found in {self.label_dir}")
            return False

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw_pplb = f.read()

        # Dynamic quantity injection into PPLB stream
        modified_pplb = self._inject_pplb_quantity(raw_pplb, quantity)
        
        job_info = {
            "type": "CARTRIDGE_LABEL",
            "file": file_path.name,
            "quantity": quantity,
            "payload_preview": modified_pplb[:150] + "..." if len(modified_pplb) > 150 else modified_pplb
        }

        return self._send_raw_to_printer(modified_pplb.encode("utf-8"), job_info)

    def dispatch_outer_box_label(self, box_data: OuterBoxSheet) -> bool:
        """
        Dynamically constructs an industrial PPLB outer box label with
        Header (Box X of Y), itemized Toner/Drum units, Quote #, Weight,
        and P2 command (2 copies for front + back of carton).
        """
        pplb_commands = []
        
        # PPLB Standard Initialization
        pplb_commands.append("N")          # Clear image buffer
        pplb_commands.append("q800")       # Printable width (dots) for 4x6" 203 DPI label
        pplb_commands.append("Q1200,24")   # Label length 1200 dots, gap 24 dots
        pplb_commands.append("D8")         # Print darkness (8=high contrast for thermal transfer)
        pplb_commands.append("S2")         # Print speed (2 inches/sec)

        # 1. Header Banner: BOX X OF Y
        box_banner = f"BOX {box_data.current_box} OF {box_data.total_boxes}".upper()
        pplb_commands.append(f'A50,30,0,4,1,1,N,"{box_banner}"')
        
        # Horizontal Divider Line below header
        pplb_commands.append("LO50,80,700,4")

        # 2. Dynamic Item Lines (Toner vs Drum Units)
        start_y = 110
        line_height = 45
        for idx, item in enumerate(box_data.box_contents):
            curr_y = start_y + (idx * line_height)
            if curr_y > 640:
                # Truncate if overflowing label boundary
                pplb_commands.append(f'A50,{curr_y},0,3,1,1,N,"... (additional items truncated)"')
                break

            line_text = f"{item.quantity}x {item.unit_type} ({item.model})"
            # Escape quotes
            safe_text = line_text.replace('"', "'")
            pplb_commands.append(f'A50,{curr_y},0,3,1,1,N,"{safe_text}"')

        # Horizontal Divider Line above footer
        pplb_commands.append("LO50,670,700,4")

        # 3. Footer: Quote Number (Bottom-Left) and Weight (Bottom-Right)
        quote_text = f"QUOTE: {box_data.quote_number}"
        weight_text = f"WEIGHT: {box_data.weight_kg.upper()}"
        if not weight_text.endswith("KG"):
            weight_text += " KG"

        pplb_commands.append(f'A50,700,0,3,1,1,N,"{quote_text}"')
        pplb_commands.append(f'A450,700,0,3,1,1,N,"{weight_text}"')

        # 4. Quantity Command: P2 (Automatic 2 copies for front and back of carton)
        copies = box_data.copies_per_box or 2
        pplb_commands.append(f"P{copies}")
        pplb_commands.append("") # Trailing newline

        full_pplb = "\n".join(pplb_commands)
        job_info = {
            "type": "OUTER_BOX_LABEL",
            "quote": box_data.quote_number,
            "box": f"{box_data.current_box}/{box_data.total_boxes}",
            "copies": copies,
            "payload_preview": full_pplb
        }

        return self._send_raw_to_printer(full_pplb.encode("utf-8"), job_info)

    def _inject_pplb_quantity(self, raw_pplb: str, quantity: int) -> str:
        """
        Replaces existing trailing P<N> command with P<quantity>,
        or appends P<quantity> if not found.
        """
        # PPLB print command pattern: P<count> or P<count>,<group> at end of line
        pattern = r'\bP\d+(\s*,\s*\d+)?\b'
        if re.search(pattern, raw_pplb):
            return re.sub(pattern, f"P{quantity}", raw_pplb)
        else:
            return raw_pplb.strip() + f"\nP{quantity}\n"

    def _resolve_label_file(self, filename: str) -> Optional[Path]:
        """
        Normalizes and resolves label file with flexible case and alias matching.
        """
        if not self.label_dir.exists():
            return None

        # Clean target
        target = filename.strip()
        if not target.lower().endswith(".prn"):
            target += ".prn"

        clean_target = re.sub(r'[\s\-]+', '_', target.lower())

        # Exact match
        direct = self.label_dir / target
        if direct.exists():
            return direct

        # Normalized search
        for f in self.label_dir.glob("*.prn"):
            norm_name = re.sub(r'[\s\-]+', '_', f.name.lower())
            if norm_name == clean_target:
                return f

        # Partial matching (e.g. 'Dell_283x' matches 'Dell_HCF_283x.prn')
        target_stem = clean_target.replace(".prn", "")
        for f in self.label_dir.glob("*.prn"):
            norm_stem = re.sub(r'[\s\-]+', '_', f.stem.lower())
            if target_stem in norm_stem or norm_stem in target_stem:
                logger.info(f"Fuzzy-matched label '{filename}' -> '{f.name}'")
                return f

        return None

    def _send_raw_to_printer(self, data: bytes, job_info: Dict[str, Any]) -> bool:
        """
        Transmits raw PPLB binary stream to USB device node or stores in simulation history.
        """
        if self.simulation_mode or not os.path.exists(self.device_path):
            logger.info(f"[SIMULATOR] Dispatched {job_info['type']} ({len(data)} bytes) to virtual Argox OS-214.")
            self.simulated_print_history.append({
                "job_info": job_info,
                "data": data.decode("utf-8", errors="replace")
            })
            return True

        try:
            with open(self.device_path, "wb") as lp:
                lp.write(data)
                lp.flush()
            logger.info(f"Dispatched {job_info['type']} directly to {self.device_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to write to printer {self.device_path}: {e}")
            return False
