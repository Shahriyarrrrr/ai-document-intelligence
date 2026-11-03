"""PDF ingestion with per-page native-text / OCR routing.

The brief calls for ~1000-page PDFs, so this module never loads the whole
document into memory as images. Pages are opened lazily, rendered one at a
time only when OCR is actually needed, and the pixmap is released immediately
after. Memory stays flat regardless of page count.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pymupdf as fitz  # the `fitz` top-level alias is deprecated upstream
from PIL import Image

from ..config import settings
from .base import ExtractedPage, IngestionError
from .layout import PositionedWord, render
from .ocr import ocr_image

log = logging.getLogger(__name__)


def _open(path: str) -> fitz.Document:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise IngestionError(f"Cannot open PDF: {exc}", code="pdf_corrupt") from exc

    if doc.needs_pass:
        # Try the empty password first - many "encrypted" invoices are only
        # permission-locked, not password-locked.
        if not doc.authenticate(""):
            doc.close()
            raise IngestionError("PDF is password protected", code="pdf_encrypted")
    return doc


def _native_text(page: fitz.Page) -> str:
    """Extract the text layer with column structure intact.

    PyMuPDF's "text" mode emits one table cell per line, which makes a
    line-item table impossible to read downstream. Extracting positioned words
    and rebuilding rows preserves the columns. If the word extraction returns
    nothing (unusual encodings, Type 3 fonts), fall back to plain mode rather
    than losing the page.
    """
    words = page.get_text("words") or []
    if not words:
        return (page.get_text("text") or "").strip()
    positioned = [
        PositionedWord(x0=w[0], y0=w[1], x1=w[2], y1=w[3], text=w[4]) for w in words
    ]
    return render(positioned).strip()


def _render(page: fitz.Page, dpi: int) -> Image.Image:
    # 72 is the PDF user-space unit; the matrix scales it to the target DPI.
    zoom = dpi / 72.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    try:
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    finally:
        # Explicit: PyMuPDF pixmaps hold C-side buffers that GC is slow to reap,
        # and at 300 DPI each page is ~25MB.
        del pixmap


def iter_pdf_pages(path: str, ocr: bool = True) -> Iterator[ExtractedPage]:
    """Yield pages one at a time.

    A page whose native text layer is shorter than `ocr_char_threshold` is
    treated as scanned and rendered for OCR. This per-page decision matters:
    real-world PDFs are routinely mixed, with a digital cover sheet followed by
    scanned attachments.
    """
    doc = _open(path)
    try:
        total = doc.page_count
        if total == 0:
            raise IngestionError("PDF has no pages", code="pdf_empty")

        for index in range(total):
            page_number = index + 1
            try:
                page = doc.load_page(index)
            except Exception as exc:
                log.warning("Page %s of %s unreadable: %s", page_number, path, exc)
                yield ExtractedPage(page_number=page_number, text="")
                continue

            try:
                native = _native_text(page)
            except Exception as exc:
                log.warning("Text layer failed on page %s: %s", page_number, exc)
                native = ""

            if len(native.strip()) >= settings.ocr_char_threshold or not ocr:
                yield ExtractedPage(page_number=page_number, text=native.strip())
                continue

            # Scanned page: render and OCR.
            try:
                image = _render(page, settings.ocr_dpi)
                text, confidence = ocr_image(image)
                image.close()
                yield ExtractedPage(
                    page_number=page_number,
                    text=text,
                    used_ocr=True,
                    ocr_confidence=confidence,
                )
            except IngestionError:
                raise
            except Exception as exc:
                log.warning("OCR failed on page %s of %s: %s", page_number, path, exc)
                # Fall back to whatever sparse native text existed rather than
                # dropping the page entirely.
                yield ExtractedPage(page_number=page_number, text=native.strip())
    finally:
        doc.close()


def ingest_pdf(path: str, ocr: bool = True) -> list[ExtractedPage]:
    pages = list(iter_pdf_pages(path, ocr=ocr))
    if not pages:
        raise IngestionError("PDF produced no pages", code="pdf_empty")
    return pages


def classify_pdf(pages: list[ExtractedPage]) -> str:
    """Label the document as digital or scanned by majority of pages."""
    if not pages:
        return "pdf_digital"
    ocr_pages = sum(1 for p in pages if p.used_ocr)
    return "pdf_scanned" if ocr_pages > len(pages) / 2 else "pdf_digital"
