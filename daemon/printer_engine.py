import os
import re
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from daemon.config import config
from daemon.classifier import OuterBoxSheet, BoxItemContent
from daemon.bartender_converter import (
    convert_bartender_to_pplb,
    convert_bartender_to_ppla,
    render_intermediate_ast,
    inject_quantity,
    LabelAST,
    sanitize_text
)

logger = logging.getLogger("PrinterEngine")

class ArgoxPrinterEngine:
    def __init__(
        self,
        device_path: str = "/dev/usb/lp0",
        label_dir: Optional[Path] = None,
        simulation_mode: bool = False,
        emulation: Optional[str] = None
    ):
        self.device_path = device_path
        self.label_dir = label_dir or config.LABEL_DIR
        self.simulation_mode = simulation_mode
        self.emulation = (emulation or os.getenv("PRINTER_EMULATION", "PPLB")).upper()
        self.simulated_print_history: List[Dict[str, Any]] = []

    def dispatch_cartridge_label(
        self,
        label_filename: str,
        quantity: int = 1,
        variables: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Locates pre-made .prd or .prn template, applies placeholder substitution
        and dynamic quantity injection via bartender_converter, and streams to printer.
        """
        file_path = self._resolve_label_file(label_filename)
        if not file_path or not file_path.exists():
            logger.error(f"Cannot print: Pre-made label file '{label_filename}' not found in {self.label_dir}")
            return False

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            template_content = f.read()

        var_dict = dict(variables or {})
        var_dict.setdefault("QUANTITY", quantity)

        if self.emulation == "PPLA":
            payload_bytes = convert_bartender_to_ppla(template_content, var_dict)
            payload_bytes = inject_quantity(payload_bytes, quantity, emulation="PPLA")
        else:
            payload_bytes = convert_bartender_to_pplb(template_content, var_dict)
            payload_bytes = inject_quantity(payload_bytes, quantity, emulation="PPLB")

        payload_str = payload_bytes.decode("utf-8", errors="replace")
        job_info = {
            "type": "CARTRIDGE_LABEL",
            "file": file_path.name,
            "quantity": quantity,
            "emulation": self.emulation,
            "payload_preview": payload_str[:150] + "..." if len(payload_str) > 150 else payload_str
        }

        return self._send_raw_to_printer(payload_bytes, job_info)

    def dispatch_outer_box_label(self, box_data: OuterBoxSheet) -> bool:
        """
        Dynamically constructs an industrial outer box label using Intermediate Label AST,
        with Header (Box X of Y), itemized Toner/Drum units, Quote #, Weight,
        and P2 command (2 copies for front + back of carton).
        """
        copies = box_data.copies_per_box or 2
        ast = LabelAST(
            width_dots=800,
            height_dots=1200,
            density=8,
            speed=2,
            gap_dots=24,
            copies=copies
        )

        # 1. Header Banner: BOX X OF Y
        box_banner = f"BOX {box_data.current_box} OF {box_data.total_boxes}".upper()
        ast.add_text(50, 30, box_banner, font=4, h_mult=1, v_mult=1)

        # Horizontal Divider Line below header
        ast.add_line(50, 80, 700, 4)

        # 2. Dynamic Item Lines (Toner vs Drum Units)
        start_y = 110
        line_height = 45
        for idx, item in enumerate(box_data.box_contents):
            curr_y = start_y + (idx * line_height)
            if curr_y > 640:
                # Truncate if overflowing label boundary
                ast.add_text(50, curr_y, "... (additional items truncated)", font=3, h_mult=1, v_mult=1)
                break

            line_text = f"{item.quantity}x {item.unit_type} ({item.model})"
            ast.add_text(50, curr_y, line_text, font=3, h_mult=1, v_mult=1)

        # Horizontal Divider Line above footer
        ast.add_line(50, 670, 700, 4)

        # 3. Footer: Quote Number (Bottom-Left) and Weight (Bottom-Right)
        quote_text = f"QUOTE: {box_data.quote_number}"
        weight_raw = str(box_data.weight_kg or "0.0").upper()
        weight_text = f"WEIGHT: {weight_raw}"
        if not weight_text.endswith("KG"):
            weight_text += " KG"

        ast.add_text(50, 700, quote_text, font=3, h_mult=1, v_mult=1)
        ast.add_text(450, 700, weight_text, font=3, h_mult=1, v_mult=1)

        payload_bytes = render_intermediate_ast(ast, emulation=self.emulation)
        full_payload_str = payload_bytes.decode("utf-8", errors="replace")

        job_info = {
            "type": "OUTER_BOX_LABEL",
            "quote": box_data.quote_number,
            "box": f"{box_data.current_box}/{box_data.total_boxes}",
            "copies": copies,
            "emulation": self.emulation,
            "payload_preview": full_payload_str
        }

        return self._send_raw_to_printer(payload_bytes, job_info)

    def _inject_pplb_quantity(self, raw_pplb: str, quantity: int) -> str:
        """
        Replaces existing trailing P<N> command with P<quantity>,
        or appends P<quantity> if not found.
        """
        return inject_quantity(raw_pplb, quantity, emulation="PPLB").decode("utf-8", errors="replace")

    def _resolve_label_file(self, filename: str) -> Optional[Path]:
        """
        Normalizes and resolves label file with flexible case and alias matching.
        Searches for both .prd and .prn formats, returning the actual on-disk Path.
        """
        target = (filename or "").strip()
        if not target:
            return None

        lower_target = target.lower()

        # Collect candidate files (.prd, .prn, .btw) from disk and SD card storage
        candidates = []
        search_dirs = []
        if self.label_dir and self.label_dir.exists():
            search_dirs.append(self.label_dir)

        sample_dir = config.BASE_DIR / "templates" / "sample_labels"
        if sample_dir.exists() and sample_dir not in search_dirs:
            search_dirs.append(sample_dir)

        sd_mount = getattr(config, "SDCARD_MOUNT", Path("/mnt/sdcard_backup"))
        for sub in ["labels", "templates", "sample_labels", "btw_labels"]:
            sd_sub = sd_mount / sub
            if sd_sub.exists() and sd_sub.is_dir() and sd_sub not in search_dirs:
                search_dirs.append(sd_sub)

        template_dir = getattr(config, "TEMPLATE_DIR", None)
        if template_dir and template_dir.exists() and template_dir.is_dir() and template_dir not in search_dirs:
            search_dirs.append(template_dir)

        for d in search_dirs:
            if d.exists() and d.is_dir():
                candidates.extend(list(d.glob("*.prd")))
                candidates.extend(list(d.glob("*.prn")))
                candidates.extend(list(d.glob("*.btw")))
                candidates.extend(list(d.glob("*.xml")))

        # 1. Exact case-sensitive match (name or stem)
        for f in candidates:
            if f.name == target or f.stem == target:
                return f

        # 2. Case-insensitive exact match (name or stem)
        for f in candidates:
            if f.name.lower() == lower_target or f.stem.lower() == lower_target:
                return f

        # 3. Normalized match (replace spaces and hyphens with underscores)
        clean_target = re.sub(r'[\s\-]+', '_', lower_target)
        clean_target_stem = re.sub(r'\.(prd|prn)$', '', clean_target)
        if not clean_target_stem:
            return None

        for f in candidates:
            norm_name = re.sub(r'[\s\-]+', '_', f.name.lower())
            norm_stem = re.sub(r'[\s\-]+', '_', f.stem.lower())
            if norm_name == clean_target or norm_stem == clean_target_stem:
                return f

        # 4. Partial fuzzy and token-based matching (e.g. 'Dell 283x' matches 'Dell_HCF_283x.prn')
        target_tokens = [tok for tok in clean_target_stem.split('_') if tok]
        for f in candidates:
            norm_stem = re.sub(r'[\s\-]+', '_', f.stem.lower())
            if clean_target_stem and (clean_target_stem in norm_stem or norm_stem in clean_target_stem):
                logger.info(f"Fuzzy-matched label '{filename}' -> '{f.name}'")
                return f
            if target_tokens and all(tok in norm_stem for tok in target_tokens):
                logger.info(f"Token-matched label '{filename}' -> '{f.name}'")
                return f

        return None

    def get_cartridge_preview(
        self,
        item_name: str,
        matched_file: str = "",
        quantity: int = 1,
        quote_number: str = "",
        description: str = "",
        handwritten_note: str = ""
    ) -> Dict[str, Any]:
        """
        Compiles and renders completed cartridge label payload and structured visual preview data.
        """
        file_path = self._resolve_label_file(matched_file) or self._resolve_label_file(item_name)
        template_name = file_path.name if file_path else "CARTRIDGE_TEMPLATE_PARAMETRIC.prd"

        if file_path and file_path.exists():
            raw_content = file_path.read_text(encoding="utf-8", errors="ignore")
        else:
            parametric = self.label_dir / "CARTRIDGE_TEMPLATE_PARAMETRIC.prd"
            raw_content = parametric.read_text(encoding="utf-8", errors="ignore") if parametric.exists() else ""

        clean_sku = (item_name or "").upper().strip()
        barcode_val = clean_sku.replace(" ", "_") if not file_path else file_path.stem.replace(" ", "_")

        var_dict = {
            "ITEM_NAME": item_name or clean_sku,
            "SKU": clean_sku,
            "QUOTE": quote_number,
            "QUOTE_NUMBER": quote_number,
            "DESC": description or item_name,
            "NOTE": handwritten_note or "",
            "BARCODE": barcode_val,
            "QUANTITY": quantity
        }

        pplb_bytes = convert_bartender_to_pplb(raw_content, var_dict)
        pplb_bytes = inject_quantity(pplb_bytes, quantity, emulation=self.emulation)
        pplb_str = pplb_bytes.decode("utf-8", errors="replace")

        # Parse rendered text lines from PPLB for high-fidelity UI preview
        lines = []
        for line in pplb_str.splitlines():
            if line.startswith('A50,30,0,4,1,1,N,"'):
                m = re.search(r'"([^"]*)"', line)
                if m: lines.append(m.group(1))
            elif line.startswith('A50,85,0,3,1,1,N,"'):
                m = re.search(r'"([^"]*)"', line)
                if m: lines.append(m.group(1))
            elif line.startswith('B50,140'):
                m = re.search(r'"([^"]*)"', line)
                if m: barcode_val = m.group(1)

        title = lines[0] if lines else item_name
        subtitle = lines[1] if len(lines) > 1 else (description or "THERMAL TRANSFER CARTRIDGE LABEL")

        return {
            "type": "CARTRIDGE",
            "item_name": item_name,
            "chosen_template": template_name,
            "quote_number": quote_number,
            "quantity": quantity,
            "title": title,
            "subtitle": subtitle,
            "note": handwritten_note,
            "barcode": barcode_val,
            "pplb_payload": pplb_str,
            "dimensions": "100mm x 50mm (800 x 400 dots)"
        }

    def get_outer_box_preview(self, box_data: OuterBoxSheet) -> Dict[str, Any]:
        """
        Compiles and renders completed outer box (serialized master box) label payload
        and structured visual preview data.
        """
        copies = box_data.copies_per_box or 2
        ast = LabelAST(
            width_dots=800,
            height_dots=1200,
            density=8,
            speed=2,
            gap_dots=24,
            copies=copies
        )

        box_banner = f"BOX {box_data.current_box} OF {box_data.total_boxes}".upper()
        ast.add_text(50, 30, box_banner, font=4, h_mult=1, v_mult=1)
        ast.add_line(50, 80, 700, 4)

        start_y = 110
        line_height = 45
        manifest_items = []
        for idx, item in enumerate(box_data.box_contents):
            curr_y = start_y + (idx * line_height)
            line_text = f"{item.quantity}x {item.unit_type} ({item.model})"
            manifest_items.append(line_text)
            if curr_y > 640:
                ast.add_text(50, curr_y, "... (additional items truncated)", font=3, h_mult=1, v_mult=1)
                break
            ast.add_text(50, curr_y, line_text, font=3, h_mult=1, v_mult=1)

        ast.add_line(50, 670, 700, 4)
        quote_text = f"QUOTE: {box_data.quote_number}"
        weight_raw = str(box_data.weight_kg or "0.0").upper()
        weight_text = f"WEIGHT: {weight_raw}"
        if not weight_text.endswith("KG"):
            weight_text += " KG"

        ast.add_text(50, 700, quote_text, font=3, h_mult=1, v_mult=1)
        ast.add_text(450, 700, weight_text, font=3, h_mult=1, v_mult=1)

        payload_bytes = render_intermediate_ast(ast, emulation=self.emulation)
        payload_str = payload_bytes.decode("utf-8", errors="replace")

        return {
            "type": "OUTER_BOX",
            "chosen_template": "OUTER_BOX_TEMPLATE.prd (Serialized Master Box Template)",
            "box_banner": box_banner,
            "quote_number": box_data.quote_number,
            "weight_text": weight_text,
            "manifest": manifest_items,
            "copies": copies,
            "barcode": f"{box_data.quote_number}-B{box_data.current_box}",
            "pplb_payload": payload_str,
            "dimensions": "100mm x 150mm (800 x 1200 dots, 4x6 inch)"
        }

    def is_connected(self) -> bool:
        """
        Returns True only if the physical printer device node exists.
        """
        return os.path.exists(self.device_path)

    def send_test_label(self) -> Dict[str, Any]:
        """
        Transmits calibration test pattern directly to Argox printer.
        Returns honest status and diagnostic message.
        """
        test_pplb = (
            "N\n"
            "q800\n"
            "Q400,24\n"
            "D8\n"
            "S2\n"
            'A50,40,0,4,1,1,N,"ARGOX OS-214 TEST OK"\n'
            'A50,110,0,3,1,1,N,"ORANGE PI HARDWARE CALIBRATION"\n'
            'B50,170,0,1,2,5,60,B,"SELF-TEST-100"\n'
            "P1\n"
        )
        job_info = {"type": "CALIBRATION_TEST", "copies": 1}

        if self.simulation_mode:
            self.simulated_print_history.append({
                "job_info": job_info,
                "data": test_pplb
            })
            return {
                "status": "simulated",
                "connected": False,
                "device": self.device_path,
                "message": "Simulation Mode active: Calibration test label recorded in memory (hardware write bypassed)."
            }

        if not os.path.exists(self.device_path):
            return {
                "status": "offline",
                "connected": False,
                "device": self.device_path,
                "message": f"Printer offline: '{self.device_path}' device node does not exist. Connect Argox OS-214 via USB and power on."
            }

        try:
            with open(self.device_path, "wb") as lp:
                lp.write(test_pplb.encode("utf-8"))
                lp.flush()
            return {
                "status": "success",
                "connected": True,
                "device": self.device_path,
                "message": f"Calibration test label (800x400 dots) sent directly to {self.device_path}."
            }
        except Exception as e:
            return {
                "status": "error",
                "connected": True,
                "device": self.device_path,
                "message": f"Hardware write failed to {self.device_path}: {e}"
            }

    def _send_raw_to_printer(self, data: bytes, job_info: Dict[str, Any]) -> bool:
        """
        Transmits raw PPLB binary stream to USB device node.
        Does NOT pretend to succeed if device does not exist and simulation mode is False.
        """
        if self.simulation_mode:
            logger.info(f"[SIMULATOR] Dispatched {job_info['type']} ({len(data)} bytes) to virtual Argox OS-214.")
            self.simulated_print_history.append({
                "job_info": job_info,
                "data": data.decode("utf-8", errors="replace")
            })
            return True

        if not os.path.exists(self.device_path):
            logger.error(f"Printer offline: '{self.device_path}' device node does not exist. Cannot dispatch {job_info.get('type')}.")
            return False

        try:
            with open(self.device_path, "wb") as lp:
                lp.write(data)
                lp.flush()
            logger.info(f"Dispatched {job_info['type']} directly to {self.device_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to write to printer {self.device_path}: {e}")
            return False
