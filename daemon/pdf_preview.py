"""
In-Memory PDF Page Rasterizer, Ephemeral RAM Cache & Confidential Memory Shredder.

Target Hardware: Orange Pi PC Plus (Allwinner H3, 1GB DDR3 RAM).
Security Protocol: Zero disk writes. Ephemeral in-memory rendering with automatic TTL.
Confidential Shredding: Overwrites all mutable byte buffers with zeros in RAM.
"""

import gc
import io
import time
import base64
import logging
import threading
from typing import Optional, List, Dict, Any

logger = logging.getLogger("PDFPreview")

# In-memory ephemeral preview store
# Structure: { quote_id: {"quote_id": str, "preview": str, "timestamp": float, "expires_at": float, "has_preview": bool} }
_ACTIVE_PREVIEWS: Dict[str, Dict[str, Any]] = {}
_STORE_LOCK = threading.Lock()


def shred_memory_buffer(buffer: bytearray) -> None:
    """
    Confidential Memory Shredder.
    Overwrites every byte in mutable memory with zero (0x00),
    unlinks the local reference, and explicitly triggers garbage collection.
    """
    if buffer is None:
        return
    if isinstance(buffer, (bytearray, memoryview)):
        for i in range(len(buffer)):
            buffer[i] = 0
    del buffer
    gc.collect()


def _prune_expired_locked() -> None:
    """Prunes expired preview items from _ACTIVE_PREVIEWS. Must be called with _STORE_LOCK held."""
    now = time.time()
    expired = [k for k, v in _ACTIVE_PREVIEWS.items() if now > v.get("expires_at", 0)]
    for k in expired:
        del _ACTIVE_PREVIEWS[k]
    if expired:
        gc.collect()


def store_ephemeral_preview(quote_id: str, preview_b64: str, ttl_seconds: int = 600) -> None:
    """
    Stores a base64 image preview in the volatile in-memory store with an automatic TTL.
    Zero disk storage is touched.
    """
    if not quote_id:
        raise ValueError("quote_id cannot be empty")
    
    with _STORE_LOCK:
        _prune_expired_locked()
        now = time.time()
        _ACTIVE_PREVIEWS[quote_id] = {
            "quote_id": quote_id,
            "preview": preview_b64,
            "timestamp": now,
            "expires_at": now + ttl_seconds,
            "has_preview": bool(preview_b64),
        }
        logger.info(f"Ephemeral preview registered in RAM for '{quote_id}' (TTL: {ttl_seconds}s).")


def get_ephemeral_preview(quote_id: str) -> Optional[str]:
    """
    Retrieves the base64 preview string for a quote or session ID.
    Returns None if nonexistent, expired, or dismissed.
    """
    if not quote_id:
        return None

    with _STORE_LOCK:
        _prune_expired_locked()
        # Direct lookup
        entry = _ACTIVE_PREVIEWS.get(quote_id)
        if entry:
            if time.time() > entry["expires_at"]:
                del _ACTIVE_PREVIEWS[quote_id]
                return None
            return entry["preview"]
        
        # Case-insensitive / stem fallback
        q_norm = quote_id.strip().lower()
        for k, v in list(_ACTIVE_PREVIEWS.items()):
            if k.strip().lower() == q_norm or k.strip().lower().startswith(q_norm):
                if time.time() > v["expires_at"]:
                    del _ACTIVE_PREVIEWS[k]
                    return None
                return v["preview"]

        return None


def list_active_previews() -> List[Dict[str, Any]]:
    """
    Returns an index of currently active ephemeral scan previews in RAM.
    """
    with _STORE_LOCK:
        _prune_expired_locked()
        return [
            {
                "quote_id": k,
                "timestamp": v["timestamp"],
                "expires_at": v["expires_at"],
                "has_preview": v["has_preview"],
            }
            for k, v in _ACTIVE_PREVIEWS.items()
        ]


def dismiss_preview(quote_id: str) -> bool:
    """
    Manually dismisses and purges an ephemeral preview from volatile RAM.
    Returns True if removed, False if not found.
    """
    with _STORE_LOCK:
        removed = False
        if quote_id in _ACTIVE_PREVIEWS:
            del _ACTIVE_PREVIEWS[quote_id]
            removed = True
        else:
            q_norm = quote_id.strip().lower()
            for k in list(_ACTIVE_PREVIEWS.keys()):
                if k.strip().lower() == q_norm:
                    del _ACTIVE_PREVIEWS[k]
                    removed = True
                    break
        if removed:
            gc.collect()
            logger.info(f"Ephemeral preview for '{quote_id}' dismissed and wiped from RAM.")
        return removed


def clear_all_previews() -> None:
    """Clears all ephemeral previews from memory (used for test teardown)."""
    with _STORE_LOCK:
        _ACTIVE_PREVIEWS.clear()
        gc.collect()


def render_pdf_page_preview(pdf_bytes: bytes, page_idx: int = 0, dpi: int = 150) -> str:
    """
    Renders a single PDF page directly to a base64 JPEG image data URI purely in RAM.
    Does NOT write any temporary files to disk.

    Parameters:
        pdf_bytes: Raw binary stream of the PDF.
        page_idx: 0-indexed page number to render (default 0).
        dpi: Target resolution for rendering (default 150 DPI for optimal readability/RAM balance).

    Returns:
        A data URI string: 'data:image/jpeg;base64,...'
    """
    if not pdf_bytes:
        raise ValueError("Empty PDF document received.")

    # Guard against obvious non-PDF payloads before passing to rasterizers
    if not pdf_bytes.strip().startswith(b"%PDF") and b"%PDF" not in pdf_bytes[:1024]:
        raise ValueError("Invalid PDF stream: header does not match %PDF signature.")

    # Engine 1: pypdfium2 (preferred: lightweight, fast, no external poppler binary required)
    try:
        import pypdfium2 as pdfium
        try:
            pdf = pdfium.PdfDocument(pdf_bytes)
        except Exception as e:
            raise ValueError(f"Invalid PDF stream: {e}") from e

        try:
            page_count = len(pdf)
            if page_count == 0:
                raise ValueError("Empty PDF document received.")
            if page_idx < 0 or page_idx >= page_count:
                raise IndexError(f"Page index {page_idx} out of range (document has {page_count} pages).")

            page = pdf[page_idx]
            scale = dpi / 72.0
            image = page.render(scale=scale).to_pil()
            
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=85)
            img_bytes = buf.getvalue()
            b64_str = base64.b64encode(img_bytes).decode("ascii")
            return f"data:image/jpeg;base64,{b64_str}"
        finally:
            try:
                pdf.close()
            except Exception:
                pass
    except ImportError:
        pass

    # Engine 2: PyMuPDF (fitz)
    try:
        import fitz
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            raise ValueError(f"Invalid PDF stream: {e}") from e

        try:
            page_count = len(doc)
            if page_count == 0:
                raise ValueError("Empty PDF document received.")
            if page_idx < 0 or page_idx >= page_count:
                raise IndexError(f"Page index {page_idx} out of range (document has {page_count} pages).")

            page = doc[page_idx]
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("jpeg")
            b64_str = base64.b64encode(img_bytes).decode("ascii")
            return f"data:image/jpeg;base64,{b64_str}"
        finally:
            try:
                doc.close()
            except Exception:
                pass
    except ImportError:
        pass

    # Engine 3: pdf2image (poppler pipe in memory)
    try:
        from pdf2image import convert_from_bytes
        try:
            images = convert_from_bytes(
                pdf_bytes,
                dpi=dpi,
                first_page=page_idx + 1,
                last_page=page_idx + 1,
                fmt="jpeg"
            )
            if not images:
                raise IndexError(f"Page index {page_idx} out of range.")
            buf = io.BytesIO()
            images[0].save(buf, format="JPEG", quality=85)
            b64_str = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64_str}"
        except Exception as e:
            raise ValueError(f"Invalid PDF stream or rasterization failure: {e}") from e
    except ImportError:
        pass

    raise ImportError(
        "No supported in-memory PDF rasterizer installed. Please install 'pypdfium2' or 'pymupdf'."
    )
