"""Deterministic rule-based extractor.

This exists for three reasons, in order of importance:

1. **Graceful degradation.** If the LLM API is down, rate-limited or
   unconfigured, documents still get processed with reduced accuracy rather
   than piling up in a failed queue.
2. **A measured baseline.** The evaluation report compares LLM accuracy
   against this. "92% accurate" means little on its own; "92% vs a 61%
   regex baseline" quantifies what the model is actually buying.
3. **Offline tests.** The whole suite runs in CI with no API key and no
   network.

It is label-driven: find a known label, take the value near it. That is
reliable on well-formed invoices and degrades honestly on OCR noise, which is
exactly the behaviour wanted from a fallback.
"""
from __future__ import annotations

import re

from .schema import InvoiceExtraction, LineItem, parse_amount, parse_date

# Ordered by specificity - the first pattern that hits wins.
# `[ \t]*` rather than `\s*`: a label and its value are on the same line.
# Allowing \s to cross a newline made "INVOICE\nNorthwind Traders" match as
# label "INVOICE No" + value "rthwind".
# `\b` after the keyword is the other half of that fix - without it, the "No"
# inside "Northwind" is read as the abbreviation for "number".
INVOICE_NO_PATTERNS = [
    r"invoice[ \t]*(?:number|no\.?|#)\b[ \t]*[:\-]?[ \t]*([A-Z0-9][A-Z0-9\-/_]{2,30})",
    r"\binv(?:oice)?[ \t]*[#:][ \t]*([A-Z0-9][A-Z0-9\-/_]{2,30})",
    r"\b(INV[-/][A-Z0-9\-/]{3,25})\b",
    r"bill[ \t]*(?:number|no\.?|#)\b[ \t]*[:\-]?[ \t]*([A-Z0-9][A-Z0-9\-/_]{2,30})",
]

DATE_PATTERNS = [
    r"invoice[ \t]*date[ \t]*[:\-]?[ \t]*([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})",
    r"invoice[ \t]*date[ \t]*[:\-]?[ \t]*(\d{1,2}[ \t]+[A-Za-z]{3,9}[ \t]+\d{4})",
    r"invoice[ \t]*date[ \t]*[:\-]?[ \t]*([A-Za-z]{3,9}[ \t]+\d{1,2},?[ \t]+\d{4})",
    r"\bdate[ \t]*[:\-]?[ \t]*([0-9]{1,4}[-/.][0-9]{1,2}[-/.][0-9]{1,4})",
    r"\bdated?[ \t]*[:\-]?[ \t]*(\d{1,2}[ \t]+[A-Za-z]{3,9}[ \t]+\d{4})",
]

AMOUNT = r"([\(\-]?\s*(?:[$€£৳₹]|USD|EUR|GBP|BDT|INR)?\s*[\d,.]+\s*\)?(?:\s*CR)?)"

AMOUNT_PATTERNS = {
    "subtotal": [
        rf"sub[\s\-]?total\s*[:\-]?\s*{AMOUNT}",
        rf"net\s*(?:amount|total)\s*[:\-]?\s*{AMOUNT}",
        rf"amount\s*before\s*tax\s*[:\-]?\s*{AMOUNT}",
    ],
    "tax": [
        rf"\b(?:tax|vat|gst|sales\s*tax)\s*(?:\([\d.]+%\))?\s*[:\-]?\s*{AMOUNT}",
        rf"\btax\s*amount\s*[:\-]?\s*{AMOUNT}",
    ],
    "total": [
        rf"\btotal\s*(?:due|payable|amount)?\s*[:\-]?\s*{AMOUNT}",
        rf"\bamount\s*due\s*[:\-]?\s*{AMOUNT}",
        rf"\bgrand\s*total\s*[:\-]?\s*{AMOUNT}",
        rf"\bbalance\s*due\s*[:\-]?\s*{AMOUNT}",
    ],
}

CURRENCY_PATTERNS = [
    r"\bcurrency\s*[:\-]?\s*([A-Z]{3})\b",
    r"\b(USD|EUR|GBP|BDT|INR|JPY|AUD|CAD)\b",
    r"([$€£৳₹])",
]

SUPPLIER_PATTERNS = [
    r"(?:from|supplier|vendor|issued\s*by|seller)\s*[:\-]\s*(.{3,80})",
]
CUSTOMER_PATTERNS = [
    r"(?:bill\s*to|sold\s*to|customer|invoice\s*to|client)\s*[:\-]?\s*(.{3,80})",
]

# Lines that look like a totals row rather than a billed line.
TOTALS_ROW = re.compile(
    r"^\s*(sub[\s\-]?total|total|tax|vat|gst|balance|amount\s+due|grand\s+total)\b",
    re.IGNORECASE,
)

# A money cell: optional symbol OR three-letter code, then the number.
# The alphabetic-code branch matters for OCR output, where "EUR 12.00" is
# common and a symbol-only pattern silently drops every line item.
_MONEY_CELL = r"(?:[$€£৳₹]|[A-Z]{3})?\s*[\d,]+(?:\.\d{1,2})?"

LINE_ITEM_RE = re.compile(
    rf"^(?P<desc>.{{3,70}}?)\s{{2,}}"
    rf"(?P<qty>\d+(?:\.\d+)?)\s{{2,}}"
    rf"(?P<unit>{_MONEY_CELL})\s{{2,}}"
    rf"(?P<amount>{_MONEY_CELL})\s*$"
)


def _first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def _clean_name(value: str | None) -> str | None:
    if not value:
        return None
    # Names run to the end of the line; cut at a label that starts the next field.
    value = re.split(
        r"\s{3,}|\s+(?:invoice|date|bill\s*to|phone|email|tel)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = " ".join(value.split()).strip(" .,:;-")
    return value or None


def _guess_supplier(text: str) -> str | None:
    """With no explicit label, the supplier is usually the first non-generic
    line of the document - the letterhead."""
    for raw in text.splitlines()[:8]:
        line = " ".join(raw.split()).strip()
        if len(line) < 4 or len(line) > 80:
            continue
        if re.fullmatch(r"(?i)(tax\s+)?invoice|receipt|bill|statement", line):
            continue
        if re.search(r"(?i)\b(invoice|date|page|bill to)\b", line):
            continue
        if not re.search(r"[A-Za-z]{3}", line):
            continue
        return line
    return None


def extract_line_items(text: str) -> list[LineItem]:
    items: list[LineItem] = []
    for raw in text.splitlines():
        if TOTALS_ROW.match(raw):
            continue
        match = LINE_ITEM_RE.match(raw.rstrip())
        if not match:
            continue
        description = " ".join(match.group("desc").split())
        if not re.search(r"[A-Za-z]{3}", description):
            continue
        items.append(
            LineItem(
                description=description,
                quantity=parse_amount(match.group("qty")),
                unit_price=parse_amount(match.group("unit")),
                amount=parse_amount(match.group("amount")),
            )
        )
    return items


def extract(text: str) -> InvoiceExtraction:
    """Best-effort structured extraction with no model call."""
    flat = re.sub(r"[ \t]+", " ", text)

    invoice_number = _first_match(flat, INVOICE_NO_PATTERNS)
    invoice_date = parse_date(_first_match(flat, DATE_PATTERNS))
    supplier = _clean_name(_first_match(flat, SUPPLIER_PATTERNS)) or _guess_supplier(text)
    customer = _clean_name(_first_match(flat, CUSTOMER_PATTERNS))
    currency = _first_match(flat, CURRENCY_PATTERNS)

    amounts: dict[str, float | None] = {}
    for field, patterns in AMOUNT_PATTERNS.items():
        amounts[field] = parse_amount(_first_match(flat, patterns))

    # "Total" patterns also match "Subtotal"; if they came back identical and
    # a distinct subtotal exists, the total match was spurious.
    if (
        amounts.get("total") is not None
        and amounts.get("subtotal") is not None
        and amounts["total"] == amounts["subtotal"]
    ):
        grand = parse_amount(_first_match(flat, [rf"\bgrand\s*total\s*[:\-]?\s*{AMOUNT}"]))
        if grand is not None:
            amounts["total"] = grand

    line_items = extract_line_items(text)

    # Confidence is fixed per field type and deliberately capped below the
    # LLM's range: these are pattern hits, not comprehension.
    confidence: dict[str, float] = {}
    for field, value in [
        ("invoice_number", invoice_number), ("invoice_date", invoice_date),
        ("supplier_name", supplier), ("customer_name", customer),
        ("currency", currency), ("subtotal", amounts.get("subtotal")),
        ("tax", amounts.get("tax")), ("total", amounts.get("total")),
    ]:
        if value is None:
            continue
        # A labelled match is more trustworthy than a positional guess.
        confidence[field] = 0.45 if field == "supplier_name" and not _first_match(
            flat, SUPPLIER_PATTERNS
        ) else 0.65

    return InvoiceExtraction(
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        supplier_name=supplier,
        customer_name=customer,
        currency=currency,
        subtotal=amounts.get("subtotal"),
        tax=amounts.get("tax"),
        total=amounts.get("total"),
        line_items=line_items,
        field_confidence=confidence,
        extraction_notes="Extracted by deterministic rule-based fallback (no LLM).",
    )
