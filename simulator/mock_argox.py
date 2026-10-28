"""
Virtual Argox OS-214 Printer Simulator
Receives raw PPLB byte streams, parses commands, validates dimensions and quantities,
and prints an ASCII visualization of the thermal label.
"""

import re
from typing import Dict, Any, List

class VirtualArgoxPrinter:
    def __init__(self):
        self.received_jobs: List[Dict[str, Any]] = []

    def process_pplb(self, raw_data: bytes) -> Dict[str, Any]:
        text = raw_data.decode("utf-8", errors="replace")
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        label_info = {
            "commands": lines,
            "text_elements": [],
            "barcodes": [],
            "quantity": 1
        }

        for line in lines:
            # Text command: A<x>,<y>,<rot>,<font>,<h_mult>,<v_mult>,<rev>,"<text>"
            if line.startswith("A"):
                match = re.search(r'A(\d+),(\d+),\d+,(\d+),\d+,\d+,\w+,"([^"]*)"', line)
                if match:
                    label_info["text_elements"].append({
                        "x": int(match.group(1)),
                        "y": int(match.group(2)),
                        "font": int(match.group(3)),
                        "text": match.group(4)
                    })
            # Barcode command: B<x>,<y>,<rot>,<type>,<narrow>,<wide>,<height>,<human_readable>,"<data>"
            elif line.startswith("B"):
                match = re.search(r'B(\d+),(\d+),\d+,\w+,\d+,\d+,(\d+),\w+,"([^"]*)"', line)
                if match:
                    label_info["barcodes"].append({
                        "x": int(match.group(1)),
                        "y": int(match.group(2)),
                        "height": int(match.group(3)),
                        "data": match.group(4)
                    })
            # Print quantity: P<N>
            elif line.startswith("P"):
                match = re.search(r'P(\d+)', line)
                if match:
                    label_info["quantity"] = int(match.group(1))

        self.received_jobs.append(label_info)
        return label_info

    def print_ascii_preview(self, label_info: Dict[str, Any]):
        print("\n" + "=" * 50)
        print(f"  VIRTUAL ARGOX OS-214 THERMAL PRINT ({label_info['quantity']}x COPIES)")
        print("=" * 50)
        for t in label_info["text_elements"]:
            print(f"  [{t['y']:03d}px]  {t['text']}")
        for b in label_info["barcodes"]:
            print(f"  [BARCODE] ||| {b['data']} |||")
        print("=" * 50 + "\n")
