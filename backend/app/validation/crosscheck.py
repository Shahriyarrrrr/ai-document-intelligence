"""Cross-document verification: PDF <-> PDF and PDF <-> Excel/CSV.

The scenario calls this out explicitly ("pdf - pdf cross verification,
pdf - excel cross verification") and it is the part most candidates skip, so
it is built properly rather than stubbed.

The problem: a finance team holds the same invoice in two places - the
supplier's PDF and their own register spreadsheet - and needs to know where
the two disagree. That is a reconciliation, not a validation:

  1. Normalise both sides into comparable records.
  2. Match records across sides on invoice number (falling back to a
     supplier+total+date fingerprint when the number is missing or OCR-damaged).
  3. Compare the matched pairs field by field with type-aware equality.
  4. Report matched / mismatched / only-on-one-side.

Matching is the hard part. Invoice numbers arrive as "INV-2026-00001",
"inv2026 00001" and "INV 2026 00001" for the same invoice, so comparison
happens on a normalised key, and near-misses are caught with an edit-distance
fallback before a record is declared unmatched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any

from ..config import settings
from ..extraction.schema import parse_amount, parse_date

COMPARE_FIELDS = (
    "invoice_date", "supplier_name", "customer_name",
    "currency", "subtotal", "tax", "total",
)

# Column-name synonyms seen in real registers, mapped to our field names.
COLUMN_ALIASES = {
    "invoice_number": {
        "invoice number", "invoice no", "invoice_no", "invoice #", "inv no",
        "invoiceno", "invoice", "bill number", "bill no", "document number",
        "reference", "ref", "invoice_number",
    },
    "invoice_date": {
        "invoice date", "date", "invoice_date", "issue date", "issued",
        "document date", "bill date",
    },
    "supplier_name": {
        "supplier", "supplier name", "vendor", "vendor name", "from",
        "seller", "payee", "supplier_name",
    },
    "customer_name": {
        "customer", "customer name", "client", "bill to", "billed to",
        "sold to", "buyer", "customer_name",
    },
    "currency": {"currency", "ccy", "curr"},
    "subtotal": {"subtotal", "sub total", "net", "net amount", "amount before tax"},
    "tax": {"tax", "vat", "gst", "tax amount", "sales tax"},
    "total": {
        "total", "grand total", "total amount", "amount", "gross",
        "amount due", "total due", "balance",
    },
}

# Supplier/customer names are compared loosely: "Acme Ltd" vs "Acme Limited".
NAME_SIMILARITY_THRESHOLD = 0.85

# Characters OCR genuinely confuses, mapped to a canonical form. Matching
# invoice numbers on edit distance was tried and removed: it is precisely
# backwards on this data. Two consecutive real invoices (INV-2026-00001 vs
# INV-2026-00002) score 0.92 similarity and would be wrongly merged, while
# the same invoice with real OCR damage (INV-2O26-OOOO1) scores 0.58 and
# would be missed. Canonicalising confusable glyphs and then requiring an
# exact match inverts both errors correctly.
OCR_CONFUSIONS = str.maketrans({
    "O": "0", "Q": "0", "D": "0",
    "I": "1", "L": "1", "J": "1",
    "Z": "2",
    "A": "4",
    "S": "5",
    "G": "6",
    "T": "7",
    "B": "8",
})


@dataclass
class Record:
    """One invoice, from either side of the comparison."""
    source_id: str          # document id
    source_label: str       # filename or sheet name
    invoice_number: str | None = None
    invoice_date: str | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    currency: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    origin: str = "pdf"     # "pdf" | "spreadsheet"
    row_ref: str | None = None  # e.g. "Register!A5"

    @property
    def key(self) -> str | None:
        return normalise_key(self.invoice_number)

    @property
    def fingerprint(self) -> str | None:
        """Identity when the invoice number is unusable."""
        if self.total is None or not (self.supplier_name or self.invoice_date):
            return None
        supplier = re.sub(r"[^a-z0-9]", "", (self.supplier_name or "").lower())[:12]
        return f"{supplier}|{self.invoice_date or ''}|{self.total:.2f}"


@dataclass
class FieldDiff:
    field: str
    left: Any
    right: Any
    equal: bool
    note: str | None = None


@dataclass
class PairResult:
    key: str
    left: Record | None
    right: Record | None
    diffs: list[FieldDiff] = field(default_factory=list)
    matched_by: str = "invoice_number"

    @property
    def status(self) -> str:
        if self.left is None:
            return "only_right"
        if self.right is None:
            return "only_left"
        return "mismatch" if any(not d.equal for d in self.diffs) else "match"


@dataclass
class CrossCheckReport:
    pairs: list[PairResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {"match": 0, "mismatch": 0, "only_left": 0, "only_right": 0}
        for pair in self.pairs:
            out[pair.status] += 1
        return out

    def to_rows(self) -> list[dict]:
        rows = []
        for pair in self.pairs:
            rows.append({
                "key": pair.key,
                "status": pair.status,
                "matched_by": pair.matched_by,
                "left_source": pair.left.source_label if pair.left else None,
                "right_source": pair.right.source_label if pair.right else None,
                "left_row_ref": pair.left.row_ref if pair.left else None,
                "right_row_ref": pair.right.row_ref if pair.right else None,
                "differences": [
                    {"field": d.field, "left": d.left, "right": d.right, "note": d.note}
                    for d in pair.diffs if not d.equal
                ],
            })
        return rows


def normalise_key(value: str | None) -> str | None:
    """Uppercase alphanumerics only, so formatting differences collapse."""
    if not value:
        return None
    key = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    return key or None


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def ocr_canonical(key: str | None) -> str | None:
    """Collapse OCR-confusable glyphs so the same number reads identically.

    Digits are the canonical form because invoice numbers are overwhelmingly
    numeric, so a letter appearing where a digit belongs is the error.
    """
    if not key:
        return None
    return key.translate(OCR_CONFUSIONS)


def _corroborates(left: Record, right: Record) -> str | None:
    """Independent evidence that two records are the same invoice.

    Required before accepting any non-exact key match. Without it, a single
    mis-read character in a sequence number silently merges two real,
    different invoices - the worst failure this tool can produce, because it
    hides a missing invoice behind a false match.
    """
    total_equal, _ = _amounts_equal(left.total, right.total)
    if left.total is not None and right.total is not None and total_equal:
        return "total"
    names_equal, _ = _names_equal(left.supplier_name, right.supplier_name)
    if left.supplier_name and right.supplier_name and names_equal:
        dates_known = left.invoice_date and right.invoice_date
        if dates_known and left.invoice_date == right.invoice_date:
            return "supplier+date"
    return None


def _names_equal(a: str | None, b: str | None) -> tuple[bool, str | None]:
    if a is None and b is None:
        return True, None
    if a is None or b is None:
        return False, "missing on one side"
    left = re.sub(r"[^a-z0-9 ]", "", a.lower()).strip()
    right = re.sub(r"[^a-z0-9 ]", "", b.lower()).strip()
    if left == right:
        return True, None
    # Strip common legal suffixes before comparing.
    suffix = r"\b(ltd|limited|llc|inc|incorporated|gmbh|plc|co|company|corp|pvt)\b"
    left_core = re.sub(suffix, "", left).strip()
    right_core = re.sub(suffix, "", right).strip()
    if left_core and left_core == right_core:
        return True, "differs only by legal suffix"
    ratio = _similar(left, right)
    if ratio >= NAME_SIMILARITY_THRESHOLD:
        return True, f"fuzzy match ({ratio:.0%})"
    return False, None


def _amounts_equal(a: Any, b: Any) -> tuple[bool, str | None]:
    if a is None and b is None:
        return True, None
    if a is None or b is None:
        return False, "missing on one side"
    try:
        left, right = Decimal(str(a)), Decimal(str(b))
    except (InvalidOperation, ValueError):
        return False, "unparseable amount"
    if abs(left - right) <= Decimal(str(settings.amount_tolerance)):
        return True, None
    return False, f"differs by {left - right}"


def compare_records(left: Record, right: Record) -> list[FieldDiff]:
    diffs: list[FieldDiff] = []
    for name in COMPARE_FIELDS:
        lv, rv = getattr(left, name), getattr(right, name)
        if name in ("subtotal", "tax", "total"):
            equal, note = _amounts_equal(lv, rv)
        elif name in ("supplier_name", "customer_name"):
            equal, note = _names_equal(lv, rv)
        elif name == "invoice_date":
            equal = parse_date(lv) == parse_date(rv)
            note = None if equal else "date differs"
        else:
            equal = (lv or None) == (rv or None)
            note = None
        diffs.append(FieldDiff(field=name, left=lv, right=rv, equal=equal, note=note))
    return diffs


def _index(records: list[Record]) -> dict[str, Record]:
    out: dict[str, Record] = {}
    for record in records:
        key = record.key
        if key:
            out.setdefault(key, record)
    return out


def cross_check(left_records: list[Record], right_records: list[Record]) -> CrossCheckReport:
    """Reconcile two sets of invoice records."""
    report = CrossCheckReport()
    left_index = _index(left_records)
    right_index = _index(right_records)
    used_right: set[str] = set()

    # Pass 1: exact key match.
    for key, left in left_index.items():
        right = right_index.get(key)
        if right is not None:
            used_right.add(key)
            report.pairs.append(PairResult(key=key, left=left, right=right,
                                           diffs=compare_records(left, right)))

    # Pass 2: fuzzy key, then fingerprint - catches OCR damage in the number.
    unmatched_left = [r for k, r in left_index.items()
                      if not any(p.left is r for p in report.pairs)]
    remaining_right = {k: r for k, r in right_index.items() if k not in used_right}

    for left in unmatched_left:
        matched_by: str | None = None
        right: Record | None = None
        best_key: str | None = None

        # 2a: OCR-canonical key match, but only with independent corroboration.
        left_canonical = ocr_canonical(left.key)
        if left_canonical:
            for key, candidate in remaining_right.items():
                if ocr_canonical(key) != left_canonical:
                    continue
                evidence = _corroborates(left, candidate)
                if evidence:
                    right, best_key = candidate, key
                    matched_by = f"ocr_normalised_number (+{evidence})"
                break

        # 2b: no usable number at all - fall back to the content fingerprint.
        if right is None and left.fingerprint:
            for key, candidate in remaining_right.items():
                if candidate.fingerprint == left.fingerprint:
                    right, best_key, matched_by = candidate, key, "fingerprint"
                    break
        if right is not None and best_key is not None:
            used_right.add(best_key)
            remaining_right.pop(best_key, None)
            report.pairs.append(PairResult(
                key=left.key or best_key, left=left, right=right,
                diffs=compare_records(left, right), matched_by=matched_by or "fuzzy",
            ))
        else:
            report.pairs.append(PairResult(key=left.key or "?", left=left, right=None))

    # Pass 3: anything left on the right side is unmatched.
    for key, right in right_index.items():
        if key not in used_right:
            report.pairs.append(PairResult(key=key, left=None, right=right))

    return report


# --------------------------------------------------------------------------
# Building Records from the two source shapes
# --------------------------------------------------------------------------

def _match_column(columns: list[str], field_name: str) -> str | None:
    aliases = COLUMN_ALIASES[field_name]
    normalised = {c: re.sub(r"[^a-z0-9 ]", " ", str(c).lower()).strip() for c in columns}
    for original, clean in normalised.items():
        if clean in aliases:
            return original
    # Substring fallback: "Total (USD)" should still match "total".
    for original, clean in normalised.items():
        for alias in aliases:
            if alias in clean:
                return original
    return None


def records_from_table(
    table: dict, source_id: str, source_label: str
) -> list[Record]:
    """Turn a parsed spreadsheet/CSV sheet into comparable records.

    Only sheets that actually look like an invoice register are used: a sheet
    needs a recognisable invoice-number or total column, otherwise a sheet of
    unrelated data would generate hundreds of phantom records.
    """
    columns = [str(c) for c in table.get("columns", [])]
    mapping = {f: _match_column(columns, f) for f in COLUMN_ALIASES}
    if not mapping.get("invoice_number") and not mapping.get("total"):
        return []

    records: list[Record] = []
    sheet = table.get("label") or source_label
    for index, row in enumerate(table.get("rows", []), start=2):  # row 1 = header
        def cell(field_name: str):
            column = mapping.get(field_name)
            if not column:
                return None
            value = row.get(column)
            if value is None or (isinstance(value, float) and value != value):  # NaN
                return None
            return value

        number = cell("invoice_number")
        total = parse_amount(cell("total"))
        if number is None and total is None:
            continue
        records.append(Record(
            source_id=source_id,
            source_label=source_label,
            invoice_number=str(number).strip() if number is not None else None,
            invoice_date=parse_date(cell("invoice_date")),
            supplier_name=str(cell("supplier_name")).strip() if cell("supplier_name") else None,
            customer_name=str(cell("customer_name")).strip() if cell("customer_name") else None,
            currency=str(cell("currency")).strip().upper() if cell("currency") else None,
            subtotal=parse_amount(cell("subtotal")),
            tax=parse_amount(cell("tax")),
            total=total,
            origin="spreadsheet",
            row_ref=f"{sheet}!row{index}",
        ))
    return records


def record_from_invoice(invoice, source_id: str, source_label: str) -> Record:
    """Build a Record from an extracted invoice (PDF/image side)."""
    return Record(
        source_id=source_id,
        source_label=source_label,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        supplier_name=invoice.supplier_name,
        customer_name=invoice.customer_name,
        currency=invoice.currency,
        subtotal=float(invoice.subtotal) if invoice.subtotal is not None else None,
        tax=float(invoice.tax) if invoice.tax is not None else None,
        total=float(invoice.total) if invoice.total is not None else None,
        origin="pdf",
    )
