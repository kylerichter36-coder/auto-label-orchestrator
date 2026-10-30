"""
BarTender Label Ingestion & Conversion Engine for Argox OS-214
Supports 203.2 DPI (8 dots/mm) coordinate transformation, Intermediate Label AST,
Native PPLB (Eltron EPL2) & PPLA (Datamax DPL) emitters, dynamic quantity injection,
BTXML, CSV integration, and string sanitization.
"""

import os
import re
import csv
import io
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Union, Tuple
import xml.etree.ElementTree as ET

logger = logging.getLogger("BarTenderConverter")

# ---------------------------------------------------------------------------
# Hardware & Coordinate Constants (Argox OS-214)
# ---------------------------------------------------------------------------
DPI_NATIVE = 203.2
DOTS_PER_MM = 8.0
DOTS_PER_INCH = 203.2

# Standard label bounds in dots
CARTRIDGE_WIDTH_DOTS = 800     # 100 mm @ 8 dots/mm
CARTRIDGE_HEIGHT_DOTS = 400    # 50 mm @ 8 dots/mm
OUTER_BOX_WIDTH_DOTS = 800     # 100 mm @ 8 dots/mm (4" wide)
OUTER_BOX_HEIGHT_DOTS = 1200   # 150 mm @ 8 dots/mm (6" long)
MAX_PRINTHEAD_WIDTH_DOTS = 832 # 4.1" max physical width (Argox OS-214)
DEFAULT_LABEL_GAP_DOTS = 24    # 3 mm transmissive gap

# Font height approximations in dots (Argox OS-214 resident bitmap fonts)
FONT_HEIGHT_MAP = {
    1: 12,  # 8x12 dots
    2: 16,  # 10x16 dots
    3: 20,  # 12x20 dots
    4: 24,  # 14x24 dots
    5: 48   # 32x48 dots
}


# ---------------------------------------------------------------------------
# Coordinate Engine
# ---------------------------------------------------------------------------
class CoordinateEngine:
    """
    Translates physical measurement units to native 203.2 DPI printhead dots
    and validates printable boundary constraints.
    """

    @staticmethod
    def mm_to_dots(mm: float) -> int:
        return int(round(mm * DOTS_PER_MM))

    @staticmethod
    def dots_to_mm(dots: int) -> float:
        return dots / DOTS_PER_MM

    @staticmethod
    def inches_to_dots(inches: float) -> int:
        return int(round(inches * DOTS_PER_INCH))

    @staticmethod
    def dots_to_inches(dots: int) -> float:
        return dots / DOTS_PER_INCH

    @staticmethod
    def scale_coordinate(value: float, unit: str = "dots") -> int:
        u = unit.strip().lower()
        if u in ("mm", "millimeter", "millimeters"):
            return CoordinateEngine.mm_to_dots(value)
        elif u in ("in", "inch", "inches"):
            return CoordinateEngine.inches_to_dots(value)
        return int(round(value))

    @staticmethod
    def validate_bounds(
        x: int,
        y: int,
        width: int = 0,
        height: int = 0,
        max_width: int = CARTRIDGE_WIDTH_DOTS,
        max_height: int = OUTER_BOX_HEIGHT_DOTS
    ) -> Tuple[bool, Optional[str]]:
        """
        Validates whether an element resides completely within printable boundaries.
        Returns (is_valid, error_message).
        """
        if x < 0 or y < 0:
            return False, f"Coordinate ({x}, {y}) cannot be negative"
        if (x + width) > max_width:
            return False, f"Horizontal span ({x} + {width} = {x + width}) exceeds max width {max_width}"
        if (y + height) > max_height:
            return False, f"Vertical span ({y} + {height} = {y + height}) exceeds max height {max_height}"
        return True, None


# ---------------------------------------------------------------------------
# String Sanitization
# ---------------------------------------------------------------------------
def sanitize_text(text: str) -> str:
    """
    Sanitizes string for printer protocol commands:
    - Replaces internal double quotes with single quotes to prevent command termination
    - Replaces backslashes with forward slashes
    - Strips unprintable ASCII control characters (0x00-0x1F, 0x7F) except space
    - Converts newlines to spaces
    """
    if text is None:
        return ""
    s = str(text)
    # Strip carriage returns and line feeds
    s = s.replace("\r", " ").replace("\n", " ")
    # Replace double quotes with single quotes
    s = s.replace('"', "'")
    # Replace backslashes
    s = s.replace("\\", "/")
    # Filter unprintable control characters
    s = "".join(ch for ch in s if ord(ch) >= 32 and ord(ch) != 127)
    return s.strip()


def sanitize_barcode_data(data: str) -> str:
    """
    Sanitizes barcode data string: removes double quotes, control chars, and whitespace.
    """
    if data is None:
        return ""
    s = str(data).strip()
    s = s.replace('"', "").replace("'", "").replace("\\", "")
    s = "".join(ch for ch in s if ord(ch) >= 32 and ord(ch) != 127)
    return s


# ---------------------------------------------------------------------------
# Intermediate Label AST (Abstract Syntax Tree)
# ---------------------------------------------------------------------------
@dataclass
class LabelElement:
    x: int = 0
    y: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TextElement(LabelElement):
    content: str = ""
    font: int = 3           # Bitmap font selector (1 to 5)
    rotation: int = 0       # 0: 0 deg, 1: 90 deg, 2: 180 deg, 3: 270 deg
    h_mult: int = 1         # Horizontal scale multiplier (1 to 8)
    v_mult: int = 1         # Vertical scale multiplier (1 to 9)
    reverse: bool = False   # True = White-on-black, False = Normal black-on-white

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = "text"
        return d


@dataclass
class Barcode1DElement(LabelElement):
    content: str = ""
    barcode_type: str = "1" # "1"=Code 128, "3"=Code 39, "E30"=EAN-13, "E80"=EAN-8, "9"=UPC-A
    narrow: int = 2         # Narrow bar width in dots (default 2 for 203 DPI)
    wide: int = 5           # Wide bar width in dots (default 5)
    height: int = 60        # Barcode vertical height in dots
    rotation: int = 0       # 0: 0 deg, 1: 90 deg, 2: 180 deg, 3: 270 deg
    hri: str = "B"          # Human-readable interpretation: "B"=below, "T"=above, "N"=none

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = "barcode_1d"
        return d


@dataclass
class Barcode2DElement(LabelElement):
    content: str = ""
    subtype: str = "QR"     # "QR" or "DATAMATRIX"
    model: int = 2          # QR Model (1 or 2)
    cell_size: int = 4      # Cell / module magnification size (1 to 9)
    ecc: str = "M"          # QR Error Correction: L, M, Q, H
    cols: int = 0           # DataMatrix columns (0 = auto)
    rows: int = 0           # DataMatrix rows (0 = auto)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = "barcode_2d"
        return d


@dataclass
class LineElement(LabelElement):
    width: int = 700        # Horizontal length in dots
    height: int = 4         # Line thickness in dots

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = "line"
        return d


@dataclass
class BoxElement(LabelElement):
    width: int = 700        # Outer width in dots
    height: int = 300       # Outer height in dots
    thickness: int = 2      # Border stroke thickness in dots

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = "box"
        return d


@dataclass
class LabelAST:
    width_dots: int = CARTRIDGE_WIDTH_DOTS
    height_dots: int = CARTRIDGE_HEIGHT_DOTS
    density: int = 8        # Thermal darkness (0-15)
    speed: int = 2          # Print speed in ips (1-3)
    gap_dots: int = DEFAULT_LABEL_GAP_DOTS
    copies: int = 1
    elements: List[LabelElement] = field(default_factory=list)

    def add_text(
        self,
        x: int,
        y: int,
        content: str,
        font: int = 3,
        rotation: int = 0,
        h_mult: int = 1,
        v_mult: int = 1,
        reverse: bool = False
    ) -> TextElement:
        elem = TextElement(
            x=x,
            y=y,
            content=sanitize_text(content),
            font=font,
            rotation=rotation,
            h_mult=h_mult,
            v_mult=v_mult,
            reverse=reverse
        )
        self.elements.append(elem)
        return elem

    def add_barcode_1d(
        self,
        x: int,
        y: int,
        content: str,
        barcode_type: str = "1",
        narrow: int = 2,
        wide: int = 5,
        height: int = 60,
        rotation: int = 0,
        hri: str = "B"
    ) -> Barcode1DElement:
        elem = Barcode1DElement(
            x=x,
            y=y,
            content=sanitize_barcode_data(content),
            barcode_type=barcode_type,
            narrow=narrow,
            wide=wide,
            height=height,
            rotation=rotation,
            hri=hri
        )
        self.elements.append(elem)
        return elem

    def add_barcode_2d(
        self,
        x: int,
        y: int,
        content: str,
        subtype: str = "QR",
        model: int = 2,
        cell_size: int = 4,
        ecc: str = "M",
        cols: int = 0,
        rows: int = 0
    ) -> Barcode2DElement:
        elem = Barcode2DElement(
            x=x,
            y=y,
            content=sanitize_text(content),
            subtype=subtype.upper(),
            model=model,
            cell_size=cell_size,
            ecc=ecc.upper(),
            cols=cols,
            rows=rows
        )
        self.elements.append(elem)
        return elem

    def add_line(self, x: int, y: int, width: int, height: int = 4) -> LineElement:
        elem = LineElement(x=x, y=y, width=width, height=height)
        self.elements.append(elem)
        return elem

    def add_box(self, x: int, y: int, width: int, height: int, thickness: int = 2) -> BoxElement:
        elem = BoxElement(x=x, y=y, width=width, height=height, thickness=thickness)
        self.elements.append(elem)
        return elem

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width_dots": self.width_dots,
            "height_dots": self.height_dots,
            "density": self.density,
            "speed": self.speed,
            "gap_dots": self.gap_dots,
            "copies": self.copies,
            "elements": [e.to_dict() for e in self.elements]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LabelAST":
        ast = cls(
            width_dots=int(data.get("width_dots", CARTRIDGE_WIDTH_DOTS)),
            height_dots=int(data.get("height_dots", CARTRIDGE_HEIGHT_DOTS)),
            density=int(data.get("density", 8)),
            speed=int(data.get("speed", 2)),
            gap_dots=int(data.get("gap_dots", DEFAULT_LABEL_GAP_DOTS)),
            copies=int(data.get("copies", 1))
        )
        raw_elements = data.get("elements", [])
        for elem_data in raw_elements:
            elem_type = elem_data.get("type", "").lower()
            x = int(elem_data.get("x", 0))
            y = int(elem_data.get("y", 0))
            if elem_type == "text":
                ast.add_text(
                    x=x,
                    y=y,
                    content=elem_data.get("content", ""),
                    font=int(elem_data.get("font", 3)),
                    rotation=int(elem_data.get("rotation", 0)),
                    h_mult=int(elem_data.get("h_mult", 1)),
                    v_mult=int(elem_data.get("v_mult", 1)),
                    reverse=bool(elem_data.get("reverse", False))
                )
            elif elem_type == "barcode_1d":
                ast.add_barcode_1d(
                    x=x,
                    y=y,
                    content=elem_data.get("content", ""),
                    barcode_type=str(elem_data.get("barcode_type", "1")),
                    narrow=int(elem_data.get("narrow", 2)),
                    wide=int(elem_data.get("wide", 5)),
                    height=int(elem_data.get("height", 60)),
                    rotation=int(elem_data.get("rotation", 0)),
                    hri=str(elem_data.get("hri", "B"))
                )
            elif elem_type == "barcode_2d":
                ast.add_barcode_2d(
                    x=x,
                    y=y,
                    content=elem_data.get("content", ""),
                    subtype=str(elem_data.get("subtype", "QR")),
                    model=int(elem_data.get("model", 2)),
                    cell_size=int(elem_data.get("cell_size", 4)),
                    ecc=str(elem_data.get("ecc", "M")),
                    cols=int(elem_data.get("cols", 0)),
                    rows=int(elem_data.get("rows", 0))
                )
            elif elem_type == "line":
                ast.add_line(
                    x=x,
                    y=y,
                    width=int(elem_data.get("width", 700)),
                    height=int(elem_data.get("height", 4))
                )
            elif elem_type == "box":
                ast.add_box(
                    x=x,
                    y=y,
                    width=int(elem_data.get("width", 700)),
                    height=int(elem_data.get("height", 300)),
                    thickness=int(elem_data.get("thickness", 2))
                )
        return ast

    def validate_bounds(self, strict: bool = False) -> List[str]:
        warnings = []
        for idx, elem in enumerate(self.elements):
            elem_w = getattr(elem, "width", 0)
            elem_h = getattr(elem, "height", 0)
            valid, msg = CoordinateEngine.validate_bounds(
                elem.x, elem.y, elem_w, elem_h, self.width_dots, self.height_dots
            )
            if not valid:
                err_str = f"Element #{idx} ({type(elem).__name__}): {msg}"
                if strict:
                    raise ValueError(err_str)
                warnings.append(err_str)
        return warnings


# ---------------------------------------------------------------------------
# Emitters: PPLB (Eltron EPL2) & PPLA (Datamax DPL)
# ---------------------------------------------------------------------------
class PPLBEmitter:
    """
    Emits native Eltron Page Language (PPLB / EPL2) commands for Argox OS-214.
    """

    @staticmethod
    def emit(ast: LabelAST) -> bytes:
        lines: List[str] = []
        # Initialization
        lines.append("N")
        lines.append(f"q{ast.width_dots}")
        lines.append(f"Q{ast.height_dots},{ast.gap_dots}")
        lines.append(f"D{ast.density}")
        lines.append(f"S{ast.speed}")

        # Render elements
        for elem in ast.elements:
            if isinstance(elem, TextElement):
                rev_char = "R" if elem.reverse else "N"
                safe_text = sanitize_text(elem.content)
                lines.append(
                    f'A{elem.x},{elem.y},{elem.rotation},{elem.font},{elem.h_mult},{elem.v_mult},{rev_char},"{safe_text}"'
                )
            elif isinstance(elem, Barcode1DElement):
                safe_barcode = sanitize_barcode_data(elem.content)
                lines.append(
                    f'B{elem.x},{elem.y},{elem.rotation},{elem.barcode_type},{elem.narrow},{elem.wide},{elem.height},{elem.hri},"{safe_barcode}"'
                )
            elif isinstance(elem, Barcode2DElement):
                safe_data = sanitize_text(elem.content)
                if elem.subtype.upper() == "QR":
                    lines.append(
                        f'b{elem.x},{elem.y},Q,s{elem.model},m{elem.cell_size},r{elem.ecc},"{safe_data}"'
                    )
                else:  # DataMatrix
                    lines.append(
                        f'b{elem.x},{elem.y},D,c{elem.cols},r{elem.rows},"{safe_data}"'
                    )
            elif isinstance(elem, LineElement):
                lines.append(f"LO{elem.x},{elem.y},{elem.width},{elem.height}")
            elif isinstance(elem, BoxElement):
                lines.append(f"X{elem.x},{elem.y},{elem.thickness},{elem.width},{elem.height}")

        # Execution copy command
        lines.append(f"P{ast.copies}")
        lines.append("") # Trailing newline

        return "\n".join(lines).encode("utf-8")


class PPLAEmitter:
    """
    Emits native Datamax DPL (PPLA) commands for Argox OS-214.
    Origin coordinate transformation: PPLA origin (0,0) is bottom-left,
    whereas PPLB origin (0,0) is top-left.
    $Y_{PPLA} = \text{LabelHeight} - Y_{PPLB} - \text{ElementHeight}$
    """

    STX = "\x02"
    CR = "\r"

    @staticmethod
    def emit(ast: LabelAST) -> bytes:
        stx = PPLAEmitter.STX
        cr = PPLAEmitter.CR
        lines: List[str] = []

        # Start format sequence
        lines.append(f"{stx}L")
        # Darkness and speed
        lines.append(f"D{ast.density}{ast.speed}")

        for elem in ast.elements:
            if isinstance(elem, TextElement):
                base_font_h = FONT_HEIGHT_MAP.get(elem.font, 20)
                font_h = base_font_h * elem.v_mult
                y_ppla = max(0, int(round(ast.height_dots - elem.y - font_h)))
                x_ppla = max(0, int(round(elem.x)))
                # Orientation: 1=0 deg, 2=90 deg, 3=180 deg, 4=270 deg
                r_code = elem.rotation + 1
                safe_text = sanitize_text(elem.content)
                # PPLA Text format: R t v h 000 yyyy xxxx [text]
                lines.append(
                    f"{r_code}{elem.font}{elem.v_mult}{elem.h_mult}000{y_ppla:04d}{x_ppla:04d}{safe_text}"
                )
            elif isinstance(elem, Barcode1DElement):
                elem_h = int(round(elem.height))
                y_ppla = max(0, int(round(ast.height_dots - elem.y - elem_h)))
                x_ppla = max(0, int(round(elem.x)))
                r_code = elem.rotation + 1
                safe_data = sanitize_barcode_data(elem.content)
                # Code 128: 1e<w><n><hhh><yyyy><xxxx><data>
                # Wide/narrow ratio: e.g. wide=4, narrow=2
                ratio = max(2, min(9, int(round(elem.wide))))
                narrow = max(1, min(9, int(round(elem.narrow))))
                b_type_code = "e"
                if elem.barcode_type == "3":
                    b_type_code = "a" # Code 39
                elif elem.barcode_type in ("E30", "E80"):
                    b_type_code = "F" # EAN-13
                lines.append(
                    f"{r_code}{b_type_code}{ratio}{narrow}{elem_h:03d}{y_ppla:04d}{x_ppla:04d}{safe_data}"
                )
            elif isinstance(elem, Barcode2DElement):
                # Estimated 2D barcode size
                b2d_size = elem.cell_size * 25
                y_ppla = max(0, int(round(ast.height_dots - elem.y - b2d_size)))
                x_ppla = max(0, int(round(elem.x)))
                safe_data = sanitize_text(elem.content)
                if elem.subtype.upper() == "QR":
                    cell_sz = int(round(elem.cell_size))
                    lines.append(
                        f"W1Q{elem.ecc}{cell_sz}{y_ppla:04d}{x_ppla:04d}{safe_data}"
                    )
                else:
                    rows = int(round(elem.rows))
                    cols = int(round(elem.cols))
                    lines.append(
                        f"W1D{rows:02d}{cols:02d}{y_ppla:04d}{x_ppla:04d}{safe_data}"
                    )
            elif isinstance(elem, (LineElement, BoxElement)):
                elem_h = int(round(elem.height))
                elem_w = int(round(elem.width))
                y_ppla = max(0, int(round(ast.height_dots - elem.y - elem_h)))
                x_ppla = max(0, int(round(elem.x)))
                lines.append(
                    f"1X11000{y_ppla:04d}{x_ppla:04d}b{elem_w:04d}h{elem_h:04d}"
                )

        # Quantity command and end format
        copies = int(round(ast.copies))
        lines.append(f"Q{copies:04d}")
        lines.append("E")

        out = cr.join(lines) + cr
        return out.encode("utf-8")


# ---------------------------------------------------------------------------
# Dynamic Quantity Injection
# ---------------------------------------------------------------------------
def inject_quantity(command_bytes: Union[bytes, str], quantity: int, emulation: str = "PPLB") -> bytes:
    """
    Cleanly replaces trailing copy command without corrupting text or barcode data.
    - PPLB: replaces standalone 'P<count>' with 'P<quantity>'
    - PPLA: replaces standalone 'Q<count>' with 'Q<quantity:04d>'
    """
    if isinstance(command_bytes, bytes):
        raw_text = command_bytes.decode("utf-8", errors="replace")
    else:
        raw_text = str(command_bytes)

    emu = emulation.upper().strip()

    if emu == "PPLA" or "\x02L" in raw_text or raw_text.startswith("L\r"):
        # PPLA syntax: Q<count:4> delimited by \r or \n
        delim = "\r" if "\r" in raw_text else "\n"
        lines = raw_text.split(delim)
        new_q = f"Q{int(round(quantity)):04d}"
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if re.match(r'^Q\d{1,5}$', stripped):
                new_lines.append(new_q)
                found = True
            elif stripped == "E" and not found:
                new_lines.append(new_q)
                new_lines.append(line)
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(new_q)
            new_lines.append("E")
        res = delim.join(new_lines)
        return res.encode("utf-8")
    else:
        # PPLB syntax: P<count> or P<count>,<groups>
        delim = "\r\n" if "\r\n" in raw_text else "\n"
        lines = raw_text.split(delim)
        new_p = f"P{quantity}"
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if re.match(r'^P\d+(\s*,\s*\d+)?$', stripped):
                new_lines.append(new_p)
                found = True
            else:
                new_lines.append(line)
        if not found:
            if new_lines and new_lines[-1] == "":
                new_lines.insert(-1, new_p)
            else:
                new_lines.append(new_p)
        res = delim.join(new_lines)
        if not res.endswith("\n"):
            res += "\n"
        return res.encode("utf-8")


# ---------------------------------------------------------------------------
# Ingestion Parsers
# ---------------------------------------------------------------------------
def substitute_placeholders(template_str: str, variables: Optional[Dict[str, Any]] = None) -> str:
    """
    Substitutes {VAR_NAME} placeholders with sanitized variable values.
    Supports standard enterprise aliases (QUOTE_ID <-> QUOTE_NUMBER,
    DESCRIPTION <-> ITEM_NAME, WEIGHT/GROSS_WEIGHT <-> WEIGHT_KG, QTY <-> QUANTITY).
    """
    if not variables:
        return template_str

    # Expand known enterprise aliases without overwriting explicitly provided values
    var_map = dict(variables)
    alias_pairs = [
        ("QUOTE_ID", "QUOTE_NUMBER"),
        ("DESCRIPTION", "ITEM_NAME"),
        ("GROSS_WEIGHT", "WEIGHT_KG"),
        ("WEIGHT", "WEIGHT_KG"),
        ("QTY", "QUANTITY"),
        ("BARCODE_SKU", "BARCODE"),
    ]
    for a, b in alias_pairs:
        if a in var_map and b not in var_map:
            var_map[b] = var_map[a]
        elif b in var_map and a not in var_map:
            var_map[a] = var_map[b]

    result = template_str
    for key, val in var_map.items():
        placeholder = f"{{{key}}}"
        if placeholder in result:
            safe_val = sanitize_text(str(val))
            result = result.replace(placeholder, safe_val)
    return result


def parse_bartender_template(
    content_or_path: str,
    variables: Optional[Dict[str, Any]] = None
) -> str:
    """
    Resolves a BarTender export file (.prd/.prn) or template string,
    substituting variable placeholders.
    """
    content = ""
    path_candidate = Path(content_or_path)
    if path_candidate.exists() and path_candidate.is_file():
        with open(path_candidate, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    else:
        content = content_or_path

    return substitute_placeholders(content, variables)


def parse_btxml(xml_content_or_path: str) -> Dict[str, Any]:
    """
    Parses BarTender XML Script (BTXML) files.
    Supports:
    1. <XMLScript><Print><RecordSet><Record><Field Name="...">value</Field>...
    2. <Label Width="..." Height="..."><Text .../><Barcode .../>...
    """
    content = ""
    path_candidate = Path(xml_content_or_path)
    if path_candidate.exists() and path_candidate.is_file():
        with open(path_candidate, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    else:
        content = xml_content_or_path

    root = ET.fromstring(content)
    records: List[Dict[str, str]] = []
    ast: Optional[LabelAST] = None
    format_name = ""

    # Check for <Format> tag
    format_elem = root.find(".//Format")
    if format_elem is not None and format_elem.text:
        format_name = format_elem.text.strip()

    # Check for <RecordSet> / <Record>
    for record_node in root.findall(".//Record"):
        rec: Dict[str, str] = {}
        for field_node in record_node.findall("Field"):
            name = field_node.get("Name") or field_node.get("name")
            if name:
                rec[name] = field_node.text.strip() if field_node.text else ""
        if rec:
            records.append(rec)

    # Check for direct <Label> element definition in XML
    label_node = root if root.tag.lower() == "label" else root.find(".//Label")
    if label_node is not None:
        width_str = label_node.get("Width", "800")
        height_str = label_node.get("Height", "400")
        w_dots = CARTRIDGE_WIDTH_DOTS
        h_dots = CARTRIDGE_HEIGHT_DOTS
        if width_str.endswith("mm"):
            w_dots = CoordinateEngine.mm_to_dots(float(width_str[:-2]))
        elif width_str.endswith("in"):
            w_dots = CoordinateEngine.inches_to_dots(float(width_str[:-2]))
        else:
            w_dots = int(width_str)

        if height_str.endswith("mm"):
            h_dots = CoordinateEngine.mm_to_dots(float(height_str[:-2]))
        elif height_str.endswith("in"):
            h_dots = CoordinateEngine.inches_to_dots(float(height_str[:-2]))
        else:
            h_dots = int(height_str)

        ast = LabelAST(width_dots=w_dots, height_dots=h_dots)

        for child in label_node:
            tag = child.tag.lower()
            x = int(child.get("X", 0))
            y = int(child.get("Y", 0))
            if tag == "text":
                ast.add_text(
                    x=x,
                    y=y,
                    content=child.get("Content", child.text or ""),
                    font=int(child.get("Font", 3)),
                    rotation=int(child.get("Rotation", 0)),
                    h_mult=int(child.get("HMult", 1)),
                    v_mult=int(child.get("VMult", 1))
                )
            elif tag == "barcode":
                b_type = child.get("Type", "Code128")
                b_code = "1"
                if "39" in b_type:
                    b_code = "3"
                elif "ean" in b_type.lower():
                    b_code = "E30"
                ast.add_barcode_1d(
                    x=x,
                    y=y,
                    content=child.get("Content", child.text or ""),
                    barcode_type=b_code,
                    height=int(child.get("Height", 60))
                )
            elif tag in ("qrcode", "datamatrix"):
                ast.add_barcode_2d(
                    x=x,
                    y=y,
                    content=child.get("Content", child.text or ""),
                    subtype="QR" if tag == "qrcode" else "DATAMATRIX"
                )
            elif tag == "line":
                ast.add_line(x=x, y=y, width=int(child.get("Width", 700)), height=int(child.get("Height", 4)))

    return {
        "format_name": format_name,
        "records": records,
        "ast": ast
    }


def parse_csv_integration(csv_content_or_path: str) -> List[Dict[str, str]]:
    """
    Parses CSV integration files into a list of row dictionaries.
    """
    content = ""
    path_candidate = Path(csv_content_or_path)
    if path_candidate.exists() and path_candidate.is_file():
        with open(path_candidate, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    else:
        content = csv_content_or_path

    reader = csv.DictReader(io.StringIO(content.strip()))
    return [row for row in reader]


def parse_pplb_to_ast(pplb_str: str) -> LabelAST:
    """
    Reverse-engineers a native PPLB print stream into an Intermediate Label AST.
    """
    lines = [l.strip() for l in pplb_str.split("\n") if l.strip()]
    ast = LabelAST()

    for line in lines:
        if line == "N":
            continue
        elif line.startswith("q"):
            m = re.match(r'^q\s*(\d+)', line)
            if m:
                ast.width_dots = int(m.group(1))
        elif line.startswith("Q"):
            m = re.match(r'^Q\s*(\d+)(?:\s*,\s*(\d+))?', line)
            if m:
                ast.height_dots = int(m.group(1))
                if m.group(2):
                    ast.gap_dots = int(m.group(2))
        elif line.startswith("D"):
            m = re.match(r'^D\s*(\d+)', line)
            if m:
                ast.density = int(m.group(1))
        elif line.startswith("S"):
            m = re.match(r'^S\s*(\d+)', line)
            if m:
                ast.speed = int(m.group(1))
        elif line.startswith(("A", "a")):
            m = re.match(r'^[Aa]\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([NRnr])\s*,\s*"?([^"]*)"?\s*$', line)
            if m:
                ast.add_text(
                    x=int(m.group(1)),
                    y=int(m.group(2)),
                    rotation=int(m.group(3)),
                    font=int(m.group(4)),
                    h_mult=int(m.group(5)),
                    v_mult=int(m.group(6)),
                    reverse=(m.group(7).upper() == "R"),
                    content=m.group(8)
                )
        elif line.startswith("B"):
            m = re.match(r'^B\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([A-Za-z0-9]+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([BTNbtn])\s*,\s*"?([^"]*)"?\s*$', line)
            if m:
                ast.add_barcode_1d(
                    x=int(m.group(1)),
                    y=int(m.group(2)),
                    rotation=int(m.group(3)),
                    barcode_type=m.group(4),
                    narrow=int(m.group(5)),
                    wide=int(m.group(6)),
                    height=int(m.group(7)),
                    hri=m.group(8).upper(),
                    content=m.group(9)
                )
        elif line.startswith("b"):
            m_qr = re.match(r'^b\s*(\d+)\s*,\s*(\d+)\s*,\s*[Qq]\s*,\s*s(\d+)\s*,\s*m(\d+)\s*,\s*r([LMQHlmqh])\s*,\s*"?([^"]*)"?\s*$', line)
            if m_qr:
                ast.add_barcode_2d(
                    x=int(m_qr.group(1)),
                    y=int(m_qr.group(2)),
                    subtype="QR",
                    model=int(m_qr.group(3)),
                    cell_size=int(m_qr.group(4)),
                    ecc=m_qr.group(5).upper(),
                    content=m_qr.group(6)
                )
            else:
                m_dm = re.match(r'^b\s*(\d+)\s*,\s*(\d+)\s*,\s*[Dd]\s*,\s*c(\d+)\s*,\s*r(\d+)\s*,\s*"?([^"]*)"?\s*$', line)
                if m_dm:
                    ast.add_barcode_2d(
                        x=int(m_dm.group(1)),
                        y=int(m_dm.group(2)),
                        subtype="DATAMATRIX",
                        cols=int(m_dm.group(3)),
                        rows=int(m_dm.group(4)),
                        content=m_dm.group(5)
                    )
        elif line.startswith("LO"):
            m = re.match(r'^LO\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', line)
            if m:
                ast.add_line(
                    x=int(m.group(1)),
                    y=int(m.group(2)),
                    width=int(m.group(3)),
                    height=int(m.group(4))
                )
        elif line.startswith("X"):
            m = re.match(r'^X\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)', line)
            if m:
                ast.add_box(
                    x=int(m.group(1)),
                    y=int(m.group(2)),
                    thickness=int(m.group(3)),
                    width=int(m.group(4)),
                    height=int(m.group(5))
                )
        elif line.startswith("P"):
            m = re.match(r'^P\s*(\d+)', line)
            if m:
                ast.copies = int(m.group(1))

    return ast


# ---------------------------------------------------------------------------
# High-Level Interface Contracts (PROJECT.md)
# ---------------------------------------------------------------------------
def render_intermediate_ast(ast_data: Union[dict, LabelAST], emulation: str = "PPLB") -> bytes:
    """
    Compiles Intermediate Label AST (JSON dict or LabelAST) into printer command stream.
    Supports emulation = "PPLB" or "PPLA".
    """
    if isinstance(ast_data, dict):
        ast = LabelAST.from_dict(ast_data)
    elif isinstance(ast_data, LabelAST):
        ast = ast_data
    else:
        raise TypeError(f"Expected dict or LabelAST, got {type(ast_data)}")

    if emulation.upper() == "PPLA":
        return PPLAEmitter.emit(ast)
    else:
        return PPLBEmitter.emit(ast)


def convert_bartender_to_pplb(
    template_path_or_content: str,
    variables: Optional[Dict[str, Any]] = None
) -> bytes:
    """
    Parses BarTender template or export (.prd/.prn, BTXML, CSV, or raw PPLB)
    and returns raw PPLB byte stream (.prd).
    """
    content = ""
    path_candidate = Path(template_path_or_content)
    if path_candidate.exists() and path_candidate.is_file():
        with open(path_candidate, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    else:
        content = template_path_or_content

    trimmed = content.strip()

    # Check if BTXML
    if trimmed.startswith("<") and ("<XMLScript" in trimmed or "<Label" in trimmed):
        parsed = parse_btxml(trimmed)
        if parsed.get("ast"):
            return PPLBEmitter.emit(parsed["ast"])
        elif parsed.get("records") and not variables:
            variables = parsed["records"][0]

    # Check if JSON AST
    if trimmed.startswith("{") and "width_dots" in trimmed:
        try:
            ast_dict = json.loads(trimmed)
            return render_intermediate_ast(ast_dict, emulation="PPLB")
        except json.JSONDecodeError:
            pass

    # Parameterized .prd / .prn text template
    rendered = substitute_placeholders(trimmed, variables)

    # Dynamic quantity injection if QUANTITY in variables
    if variables and "QUANTITY" in variables:
        try:
            qty = int(variables["QUANTITY"])
            return inject_quantity(rendered, qty, emulation="PPLB")
        except (ValueError, TypeError):
            pass

    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered.encode("utf-8")


def convert_bartender_to_ppla(
    template_path_or_content: str,
    variables: Optional[Dict[str, Any]] = None
) -> bytes:
    """
    Parses BarTender template or export and returns raw PPLA (Datamax DPL) byte stream.
    """
    content = ""
    path_candidate = Path(template_path_or_content)
    if path_candidate.exists() and path_candidate.is_file():
        with open(path_candidate, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    else:
        content = template_path_or_content

    trimmed = content.strip()

    # Check if BTXML
    if trimmed.startswith("<") and ("<XMLScript" in trimmed or "<Label" in trimmed):
        parsed = parse_btxml(trimmed)
        if parsed.get("ast"):
            return PPLAEmitter.emit(parsed["ast"])
        elif parsed.get("records") and not variables:
            variables = parsed["records"][0]

    # Check if JSON AST
    if trimmed.startswith("{") and "width_dots" in trimmed:
        try:
            ast_dict = json.loads(trimmed)
            return render_intermediate_ast(ast_dict, emulation="PPLA")
        except json.JSONDecodeError:
            pass

    # Parameterized template substitution
    rendered = substitute_placeholders(trimmed, variables)

    # Parse PPLB commands into AST, then emit PPLA
    ast = parse_pplb_to_ast(rendered)
    if variables and "QUANTITY" in variables:
        try:
            ast.copies = int(variables["QUANTITY"])
        except (ValueError, TypeError):
            pass

    return PPLAEmitter.emit(ast)
