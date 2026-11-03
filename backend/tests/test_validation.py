"""Validation and cross-verification tests."""
from __future__ import annotations

import pytest

from app.extraction.schema import InvoiceExtraction
from app.validation import crosscheck as cc
from app.validation.rules import needs_review, validate


def make(**kwargs) -> InvoiceExtraction:
    base = {
        "invoice_number": "INV-1",
        "invoice_date": "2026-01-15",
        "supplier_name": "Acme Ltd",
        "customer_name": "Buyer Inc",
        "currency": "USD",
        "subtotal": 100.0,
        "tax": 20.0,
        "total": 120.0,
    }
    base.update(kwargs)
    return InvoiceExtraction.model_validate(base)


def codes(issues) -> set[str]:
    return {i.code for i in issues}


class TestArithmetic:
    def test_consistent_invoice_is_clean(self):
        assert not needs_review(validate(make(), {}))

    def test_total_mismatch_is_an_error(self):
        issues = validate(make(total=145.0), {})
        assert "total_mismatch" in codes(issues)
        assert needs_review(issues)

    def test_rounding_within_tolerance_passes(self):
        """0.1 + 0.2 != 0.3 in float. A validator that fires on correct
        invoices is worse than none, so comparison uses Decimal + tolerance."""
        assert "total_mismatch" not in codes(validate(make(total=120.01), {}))

    def test_zero_tax_is_valid(self):
        assert "total_mismatch" not in codes(
            validate(make(subtotal=100.0, tax=0.0, total=100.0), {})
        )

    def test_line_items_must_sum_to_subtotal(self):
        invoice = make(
            line_items=[
                {"description": "a", "quantity": 1, "unit_price": 30, "amount": 30},
                {"description": "b", "quantity": 1, "unit_price": 40, "amount": 40},
            ]
        )
        assert "line_items_sum_mismatch" in codes(validate(invoice, {}))

    def test_line_item_arithmetic_checked(self):
        invoice = make(
            subtotal=100.0,
            line_items=[
                {"description": "a", "quantity": 3, "unit_price": 10, "amount": 100}
            ],
        )
        assert "line_item_arithmetic" in codes(validate(invoice, {}))


class TestFieldChecks:
    def test_missing_required_field(self):
        issues = validate(make(invoice_number=None), {})
        assert "missing_required_field" in codes(issues)
        assert needs_review(issues)

    def test_missing_recommended_is_only_a_warning(self):
        issues = validate(make(customer_name=None), {})
        assert "missing_recommended_field" in codes(issues)
        assert not needs_review(issues)

    def test_future_date_warns(self):
        assert "future_date" in codes(validate(make(invoice_date="2099-01-01"), {}))

    def test_implausible_amount_errors(self):
        issues = validate(make(subtotal=1e9, tax=0.0, total=1e9), {})
        assert "implausible_amount" in codes(issues)

    def test_implausible_tax_rate_warns(self):
        assert "implausible_tax_rate" in codes(
            validate(make(subtotal=100.0, tax=60.0, total=160.0), {})
        )

    def test_low_confidence_warns(self):
        invoice = make(field_confidence={"supplier_name": 0.2})
        assert "low_confidence" in codes(validate(invoice, {}))

    def test_a_broken_rule_does_not_fail_the_document(self, monkeypatch):
        from app.validation import rules

        def boom(inv, ctx):
            raise RuntimeError("rule exploded")

        monkeypatch.setattr(rules, "ALL_RULES", [boom, rules.rule_totals_arithmetic])
        issues = rules.validate(make(total=145.0), {})
        assert "rule_error" in codes(issues)
        assert "total_mismatch" in codes(issues)


class TestDuplicates:
    def test_same_number_and_supplier_is_a_duplicate(self):
        existing = [("INV-1", "Acme Ltd", 120.0, "doc-a")]
        assert "duplicate_invoice" in codes(validate(make(), {"existing": existing}))

    def test_same_number_different_supplier_and_total_is_not(self):
        """Different suppliers legitimately reuse short sequence numbers."""
        existing = [("INV-1", "Totally Different Co", 9999.0, "doc-a")]
        assert "duplicate_invoice" not in codes(
            validate(make(), {"existing": existing})
        )

    def test_formatting_differences_still_match(self):
        existing = [("inv-1", "acme ltd", 120.0, "doc-a")]
        assert "duplicate_invoice" in codes(validate(make(), {"existing": existing}))


def record(number, supplier="Acme Ltd", total=100.0, date="2026-05-01", **kw):
    return cc.Record(
        source_id=kw.get("sid", "s"), source_label=kw.get("label", "f.pdf"),
        invoice_number=number, supplier_name=supplier, total=total,
        invoice_date=date,
    )


class TestCrossCheck:
    def test_exact_match(self):
        report = cc.cross_check([record("INV-2026-00001")], [record("INV-2026-00001")])
        assert report.counts()["match"] == 1

    def test_formatting_differences_match(self):
        report = cc.cross_check([record("INV-2026-00001")], [record("inv 2026 00001")])
        assert report.counts()["match"] == 1

    def test_consecutive_invoices_must_not_be_merged(self):
        """Regression. Edit-distance matching scored INV-2026-00001 against
        INV-2026-00002 at 0.92 and merged two genuinely different invoices -
        hiding a missing one behind a false match."""
        report = cc.cross_check(
            [record("INV-2026-00001", total=100.0)],
            [record("INV-2026-00002", total=250.0)],
        )
        counts = report.counts()
        assert counts["match"] == 0 and counts["mismatch"] == 0
        assert counts["only_left"] == 1 and counts["only_right"] == 1

    def test_ocr_damaged_number_matches_with_corroboration(self):
        """The same regression in the other direction: real OCR damage scored
        0.58 and was missed. Canonicalising confusable glyphs catches it."""
        report = cc.cross_check(
            [record("INV-2O26-OOOO1", total=100.0)],
            [record("INV-2026-00001", total=100.0)],
        )
        assert report.counts()["match"] == 1
        assert "ocr_normalised" in report.pairs[0].matched_by

    def test_ocr_damaged_number_without_corroboration_does_not_match(self):
        report = cc.cross_check(
            [record("INV-2O26-OOOO1", supplier="Acme Ltd", total=100.0)],
            [record("INV-2026-00001", supplier="Zeta Corp", total=999.0)],
        )
        assert report.counts()["match"] == 0

    def test_field_difference_reported_as_mismatch(self):
        report = cc.cross_check(
            [record("INV-7", total=100.0)], [record("INV-7", total=105.0)]
        )
        assert report.counts()["mismatch"] == 1
        diffs = report.to_rows()[0]["differences"]
        assert any(d["field"] == "total" for d in diffs)

    def test_legal_suffix_is_not_a_difference(self):
        report = cc.cross_check(
            [record("INV-8", supplier="Acme Ltd")],
            [record("INV-8", supplier="Acme Limited")],
        )
        assert report.counts()["match"] == 1

    def test_unmatched_on_both_sides_counted(self):
        report = cc.cross_check([record("INV-A")], [record("INV-B", total=777.0)])
        counts = report.counts()
        assert counts["only_left"] == 1 and counts["only_right"] == 1

    def test_table_without_invoice_columns_yields_no_records(self):
        """A sheet of unrelated data must not generate phantom invoices."""
        table = {"label": "Notes", "columns": ["comment", "author"],
                 "rows": [{"comment": "hi", "author": "me"}]}
        assert cc.records_from_table(table, "d", "f.xlsx") == []

    def test_column_alias_matching(self):
        table = {
            "label": "Register",
            "columns": ["Invoice No", "Date", "Vendor", "Grand Total"],
            "rows": [{"Invoice No": "INV-5", "Date": "2026-02-02",
                      "Vendor": "Acme", "Grand Total": "1,200.00"}],
        }
        records = cc.records_from_table(table, "d", "f.xlsx")
        assert len(records) == 1
        assert records[0].invoice_number == "INV-5"
        assert records[0].total == 1200.0
        assert records[0].invoice_date == "2026-02-02"
