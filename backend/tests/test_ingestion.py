"""Ingestion tests: format routing, OCR, spreadsheets, and failure paths."""
from __future__ import annotations

import os

import pytest

from app.ingestion.base import IngestionError
from app.ingestion.layout import PositionedWord, render
from app.ingestion.router import ingest, sniff_kind
from app.ingestion.spreadsheet import ingest_csv, ingest_excel

from .conftest import CORRUPT_DIR, FILES_DIR


def _file(name: str) -> str:
    return os.path.join(FILES_DIR, name)


class TestFormatDetection:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("digital_INV-2026-00001.pdf", "pdf"),
            ("register_multi_sheet.xlsx", "excel"),
            ("register.csv", "csv"),
        ],
    )
    def test_known_formats(self, filename, expected):
        assert sniff_kind(_file(filename)) == expected

    def test_image_formats(self, dataset_files):
        for name in dataset_files:
            if name.startswith("image_"):
                assert sniff_kind(_file(name)) == "image"

    def test_magic_bytes_beat_extension(self):
        """A JPEG named .pdf is detected as an image, not trusted blindly."""
        assert sniff_kind(os.path.join(CORRUPT_DIR, "mislabeled.pdf")) == "image"

    def test_docx_is_rejected_not_parsed_as_csv(self):
        """Regression: a .docx is a ZIP like .xlsx. Trusting the extension
        alone routed it to the CSV parser, which 'succeeded' on binary junk."""
        with pytest.raises(IngestionError) as exc:
            sniff_kind(os.path.join(CORRUPT_DIR, "notes.docx"))
        assert exc.value.code == "unsupported_type"


class TestFailureHandling:
    @pytest.mark.parametrize(
        "filename,code",
        [
            ("empty.pdf", "file_empty"),
            ("empty.csv", "file_empty"),
            ("truncated.pdf", "pdf_corrupt"),
            ("broken.xlsx", "xlsx_corrupt"),
            ("notes.docx", "unsupported_type"),
        ],
    )
    def test_corrupt_files_raise_with_code(self, filename, code):
        with pytest.raises(IngestionError) as exc:
            ingest(os.path.join(CORRUPT_DIR, filename))
        assert exc.value.code == code

    def test_missing_file(self):
        with pytest.raises(IngestionError):
            ingest(os.path.join(FILES_DIR, "does_not_exist.pdf"))


class TestPdf:
    def test_digital_pdf_uses_native_text(self):
        result = ingest(_file("digital_INV-2026-00001.pdf"))
        assert result.source_kind == "pdf_digital"
        assert result.ocr_page_count == 0
        assert "INV-2026-00001" in result.full_text

    def test_scanned_pdf_routes_to_ocr(self, dataset_files):
        name = next(n for n in dataset_files if n.startswith("scanned_light_"))
        result = ingest(_file(name))
        assert result.source_kind == "pdf_scanned"
        assert result.ocr_page_count == result.page_count
        assert result.pages[0].ocr_confidence is not None

    def test_columns_are_preserved(self):
        """Regression: PyMuPDF emits one table cell per line, which made line
        items unrecoverable. Rows must come back with their columns intact."""
        result = ingest(_file("digital_INV-2026-00001.pdf"))
        item_lines = [
            line for line in result.full_text.splitlines()
            if "   " in line and any(c.isdigit() for c in line)
        ]
        assert item_lines, "no multi-column rows were reconstructed"


class TestOcr:
    def test_image_formats_all_ocr(self, dataset_files):
        for name in dataset_files:
            if not name.startswith("image_"):
                continue
            result = ingest(_file(name))
            assert result.source_kind == "image"
            assert result.pages[0].used_ocr
            assert result.full_text.strip()

    def test_ocr_preserves_line_structure(self, dataset_files):
        """Joining OCR tokens with spaces produced one unparseable line."""
        name = next(n for n in dataset_files if n.startswith("image_light_"))
        result = ingest(_file(name))
        assert len(result.full_text.splitlines()) > 3


class TestSpreadsheets:
    def test_multi_sheet_workbook(self):
        pages, tables = ingest_excel(_file("register_multi_sheet.xlsx"))
        assert len(pages) == 3                     # Register, Line Items, Notes
        assert {t["label"] for t in tables} >= {"Register", "Line Items"}
        assert any("(empty)" in p.text for p in pages)   # the Notes sheet

    def test_csv_round_trip(self):
        pages, tables = ingest_csv(_file("register.csv"))
        assert len(pages) == 1
        assert tables[0]["row_count"] == 5

    def test_csv_delimiter_and_encoding_sniffing(self):
        """Semicolon-delimited latin-1 must parse identically to utf-8 commas."""
        _, comma = ingest_csv(_file("register.csv"))
        _, semi = ingest_csv(_file("register_semicolon_latin1.csv"))
        assert comma[0]["row_count"] == semi[0]["row_count"]
        assert len(comma[0]["columns"]) == len(semi[0]["columns"])


class TestLayout:
    def test_wide_gap_becomes_column_break(self):
        words = [
            PositionedWord(0, 0, 50, 10, "Description"),
            PositionedWord(200, 0, 220, 10, "Qty"),
        ]
        assert "   " in render(words)

    def test_normal_spacing_stays_one_space(self):
        words = [
            PositionedWord(0, 0, 30, 10, "Acme"),
            PositionedWord(34, 0, 70, 10, "Industrial"),
        ]
        assert render(words) == "Acme Industrial"

    def test_rows_split_on_vertical_separation(self):
        words = [
            PositionedWord(0, 0, 30, 10, "first"),
            PositionedWord(0, 40, 30, 50, "second"),
        ]
        assert render(words).splitlines() == ["first", "second"]

    def test_empty_input(self):
        assert render([]) == ""
