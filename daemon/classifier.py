import json
import logging
from typing import List, Optional, Literal, Union
from pydantic import BaseModel, Field

from daemon.gemini_pool import GeminiKeyPoolManager

logger = logging.getLogger("Classifier")

class CartridgeItem(BaseModel):
    item_name: str
    description: Optional[str] = ""
    handwritten_note: Optional[str] = None
    color_group: Literal["BLACK", "CYAN", "MAGENTA", "YELLOW", "OTHER"] = "BLACK"
    quantity: int = 1
    matched_label_file: Optional[str] = None

class QuoteCartridgeSheet(BaseModel):
    document_type: Literal["QUOTE_CARTRIDGE_LABELS"] = "QUOTE_CARTRIDGE_LABELS"
    quote_number: str
    items_to_print: List[CartridgeItem]
    requires_confirmation: bool = False
    confirmation_reason: Optional[str] = None

class BoxItemContent(BaseModel):
    quantity: int
    unit_type: Literal["Toner Unit", "Drum Unit", "Accessory", "Other"] = "Toner Unit"
    model: str

class OuterBoxSheet(BaseModel):
    document_type: Literal["PICK_PACK_OUTER_BOX"] = "PICK_PACK_OUTER_BOX"
    quote_number: str
    weight_kg: str
    current_box: int = 1
    total_boxes: int = 1
    copies_per_box: int = 2
    box_contents: List[BoxItemContent]
    requires_confirmation: bool = False
    confirmation_reason: Optional[str] = None

SYSTEM_CLASSIFICATION_PROMPT = """
You are an expert industrial document parser for a thermal transfer label printing station.
Analyze the attached scanned document and classify it into one of two operational workflows:

======================================================================
WORKFLOW A: QUOTATION / TAX INVOICE (CARTRIDGE LABELS)
======================================================================
- The document has a header saying 'Quote', 'Quotation', 'Tax Invoice', or similar with customer billing info.
- It contains a printed table of line items with columns typically arranged as:
  [Mark / Tick] | [Item Code / Name] | [Description] | [Quantity] | [Price] | [Total]
- YOUR TASK:
  1. Inspect the left margin, check boxes, or rows for handwritten circles, 'X' marks, or ticks.
  2. ONLY extract items that have been marked with a circle, checkmark, or 'X'. Ignore unmarked rows.
  3. For each marked row, extract:
     - item_name (e.g. 'Dell HCF 283x', 'CB540A', 'Q2612A', 'CE285A')
     - description (full printed description)
     - quantity: The integer number from the Quantity column (e.g. 10, 30, 100).
       * IMPORTANT: If there is a handwritten quantity annotation (e.g. 'only 5', 'x2'), use the handwritten quantity!
     - handwritten_note: Any notes written by staff (e.g. 'Mark as Canon', 'Canon 716', 'HCF only').
     - color_group: Categorize as 'BLACK', 'CYAN', 'MAGENTA', or 'YELLOW' based on the cartridge code/description (e.g. CB540A/Black -> BLACK, CB541A/Cyan -> CYAN).
     - matched_label_file: Suggest a clean sanitized filename with .prn extension (e.g. 'Dell_HCF_283x.prn', 'CANON_716_BLACK.prn').
  4. Output JSON matching the QUOTE_CARTRIDGE_LABELS schema below.

======================================================================
WORKFLOW B: PICK & PACK ORDER (OUTER BOX LABELS)
======================================================================
- The document is a handwritten or printed warehouse Pick & Pack packing sheet / box list.
- Contains Quote Number at top, Box Weight, and Box Count (e.g., 'Box 1 of 5', '1/5', '1-5').
- Lists items physically packed inside that specific carton/box.
- YOUR TASK:
  1. Extract quote_number (e.g. 'QT-90124').
  2. Extract weight_kg (e.g. '14.5 kg' or '8.2').
  3. Extract current_box and total_boxes (e.g. current_box: 1, total_boxes: 5).
  4. Extract each item in the box:
     - quantity (e.g. 5)
     - unit_type: Classify as 'Toner Unit' or 'Drum Unit':
       * 'Toner Unit': Standard laser toner cartridges (e.g. HP CB540A, Canon, Dell 283x, Brother TN series).
       * 'Drum Unit': Imaging drums / photoconductors (e.g. Brother DR3115, DR3215, DR-xxx, Lexmark imaging units).
     - model: The model name (e.g. 'CB540A Black' or 'DR3115').
  5. copies_per_box must ALWAYS be 2 (one for the front of the box, one for the back).
  6. Output JSON matching the PICK_PACK_OUTER_BOX schema below.

======================================================================
HANDWRITING CONFIRMATION SAFETY RULE:
======================================================================
If any critical handwritten text is severely smudged, crossed out multiple times, or ambiguous (e.g. quantity could be 3 or 8), set:
"requires_confirmation": true
"confirmation_reason": "Specific description of the ambiguity (e.g. 'Handwritten quantity on line 2 is ambiguous between 3 and 8')"
Otherwise set "requires_confirmation": false.

======================================================================
OUTPUT STRICTLY IN ONE OF THESE TWO JSON FORMATS (NO CONVERSATIONAL CHATTER):
======================================================================

Format 1 (Workflow A):
{
  "document_type": "QUOTE_CARTRIDGE_LABELS",
  "quote_number": "QT-90124",
  "items_to_print": [
    {
      "item_name": "Dell HCF 283x",
      "description": "Dell High Capacity Toner 283X",
      "handwritten_note": null,
      "color_group": "BLACK",
      "quantity": 30,
      "matched_label_file": "Dell_HCF_283x.prn"
    }
  ],
  "requires_confirmation": false,
  "confirmation_reason": null
}

Format 2 (Workflow B):
{
  "document_type": "PICK_PACK_OUTER_BOX",
  "quote_number": "QT-90124",
  "weight_kg": "14.5",
  "current_box": 1,
  "total_boxes": 5,
  "copies_per_box": 2,
  "box_contents": [
    {
      "quantity": 5,
      "unit_type": "Toner Unit",
      "model": "Dell HCF 283x"
    },
    {
      "quantity": 2,
      "unit_type": "Drum Unit",
      "model": "DR3115"
    }
  ],
  "requires_confirmation": false,
  "confirmation_reason": null
}
"""

class DocumentClassifier:
    def __init__(self, key_pool: GeminiKeyPoolManager):
        self.key_pool = key_pool

    def classify_and_extract(self, pdf_bytes: bytes) -> Union[QuoteCartridgeSheet, OuterBoxSheet]:
        """
        Sends the in-memory PDF to Gemini Flash and parses into Pydantic models.
        """
        raw_json = self.key_pool.analyze_document_in_memory(pdf_bytes, SYSTEM_CLASSIFICATION_PROMPT)
        
        # Clean response if wrapped in markdown code fence
        cleaned = raw_json.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        data = json.loads(cleaned)
        doc_type = data.get("document_type")

        if doc_type == "QUOTE_CARTRIDGE_LABELS":
            return QuoteCartridgeSheet.model_validate(data)
        elif doc_type == "PICK_PACK_OUTER_BOX":
            return OuterBoxSheet.model_validate(data)
        else:
            raise ValueError(f"Unknown document_type returned by AI: {doc_type}")
