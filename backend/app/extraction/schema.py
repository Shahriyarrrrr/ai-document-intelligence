"""The extraction contract.

Every field is Optional by design. The brief says the system must "handle
missing or uncertain information without inventing values", and the single
most effective way to enforce that is to make null a first-class, valid
answer at the type level. A model that cannot return null will guess.

Validators here normalise rather than reject: an LLM will return "1,234.56",
"$1234.56" and "1234,56" for the same number, and a parse failure that throws
away the whole invoice is worse than one coerced field.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Tokens an LLM emits when it means "not present". Treated as null.
NULL_TOKENS = {
    "", "n/a", "na", "null", "none", "not found", "not present", "unknown",
    "not specified", "not available", "-", "--", "—",
}

CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "৳": "BDT", "tk": "BDT", "bdt": "BDT",
    "₹": "INR", "inr": "INR",
    "¥": "JPY", "jpy": "JPY",
}

DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
    "%Y/%m/%d", "%d.%m.%Y", "%Y%m%d",
]


def _is_null(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in NULL_TOKENS)


def parse_amount(value: Any) -> float | None:
    """Parse a money string into a float, or None.

    Handles symbols, thousands separators in both conventions, parenthesised
    negatives, and trailing credit markers.
    """
    if _is_null(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.upper().endswith("CR"):
        negative, text = True, text[:-2]
    if text.startswith("-"):
        negative, text = True, text[1:]

    # Strip everything that is not a digit or a separator.
    text = re.sub(r"[^\d.,]", "", text)
    if not text:
        return None

    if "," in text and "." in text:
        # Whichever separator appears last is the decimal point.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        tail = text.split(",")[-1]
        # "1,234" is thousands; "1234,56" is a decimal comma.
        text = text.replace(",", "") if len(tail) == 3 else text.replace(",", ".")

    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def parse_date(value: Any) -> str | None:
    """Return an ISO-8601 date string, or None. Never guesses a year."""
    if _is_null(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()

    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    # Last resort: a loose YYYY-MM-DD anywhere in the string.
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        try:
            y, m, d = (int(g) for g in match.groups())
            return date(y, m, d).isoformat()
        except ValueError:
            return None
    return None


def normalise_currency(value: Any) -> str | None:
    if _is_null(value):
        return None
    text = str(value).strip()
    if re.fullmatch(r"[A-Za-z]{3}", text):
        return text.upper()
    for token, code in CURRENCY_SYMBOLS.items():
        if token in text.lower() or token in text:
            return code
    return None


class LineItem(BaseModel):
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, v: Any) -> str | None:
        if _is_null(v):
            return None
        return " ".join(str(v).split())

    @field_validator("quantity", "unit_price", "amount", mode="before")
    @classmethod
    def _amounts(cls, v: Any) -> float | None:
        return parse_amount(v)

    @model_validator(mode="after")
    def _derive_amount(self) -> LineItem:
        # Only fill a gap that is arithmetically determined - this is
        # completion, not invention.
        if self.amount is None and self.quantity is not None and self.unit_price is not None:
            self.amount = round(self.quantity * self.unit_price, 2)
        return self


class InvoiceExtraction(BaseModel):
    """The nine required fields plus line items."""

    invoice_number: str | None = None
    invoice_date: str | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    currency: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)

    # 0-1 per field, keyed by field name. Absent means "not reported".
    field_confidence: dict[str, float] = Field(default_factory=dict)
    # Free-text note from the model about anything ambiguous.
    extraction_notes: str | None = None

    @field_validator("invoice_number", "supplier_name", "customer_name",
                     "extraction_notes", mode="before")
    @classmethod
    def _clean_text(cls, v: Any) -> str | None:
        if _is_null(v):
            return None
        return " ".join(str(v).split()) or None

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def _clean_amounts(cls, v: Any) -> float | None:
        return parse_amount(v)

    @field_validator("invoice_date", mode="before")
    @classmethod
    def _clean_date(cls, v: Any) -> str | None:
        return parse_date(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _clean_currency(cls, v: Any) -> str | None:
        return normalise_currency(v)

    @field_validator("field_confidence", mode="before")
    @classmethod
    def _clean_confidence(cls, v: Any) -> dict[str, float]:
        if not isinstance(v, dict):
            return {}
        out: dict[str, float] = {}
        for key, raw in v.items():
            try:
                score = float(raw)
            except (TypeError, ValueError):
                continue
            # Some models report 0-100 instead of 0-1.
            if score > 1.0:
                score = score / 100.0
            out[str(key)] = max(0.0, min(1.0, score))
        return out

    @property
    def populated_fields(self) -> int:
        return sum(
            1 for f in ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                        "subtotal", "tax", "total", "currency")
            if getattr(self, f) is not None
        )


# JSON Schema handed to the LLM. Generated from the model so the prompt and
# the parser can never drift apart.
def json_schema() -> dict:
    schema = InvoiceExtraction.model_json_schema()
    schema.pop("$defs", None)
    return schema
