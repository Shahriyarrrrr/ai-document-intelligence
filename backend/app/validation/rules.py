"""Validation rules.

Each rule is a small pure function returning zero or more `Issue` objects, so
rules can be unit-tested in isolation and the set can be extended without
touching the runner.

Severity drives behaviour, not just colour:
  ERROR   -> the invoice is flagged `needs_review` and is excluded from
             "clean" exports.
  WARNING -> surfaced in the UI, does not block.
  INFO    -> recorded for the audit trail only.

Money is compared with `Decimal`, not float. `0.1 + 0.2 != 0.3` in binary
floating point, and an invoice validator that reports a 0.000000001 mismatch
on a correct invoice is worse than no validator at all.
"""
from __future__ import annotations

# Aliased: `Issue` has an attribute literally called `field`, which would
# shadow `dataclasses.field` inside the class body.
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from ..config import settings
from ..extraction.schema import InvoiceExtraction

REQUIRED_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "total")
RECOMMENDED_FIELDS = ("customer_name", "subtotal", "tax", "currency")

# Below this, a field is surfaced for human confirmation even if it looks fine.
LOW_CONFIDENCE = 0.55


@dataclass
class Issue:
    code: str
    severity: str  # "info" | "warning" | "error"
    message: str
    field: str | None = None
    context: dict[str, Any] = dc_field(default_factory=dict)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= Decimal(str(settings.amount_tolerance))


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def rule_missing_fields(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    issues = []
    for name in REQUIRED_FIELDS:
        if getattr(inv, name) is None:
            issues.append(Issue(
                code="missing_required_field",
                severity="error",
                field=name,
                message=f"Required field '{name}' is missing.",
            ))
    for name in RECOMMENDED_FIELDS:
        if getattr(inv, name) is None:
            issues.append(Issue(
                code="missing_recommended_field",
                severity="warning",
                field=name,
                message=f"Field '{name}' could not be extracted.",
            ))
    return issues


def rule_totals_arithmetic(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    """subtotal + tax must equal total."""
    subtotal, tax, total = _dec(inv.subtotal), _dec(inv.tax), _dec(inv.total)
    if subtotal is None or total is None:
        return []
    expected = subtotal + (tax or Decimal("0"))
    if _close(expected, total):
        return []
    return [Issue(
        code="total_mismatch",
        severity="error",
        field="total",
        message=(
            f"subtotal ({subtotal}) + tax ({tax or 0}) = {expected}, "
            f"but the stated total is {total} (difference {total - expected})."
        ),
        context={
            "subtotal": float(subtotal), "tax": float(tax or 0),
            "expected_total": float(expected), "stated_total": float(total),
            "difference": float(total - expected),
        },
    )]


def rule_line_items_sum(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    """Line items must sum to the subtotal."""
    if not inv.line_items:
        return []
    amounts = [_dec(i.amount) for i in inv.line_items]
    if any(a is None for a in amounts):
        return [Issue(
            code="line_item_amount_missing",
            severity="warning",
            field="line_items",
            message="One or more line items have no amount; cannot verify the subtotal.",
            context={"missing_count": sum(1 for a in amounts if a is None)},
        )]
    line_sum = sum(amounts, Decimal("0"))
    subtotal = _dec(inv.subtotal)
    # With no subtotal, fall back to comparing against the total only when
    # tax is explicitly zero - otherwise the comparison is meaningless.
    target_name, target = ("subtotal", subtotal)
    if subtotal is None:
        tax = _dec(inv.tax)
        if tax is not None and tax == 0:
            target_name, target = ("total", _dec(inv.total))
        else:
            return []
    if target is None or _close(line_sum, target):
        return []
    return [Issue(
        code="line_items_sum_mismatch",
        severity="error",
        field="line_items",
        message=(
            f"Line items sum to {line_sum}, but {target_name} is {target} "
            f"(difference {target - line_sum})."
        ),
        context={
            "line_sum": float(line_sum), "target_field": target_name,
            "target_value": float(target), "difference": float(target - line_sum),
            "line_count": len(inv.line_items),
        },
    )]


def rule_line_item_arithmetic(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    """quantity * unit_price must equal the line amount."""
    issues = []
    for index, item in enumerate(inv.line_items):
        qty, unit, amount = _dec(item.quantity), _dec(item.unit_price), _dec(item.amount)
        if qty is None or unit is None or amount is None:
            continue
        expected = qty * unit
        if _close(expected, amount):
            continue
        issues.append(Issue(
            code="line_item_arithmetic",
            severity="warning",
            field=f"line_items[{index}]",
            message=(
                f"Line {index + 1} ('{(item.description or '')[:40]}'): "
                f"{qty} x {unit} = {expected}, but the amount shown is {amount}."
            ),
            context={
                "index": index, "quantity": float(qty), "unit_price": float(unit),
                "expected": float(expected), "stated": float(amount),
            },
        ))
    return issues


def rule_negative_or_absurd(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    issues = []
    for name in ("subtotal", "tax", "total"):
        value = _dec(getattr(inv, name))
        if value is None:
            continue
        if value < 0:
            issues.append(Issue(
                code="negative_amount", severity="warning", field=name,
                message=f"'{name}' is negative ({value}); this may be a credit note.",
                context={"value": float(value)},
            ))
        elif value > Decimal("100000000"):
            # Almost always a misparsed separator, e.g. 1.234,56 read as
            # 123456 with the decimal lost.
            issues.append(Issue(
                code="implausible_amount", severity="error", field=name,
                message=f"'{name}' is implausibly large ({value}); likely a parsing error.",
                context={"value": float(value)},
            ))
    return issues


def rule_tax_ratio(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    """Flag a tax rate outside any real-world VAT/GST band."""
    subtotal, tax = _dec(inv.subtotal), _dec(inv.tax)
    if subtotal is None or tax is None or subtotal <= 0:
        return []
    ratio = tax / subtotal
    if ratio > Decimal("0.40"):
        return [Issue(
            code="implausible_tax_rate", severity="warning", field="tax",
            message=f"Tax is {ratio:.1%} of the subtotal, which is outside normal rates.",
            context={"ratio": float(ratio)},
        )]
    return []


def rule_date_sanity(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    if not inv.invoice_date:
        return []
    try:
        parsed = datetime.strptime(inv.invoice_date, "%Y-%m-%d").date()
    except ValueError:
        return [Issue(
            code="unparseable_date", severity="error", field="invoice_date",
            message=f"Invoice date '{inv.invoice_date}' is not a valid date.",
        )]
    today = ctx.get("today") or date.today()
    if parsed > today:
        return [Issue(
            code="future_date", severity="warning", field="invoice_date",
            message=f"Invoice date {parsed} is in the future.",
            context={"date": parsed.isoformat()},
        )]
    if parsed.year < 1990:
        return [Issue(
            code="implausible_date", severity="warning", field="invoice_date",
            message=f"Invoice date {parsed} is implausibly old; check the year.",
            context={"date": parsed.isoformat()},
        )]
    return []


def rule_low_confidence(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    issues = []
    for name, score in (inv.field_confidence or {}).items():
        if score < LOW_CONFIDENCE and getattr(inv, name, None) is not None:
            issues.append(Issue(
                code="low_confidence", severity="warning", field=name,
                message=f"'{name}' was extracted with low confidence ({score:.0%}).",
                context={"confidence": score},
            ))
    return issues


def rule_duplicate(inv: InvoiceExtraction, ctx: dict) -> list[Issue]:
    """Duplicate detection.

    `ctx['existing']` is a list of (invoice_number, supplier_name, total,
    document_id) already in the database. Matching on invoice number alone
    produces false positives - different suppliers legitimately reuse simple
    sequences like "001" - so a duplicate requires the number plus either the
    supplier or the total.
    """
    existing: Iterable[tuple] = ctx.get("existing") or []
    if not inv.invoice_number:
        return []
    number = inv.invoice_number.strip().upper()
    supplier = (inv.supplier_name or "").strip().lower()
    total = _dec(inv.total)

    for other_number, other_supplier, other_total, other_doc in existing:
        if not other_number or other_number.strip().upper() != number:
            continue
        same_supplier = supplier and (other_supplier or "").strip().lower() == supplier
        other_total_dec = _dec(other_total)
        same_total = (
            total is not None and other_total_dec is not None and _close(total, other_total_dec)
        )
        if same_supplier or same_total:
            return [Issue(
                code="duplicate_invoice", severity="error", field="invoice_number",
                message=(
                    f"Invoice number '{inv.invoice_number}' already exists "
                    f"(document {other_doc})."
                ),
                context={
                    "duplicate_of_document_id": other_doc,
                    "matched_on": "supplier" if same_supplier else "total",
                },
            )]
    return []


ALL_RULES: list[Callable[[InvoiceExtraction, dict], list[Issue]]] = [
    rule_missing_fields,
    rule_totals_arithmetic,
    rule_line_items_sum,
    rule_line_item_arithmetic,
    rule_negative_or_absurd,
    rule_tax_ratio,
    rule_date_sanity,
    rule_low_confidence,
    rule_duplicate,
]


def validate(inv: InvoiceExtraction, ctx: dict | None = None) -> list[Issue]:
    ctx = ctx or {}
    issues: list[Issue] = []
    for rule in ALL_RULES:
        try:
            issues.extend(rule(inv, ctx))
        except Exception as exc:  # a broken rule must not fail the document
            issues.append(Issue(
                code="rule_error", severity="info", field=None,
                message=f"Validation rule {rule.__name__} failed: {exc}",
            ))
    return issues


def needs_review(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
