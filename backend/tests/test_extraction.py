"""Extraction tests: parsing primitives, the rule-based extractor, and the
LLM client's failure handling (with a stub, so no network or key is needed)."""
from __future__ import annotations

import os

import pytest

from app.extraction import fallback, llm
from app.extraction.schema import (
    InvoiceExtraction,
    normalise_currency,
    parse_amount,
    parse_date,
)
from app.ingestion.router import ingest

from .conftest import FILES_DIR


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1234.56", 1234.56),
            ("$1,234.56", 1234.56),
            ("1.234,56", 1234.56),      # European convention
            ("1,234", 1234.0),          # thousands, not a decimal comma
            ("1234,56", 1234.56),       # decimal comma
            ("EUR 2,400.00", 2400.0),
            ("(500.00)", -500.0),       # parenthesised negative
            ("500.00CR", -500.0),       # credit marker
            ("-42.10", -42.10),
            (1234.56, 1234.56),
            ("", None),
            ("N/A", None),
            (None, None),
            ("not a number", None),
        ],
    )
    def test_amounts(self, raw, expected):
        assert parse_amount(raw) == expected


class TestParseDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-03-25", "2026-03-25"),
            ("25/03/2026", "2026-03-25"),
            ("25 March 2026", "2026-03-25"),
            ("Mar 25, 2026", "2026-03-25"),
            ("25.03.2026", "2026-03-25"),
            ("20260325", "2026-03-25"),
            ("not a date", None),
            ("", None),
            (None, None),
        ],
    )
    def test_dates(self, raw, expected):
        assert parse_date(raw) == expected


class TestCurrency:
    @pytest.mark.parametrize(
        "raw,expected",
        [("USD", "USD"), ("$", "USD"), ("eur", "EUR"), ("£", "GBP"), ("", None)],
    )
    def test_currency(self, raw, expected):
        assert normalise_currency(raw) == expected


class TestSchemaNeverInvents:
    def test_null_tokens_become_none(self):
        invoice = InvoiceExtraction.model_validate(
            {"invoice_number": "N/A", "supplier_name": "not found", "total": ""}
        )
        assert invoice.invoice_number is None
        assert invoice.supplier_name is None
        assert invoice.total is None

    def test_missing_total_is_not_computed(self):
        """The schema must not fill `total` from subtotal + tax - that is the
        validator's job, and silently deriving it would hide a real gap."""
        invoice = InvoiceExtraction.model_validate({"subtotal": 100, "tax": 20})
        assert invoice.total is None

    def test_line_amount_is_derived_only_when_determined(self):
        invoice = InvoiceExtraction.model_validate(
            {"line_items": [{"description": "x", "quantity": 3, "unit_price": 10}]}
        )
        assert invoice.line_items[0].amount == 30.0

    def test_confidence_normalised_from_percent(self):
        invoice = InvoiceExtraction.model_validate(
            {"invoice_number": "A1", "field_confidence": {"invoice_number": 85}}
        )
        assert invoice.field_confidence["invoice_number"] == 0.85


class TestRuleBasedExtractor:
    @pytest.mark.parametrize(
        "filename",
        [
            "digital_INV-2026-00001.pdf",
            "scanned_medium_INV-2026-00016.pdf",
            "image_light_INV-2026-00023.jpg",
        ],
    )
    def test_core_fields_match_ground_truth(self, filename, ground_truth):
        text = ingest(os.path.join(FILES_DIR, filename)).full_text
        invoice = fallback.extract(text)
        truth = ground_truth[filename]
        assert invoice.invoice_number == truth["invoice_number"]
        assert invoice.invoice_date == truth["invoice_date"]
        assert invoice.currency == truth["currency"]
        assert invoice.total == truth["total"]

    def test_line_items_extracted(self, ground_truth):
        filename = "digital_INV-2026-00001.pdf"
        text = ingest(os.path.join(FILES_DIR, filename)).full_text
        invoice = fallback.extract(text)
        assert len(invoice.line_items) == len(ground_truth[filename]["line_items"])

    def test_header_word_not_read_as_invoice_number(self):
        """Regression: 'INVOICE\\nNorthwind Traders' matched the pattern
        `invoice\\s*no` and yielded 'rthwind' as the invoice number."""
        text = "INVOICE\nNorthwind Traders Ltd\nInvoice Number: INV-2026-00033\n"
        assert fallback.extract(text).invoice_number == "INV-2026-00033"

    def test_totals_row_not_treated_as_line_item(self):
        text = (
            "Description   Qty   Unit Price   Amount\n"
            "Widget   2   $5.00   $10.00\n"
            "Subtotal:   $10.00\n"
            "TOTAL:   $10.00\n"
        )
        items = fallback.extract_line_items(text)
        assert len(items) == 1
        assert items[0].description == "Widget"


class _StubClient:
    """Scripted LLM client, so retry and fallback behaviour is testable."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.calls = 0

    def complete(self, system: str, user: str):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response, {"prompt_tokens": 10, "completion_tokens": 5}


class TestLlmClient:
    def test_parses_clean_json(self):
        payload = '{"invoice_number": "INV-1", "total": "1,234.56"}'
        invoice, meta = llm.extract_invoice(
            text="x", filename="f.pdf", source_kind="pdf_digital",
            client=_StubClient([payload]),
        )
        assert invoice.invoice_number == "INV-1"
        assert invoice.total == 1234.56
        assert meta["method"] == "llm"

    def test_strips_markdown_fences(self):
        payload = '```json\n{"invoice_number": "INV-2"}\n```'
        invoice, _ = llm.extract_invoice(
            text="x", filename="f.pdf", source_kind="pdf_digital",
            client=_StubClient([payload]),
        )
        assert invoice.invoice_number == "INV-2"

    def test_extracts_json_surrounded_by_prose(self):
        payload = 'Here you go:\n{"invoice_number": "INV-3", "line_items": []}\nDone.'
        invoice, _ = llm.extract_invoice(
            text="x", filename="f.pdf", source_kind="pdf_digital",
            client=_StubClient([payload]),
        )
        assert invoice.invoice_number == "INV-3"

    def test_retries_then_succeeds(self):
        client = _StubClient(["not json at all", '{"invoice_number": "INV-4"}'])
        invoice, meta = llm.extract_invoice(
            text="x", filename="f.pdf", source_kind="pdf_digital", client=client,
        )
        assert invoice.invoice_number == "INV-4"
        assert meta["attempts"] == 2
        assert len(meta["errors"]) == 1

    def test_falls_back_to_rules_after_exhausting_retries(self):
        client = _StubClient(["garbage"])
        text = "Invoice Number: INV-9\nTOTAL: $50.00\n"
        invoice, meta = llm.extract_invoice(
            text=text, filename="f.pdf", source_kind="pdf_digital", client=client,
        )
        assert meta["method"] == "rules_after_llm_failure"
        assert invoice.invoice_number == "INV-9"

    def test_api_exception_does_not_propagate(self):
        client = _StubClient([RuntimeError("rate limited")])
        invoice, meta = llm.extract_invoice(
            text="Invoice Number: INV-7\n", filename="f.pdf",
            source_kind="pdf_digital", client=client,
        )
        assert meta["method"] == "rules_after_llm_failure"
        assert invoice is not None

    def test_long_text_truncated_keeping_head_and_tail(self):
        text = "HEAD-MARKER\n" + ("filler line\n" * 20000) + "TAIL-MARKER\n"
        truncated, was_truncated = llm._truncate(text)
        assert was_truncated
        assert "HEAD-MARKER" in truncated and "TAIL-MARKER" in truncated
        assert len(truncated) < len(text)
