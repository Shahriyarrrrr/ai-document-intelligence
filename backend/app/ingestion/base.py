"""Shared types for the ingestion layer.

Every reader returns the same `ExtractedPage` shape regardless of whether the
source was a digital PDF, a scanned TIFF or a spreadsheet sheet. That uniformity
is what lets the downstream extraction, validation and search stages stay
format-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class IngestionError(Exception):
    """Raised when a file cannot be read at all.

    Carries a machine-readable `code` so the API can distinguish an encrypted
    PDF (user can fix it) from a truly corrupt one (user cannot).
    """

    def __init__(self, message: str, code: str = "ingestion_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ExtractedPage:
    page_number: int  # 1-based
    text: str
    used_ocr: bool = False
    ocr_confidence: float | None = None
    label: str | None = None  # sheet name, or None for PDFs/images

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


@dataclass
class IngestionResult:
    pages: list[ExtractedPage] = field(default_factory=list)
    source_kind: str = "unknown"
    # Non-fatal problems: pages that failed OCR, sheets that were empty, etc.
    # The document still completes; these surface in the UI as warnings.
    warnings: list[str] = field(default_factory=list)
    # Structured table rows pulled from spreadsheets/CSVs, used by the
    # cross-verification engine.
    tables: list[dict] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for p in self.pages if p.used_ocr)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text.strip())
