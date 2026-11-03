"""Format detection and dispatch.

Extension is a hint, not a fact: users rename files, and browsers send
application/octet-stream often enough that content-type alone is unreliable.
So the magic bytes win, and the extension is the fallback.
"""
from __future__ import annotations

import logging
import os
import zipfile

from .base import IngestionError, IngestionResult
from .ocr import ingest_image_file
from .pdf_reader import classify_pdf, ingest_pdf
from .spreadsheet import ingest_csv, ingest_excel

log = logging.getLogger(__name__)

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
EXCEL_EXT = {".xlsx", ".xls", ".xlsm", ".xltx"}
CSV_EXT = {".csv", ".tsv", ".txt"}

SUPPORTED_EXT = PDF_EXT | IMAGE_EXT | EXCEL_EXT | CSV_EXT

MAGIC = {
    b"%PDF": "pdf",
    b"\x89PNG": "image",
    b"\xff\xd8\xff": "image",       # JPEG
    b"II*\x00": "image",            # TIFF little-endian
    b"MM\x00*": "image",            # TIFF big-endian
    b"PK\x03\x04": "zip",           # xlsx/xlsm are zip containers
    b"\xd0\xcf\x11\xe0": "ole",     # legacy .xls
}


def _zip_is_xlsx(path: str) -> bool:
    """True if the archive contains an OOXML spreadsheet part."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return "xl/workbook.xml" in names or any(n.startswith("xl/") for n in names)


def sniff_kind(path: str) -> str:
    """Return one of: pdf, image, excel, csv."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError as exc:
        raise IngestionError(f"Cannot read file: {exc}", code="file_unreadable") from exc

    if not head:
        raise IngestionError("File is empty", code="file_empty")

    ext = os.path.splitext(path)[1].lower()

    for signature, kind in MAGIC.items():
        if head.startswith(signature):
            if kind == "zip":
                # OOXML files are all zip containers - .xlsx, but also .docx
                # and .pptx. Looking at the extension is not enough (users
                # rename things), so inspect the archive for the workbook
                # part. This is what stops a .docx being parsed as a CSV of
                # binary garbage.
                if _zip_is_xlsx(path):
                    return "excel"
                raise IngestionError(
                    "File is a ZIP/OOXML container but not a spreadsheet",
                    code="unsupported_type",
                )
            if kind == "ole":
                # Legacy OLE2: .xls, but also .doc. Only accept known
                # spreadsheet extensions.
                if ext in EXCEL_EXT:
                    return "excel"
                raise IngestionError(
                    "Legacy OLE file is not a supported spreadsheet",
                    code="unsupported_type",
                )
            return kind

    # No magic match: plain text of some kind, or an extension we trust.
    if ext in EXCEL_EXT:
        return "excel"
    if ext in CSV_EXT:
        return "csv"
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        # Extension says PDF but the header does not - corrupt header.
        raise IngestionError("File claims to be a PDF but has no PDF header",
                             code="pdf_corrupt")
    raise IngestionError(f"Unsupported file type: {ext or 'unknown'}",
                         code="unsupported_type")


def ingest(path: str) -> IngestionResult:
    """Read any supported file into the uniform IngestionResult shape."""
    kind = sniff_kind(path)
    result = IngestionResult()

    if kind == "pdf":
        pages = ingest_pdf(path)
        result.pages = pages
        result.source_kind = classify_pdf(pages)
    elif kind == "image":
        result.pages = ingest_image_file(path)
        result.source_kind = "image"
    elif kind == "excel":
        pages, tables = ingest_excel(path)
        result.pages, result.tables = pages, tables
        result.source_kind = "spreadsheet"
    elif kind == "csv":
        pages, tables = ingest_csv(path)
        result.pages, result.tables = pages, tables
        result.source_kind = "csv"
    else:  # pragma: no cover - sniff_kind raises first
        raise IngestionError(f"Unhandled kind: {kind}", code="unsupported_type")

    blank = [p.page_number for p in result.pages if not p.text.strip()]
    if blank:
        preview = ", ".join(str(n) for n in blank[:10])
        suffix = "..." if len(blank) > 10 else ""
        result.warnings.append(
            f"{len(blank)} page(s) produced no text (pages {preview}{suffix})"
        )

    low_conf = [
        p.page_number
        for p in result.pages
        if p.used_ocr and p.ocr_confidence is not None and p.ocr_confidence < 60
    ]
    if low_conf:
        result.warnings.append(
            f"{len(low_conf)} page(s) OCR'd below 60% confidence - extraction may be unreliable"
        )

    if not result.full_text.strip():
        raise IngestionError(
            "No text could be extracted from this file", code="no_text_extracted"
        )

    return result
