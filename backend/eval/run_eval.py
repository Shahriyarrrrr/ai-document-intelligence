"""Evaluation harness.

Runs the real pipeline over the labelled dataset and measures what actually
happened. Every number in the evaluation report comes from here; none are
estimated or carried over from a previous run.

Runs the ingestion + extraction + validation path directly rather than through
the API, so results are reproducible with no database, broker or network.

Usage:
    python eval/run_eval.py                 # rule-based extractor (offline)
    LLM_PROVIDER=openai LLM_API_KEY=sk-... python eval/run_eval.py
    python eval/run_eval.py --compare       # run both and tabulate the delta

Outputs eval/results/metrics.json and eval/results/REPORT.md.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.extraction import fallback  # noqa: E402
from app.extraction.llm import build_client, extract_invoice  # noqa: E402
from app.ingestion.base import IngestionError  # noqa: E402
from app.ingestion.router import ingest  # noqa: E402
from app.validation.rules import needs_review, validate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(HERE, "dataset")
FILES = os.path.join(DATASET, "files")
CORRUPT = os.path.join(DATASET, "corrupt")
RESULTS = os.path.join(HERE, "results")

SCORED_FIELDS = [
    "invoice_number", "invoice_date", "supplier_name",
    "customer_name", "currency", "subtotal", "tax", "total",
]
# Money is compared to the cent; a 0.01 difference is a real mismatch here
# because the ground truth is exact.
AMOUNT_FIELDS = {"subtotal", "tax", "total"}
# Names are compared case- and punctuation-insensitively: "Acme Ltd." and
# "Acme Ltd" are the same supplier, and counting that as an error would
# understate accuracy for reasons that do not matter operationally.
NAME_FIELDS = {"supplier_name", "customer_name"}


def _normalise_name(value) -> str:
    if value is None:
        return ""
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def field_matches(field: str, predicted, expected) -> bool:
    if expected is None:
        return predicted is None
    if predicted is None:
        return False
    if field in AMOUNT_FIELDS:
        try:
            return abs(float(predicted) - float(expected)) < 0.005
        except (TypeError, ValueError):
            return False
    if field in NAME_FIELDS:
        return _normalise_name(predicted) == _normalise_name(expected)
    return str(predicted).strip() == str(expected).strip()


def evaluate_file(
    path: str, truth: dict, use_llm: bool, client, seen: list | None = None
) -> dict:
    """Process one file and score it. Never raises.

    `seen` accumulates (number, supplier, total, file) for invoices already
    processed in this run, so duplicate detection is actually exercised.
    Validating each file against an empty index would make the duplicate rule
    structurally unable to fire, and the report would then claim a detection
    miss that the rule never had a chance at.
    """
    name = os.path.basename(path)
    record: dict = {
        "file": name,
        "variant": truth.get("_variant", "unknown"),
        "ok": False,
        "fields": {},
        "timings": {},
    }

    start = time.perf_counter()
    try:
        result = ingest(path)
    except IngestionError as exc:
        record.update(
            ok=False, failure_stage="ingest", failure_code=exc.code, error=str(exc),
            timings={"total_s": round(time.perf_counter() - start, 3)},
        )
        return record
    except Exception as exc:
        record.update(
            ok=False, failure_stage="ingest", failure_code="unexpected",
            error=f"{type(exc).__name__}: {exc}",
            timings={"total_s": round(time.perf_counter() - start, 3)},
        )
        return record

    ingest_s = time.perf_counter() - start
    record["pages"] = result.page_count
    record["ocr_pages"] = result.ocr_page_count
    record["source_kind"] = result.source_kind
    ocr_pages = [p for p in result.pages if p.ocr_confidence is not None]
    record["mean_ocr_confidence"] = (
        round(sum(p.ocr_confidence for p in ocr_pages) / len(ocr_pages), 1)
        if ocr_pages else None
    )

    # Spreadsheet registers hold many invoices and are scored separately by
    # the cross-check section, not field-by-field here.
    if truth.get("_invoices"):
        record.update(ok=True, skipped_scoring="multi_invoice_register",
                      timings={"ingest_s": round(ingest_s, 3)})
        return record

    extract_start = time.perf_counter()
    if use_llm:
        extraction, meta = extract_invoice(
            text=result.full_text, filename=name,
            source_kind=result.source_kind,
            ocr_confidence=record["mean_ocr_confidence"], client=client,
        )
        method = meta.get("method", "llm")
    else:
        extraction = fallback.extract(result.full_text)
        method = "rules"
    extract_s = time.perf_counter() - extract_start

    validate_start = time.perf_counter()
    issues = validate(extraction, {"existing": list(seen or [])})
    validate_s = time.perf_counter() - validate_start

    if seen is not None and extraction.invoice_number:
        seen.append((
            extraction.invoice_number, extraction.supplier_name,
            extraction.total, name,
        ))

    for field in SCORED_FIELDS:
        predicted = getattr(extraction, field)
        expected = truth.get(field)
        record["fields"][field] = {
            "predicted": predicted,
            "expected": expected,
            "correct": field_matches(field, predicted, expected),
            "extracted": predicted is not None,
        }

    expected_items = truth.get("line_items") or []
    record["line_items"] = {
        "expected": len(expected_items),
        "extracted": len(extraction.line_items),
        "count_correct": len(expected_items) == len(extraction.line_items),
    }

    record.update(
        ok=True,
        method=method,
        needs_review=needs_review(issues),
        issue_codes=sorted({i.code for i in issues}),
        timings={
            "ingest_s": round(ingest_s, 3),
            "extract_s": round(extract_s, 3),
            "validate_s": round(validate_s, 3),
            "total_s": round(time.perf_counter() - start, 3),
        },
    )

    expected_issue = truth.get("_expect_issue")
    if expected_issue:
        record["expected_issue"] = expected_issue
        record["expected_issue_detected"] = expected_issue in record["issue_codes"]
    return record


def evaluate_corrupt() -> dict:
    """Corrupt files must fail, with a specific code. Silent success is the
    failure mode being tested for."""
    rows = []
    for name in sorted(os.listdir(CORRUPT)):
        path = os.path.join(CORRUPT, name)
        try:
            ingest(path)
            rows.append({"file": name, "rejected": False, "code": None})
        except IngestionError as exc:
            rows.append({"file": name, "rejected": True, "code": exc.code})
        except Exception as exc:
            rows.append({
                "file": name, "rejected": True, "code": f"uncaught:{type(exc).__name__}"
            })
    rejected = sum(1 for r in rows if r["rejected"])
    return {
        "total": len(rows),
        "correctly_rejected": rejected,
        "rate": round(rejected / len(rows), 4) if rows else 0.0,
        "rows": rows,
    }


def evaluate_crosscheck(truth: dict) -> dict:
    """Measure PDF<->Excel reconciliation on the files built to match."""
    from app.validation import crosscheck as cc

    register = "register_multi_sheet.xlsx"
    if register not in truth:
        return {"skipped": "no register in dataset"}

    right: list = []
    result = ingest(os.path.join(FILES, register))
    for table in result.tables:
        right.extend(cc.records_from_table(table, "xlsx", register))

    left: list = []
    pdf_names = [n for n in sorted(os.listdir(FILES)) if n.startswith("crosscheck_")]
    for name in pdf_names:
        text = ingest(os.path.join(FILES, name)).full_text
        left.append(cc.record_from_invoice(fallback.extract(text), name, name))

    report = cc.cross_check(left, right)
    counts = report.counts()
    return {
        "pdf_count": len(left),
        "register_rows": len(right),
        "counts": counts,
        "expected_matches": len(pdf_names),
        "precision_note": (
            "All PDFs in this set have a corresponding register row, so a "
            "correct run yields match == pdf_count with zero mismatches."
        ),
    }


def aggregate(records: list[dict]) -> dict:
    scored = [r for r in records if r.get("ok") and r.get("fields")]
    failed = [r for r in records if not r.get("ok")]

    per_field: dict[str, dict] = {}
    for field in SCORED_FIELDS:
        applicable = [r for r in scored if field in r["fields"]]
        correct = sum(1 for r in applicable if r["fields"][field]["correct"])
        extracted = sum(1 for r in applicable if r["fields"][field]["extracted"])
        per_field[field] = {
            "n": len(applicable),
            "correct": correct,
            "accuracy": round(correct / len(applicable), 4) if applicable else None,
            "extraction_rate": round(extracted / len(applicable), 4) if applicable else None,
        }

    by_variant: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "field_correct": 0, "field_total": 0, "times": []}
    )
    for record in scored:
        family = record["variant"].split("_")[0]
        bucket = by_variant[family]
        bucket["n"] += 1
        bucket["field_correct"] += sum(
            1 for f in record["fields"].values() if f["correct"]
        )
        bucket["field_total"] += len(record["fields"])
        bucket["times"].append(record["timings"]["total_s"])

    variant_summary = {}
    for family, bucket in by_variant.items():
        variant_summary[family] = {
            "documents": bucket["n"],
            "field_accuracy": round(bucket["field_correct"] / bucket["field_total"], 4)
            if bucket["field_total"] else None,
            "mean_s": round(statistics.mean(bucket["times"]), 3) if bucket["times"] else None,
            "p95_s": round(
                sorted(bucket["times"])[int(len(bucket["times"]) * 0.95) - 1], 3
            ) if len(bucket["times"]) >= 2 else None,
        }

    total_fields = sum(len(r["fields"]) for r in scored)
    total_correct = sum(
        1 for r in scored for f in r["fields"].values() if f["correct"]
    )
    all_times = [r["timings"]["total_s"] for r in records if r.get("timings", {}).get("total_s")]
    perfect = sum(
        1 for r in scored if all(f["correct"] for f in r["fields"].values())
    )
    line_item_scored = [r for r in scored if "line_items" in r]

    expected_issue_rows = [r for r in scored if "expected_issue" in r]

    return {
        "documents": {
            "total": len(records),
            "processed": len(records) - len(failed),
            "failed": len(failed),
            "success_rate": round((len(records) - len(failed)) / len(records), 4)
            if records else None,
            "scored": len(scored),
        },
        "overall": {
            "field_accuracy": round(total_correct / total_fields, 4) if total_fields else None,
            "fields_compared": total_fields,
            "fields_correct": total_correct,
            "documents_fully_correct": perfect,
            "document_exact_match_rate": round(perfect / len(scored), 4) if scored else None,
            "line_item_count_accuracy": round(
                sum(1 for r in line_item_scored if r["line_items"]["count_correct"])
                / len(line_item_scored), 4
            ) if line_item_scored else None,
        },
        "per_field": per_field,
        "by_variant": variant_summary,
        "timing": {
            "mean_s": round(statistics.mean(all_times), 3) if all_times else None,
            "median_s": round(statistics.median(all_times), 3) if all_times else None,
            "min_s": round(min(all_times), 3) if all_times else None,
            "max_s": round(max(all_times), 3) if all_times else None,
            "total_s": round(sum(all_times), 2) if all_times else None,
        },
        "validation": {
            "flagged_for_review": sum(1 for r in scored if r.get("needs_review")),
            "seeded_issues_detected": sum(
                1 for r in expected_issue_rows if r.get("expected_issue_detected")
            ),
            "seeded_issues_total": len(expected_issue_rows),
        },
        "failures": [
            {"file": r["file"], "stage": r.get("failure_stage"),
             "code": r.get("failure_code"), "error": r.get("error")}
            for r in failed
        ],
    }


def run(use_llm: bool) -> dict:
    with open(os.path.join(DATASET, "ground_truth.json")) as handle:
        truth = json.load(handle)

    client = build_client() if use_llm else None
    if use_llm and client is None:
        print("! LLM requested but no client could be built; "
              "results will reflect the rule-based fallback.", file=sys.stderr)

    records = []
    seen: list = []
    # Sorted so "digital_INV-2026-00001" is processed before
    # "duplicate_INV-2026-00001" - the duplicate rule needs the original to
    # already be in the index, exactly as it would be in production.
    for index, (name, expected) in enumerate(sorted(truth.items()), start=1):
        path = os.path.join(FILES, name)
        if not os.path.exists(path):
            continue
        print(f"  [{index:2d}/{len(truth)}] {name}", file=sys.stderr)
        records.append(evaluate_file(path, expected, use_llm, client, seen))

    summary = aggregate(records)
    summary["corrupt_handling"] = evaluate_corrupt()
    summary["crosscheck"] = evaluate_crosscheck(truth)
    summary["mode"] = "llm" if (use_llm and client) else "rule_based"
    summary["methods_used"] = sorted({
        r.get("method") for r in records if r.get("method")
    })
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    return {"summary": summary, "records": records}


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def write_report(payload: dict, comparison: dict | None = None) -> str:
    s = payload["summary"]
    lines: list[str] = []
    add = lines.append

    add("# Evaluation Report")
    add("")
    add(f"Generated: `{s['generated_at']}`  ")
    add(f"Extraction mode: **{s['mode']}**  ")
    add(f"Methods observed: `{', '.join(s['methods_used']) or 'n/a'}`")
    add("")
    add("All figures below are measured by `eval/run_eval.py` against the "
        "labelled dataset in `eval/dataset/`. Nothing here is estimated.")
    add("")

    add("## Headline")
    add("")
    d, o, t = s["documents"], s["overall"], s["timing"]
    add("| Metric | Value |")
    add("| --- | --- |")
    add(f"| Documents processed | {d['processed']} / {d['total']} |")
    add(f"| Document success rate | {_pct(d['success_rate'])} |")
    add(f"| Field-level accuracy | {_pct(o['field_accuracy'])} "
        f"({o['fields_correct']}/{o['fields_compared']}) |")
    add(f"| Documents with every field correct | {o['documents_fully_correct']}"
        f"/{d['scored']} ({_pct(o['document_exact_match_rate'])}) |")
    add(f"| Line-item count accuracy | {_pct(o['line_item_count_accuracy'])} |")
    add(f"| Mean time per document | {t['mean_s']}s |")
    add(f"| Median / max | {t['median_s']}s / {t['max_s']}s |")
    add("")

    add("## Accuracy by field")
    add("")
    add("| Field | N | Correct | Accuracy | Extraction rate |")
    add("| --- | ---: | ---: | ---: | ---: |")
    for field, stats in s["per_field"].items():
        add(f"| `{field}` | {stats['n']} | {stats['correct']} | "
            f"{_pct(stats['accuracy'])} | {_pct(stats['extraction_rate'])} |")
    add("")
    add("*Extraction rate* is how often a value was produced at all; "
        "*accuracy* is how often it was right. A field can be extracted "
        "often and still be wrong, and the two columns separate those.")
    add("")

    add("## Accuracy by document type")
    add("")
    add("| Type | Docs | Field accuracy | Mean time | p95 |")
    add("| --- | ---: | ---: | ---: | ---: |")
    for family, stats in sorted(s["by_variant"].items()):
        add(f"| {family} | {stats['documents']} | {_pct(stats['field_accuracy'])} | "
            f"{stats['mean_s']}s | {stats['p95_s'] or '—'}s |")
    add("")

    add("## Failure handling")
    add("")
    c = s["corrupt_handling"]
    add(f"Deliberately corrupt/unsupported files correctly rejected: "
        f"**{c['correctly_rejected']}/{c['total']}** ({_pct(c['rate'])})")
    add("")
    add("| File | Rejected | Error code |")
    add("| --- | --- | --- |")
    for row in c["rows"]:
        add(f"| `{row['file']}` | {'yes' if row['rejected'] else 'NO'} | "
            f"`{row['code'] or '-'}` |")
    add("")
    if s["failures"]:
        add("Documents that failed during the run:")
        add("")
        for failure in s["failures"]:
            add(f"- `{failure['file']}` — {failure['stage']}: "
                f"`{failure['code']}` — {failure['error']}")
        add("")

    add("## Validation")
    add("")
    v = s["validation"]
    add(f"- Documents flagged for manual review: **{v['flagged_for_review']}**")
    add(f"- Seeded defects detected: **{v['seeded_issues_detected']}/"
        f"{v['seeded_issues_total']}** "
        "(the dataset contains a deliberately inconsistent invoice and a "
        "deliberate duplicate)")
    add("")

    add("## Cross-verification (PDF ↔ Excel)")
    add("")
    cross = s["crosscheck"]
    if "skipped" in cross:
        add(f"Skipped: {cross['skipped']}")
    else:
        add(f"- PDFs compared: **{cross['pdf_count']}**")
        add(f"- Register rows: **{cross['register_rows']}**")
        add(f"- Result: `{json.dumps(cross['counts'])}`")
        add(f"- Expected matches: **{cross['expected_matches']}**")
        add("")
        add(cross["precision_note"])
    add("")

    if comparison:
        add("## Rule-based baseline vs LLM")
        add("")
        add("| Metric | Rule-based | LLM | Delta |")
        add("| --- | ---: | ---: | ---: |")
        for label, key in [
            ("Field accuracy", "field_accuracy"),
            ("Document exact match", "document_exact_match_rate"),
            ("Line-item count accuracy", "line_item_count_accuracy"),
        ]:
            base = comparison["rule_based"]["overall"][key]
            llm_value = comparison["llm"]["overall"][key]
            delta = (
                f"{(llm_value - base) * 100:+.1f} pp"
                if base is not None and llm_value is not None else "—"
            )
            add(f"| {label} | {_pct(base)} | {_pct(llm_value)} | {delta} |")
        add("")
        base_t = comparison["rule_based"]["timing"]["mean_s"]
        llm_t = comparison["llm"]["timing"]["mean_s"]
        add(f"| Mean time/doc | {base_t}s | {llm_t}s | "
            f"{llm_t - base_t:+.2f}s |")
        add("")

    add("## How to reproduce")
    add("")
    add("```bash")
    add("cd backend")
    add("python eval/make_dataset.py      # regenerate the labelled dataset")
    add("python eval/run_eval.py          # rule-based baseline, no network")
    add("LLM_PROVIDER=openai LLM_API_KEY=sk-... python eval/run_eval.py")
    add("python eval/run_eval.py --compare   # both, with the delta table")
    add("```")
    add("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", action="store_true",
                        help="run rule-based and LLM modes and tabulate both")
    parser.add_argument("--llm", action="store_true",
                        help="force the LLM path")
    args = parser.parse_args()

    os.makedirs(RESULTS, exist_ok=True)

    if not os.path.exists(os.path.join(DATASET, "ground_truth.json")):
        print("Dataset missing; generating...", file=sys.stderr)
        from make_dataset import build

        build()

    comparison = None
    if args.compare:
        print("== rule-based baseline ==", file=sys.stderr)
        base = run(use_llm=False)
        print("== llm ==", file=sys.stderr)
        llm_run = run(use_llm=True)
        comparison = {
            "rule_based": base["summary"],
            "llm": llm_run["summary"],
        }
        payload = llm_run
    else:
        use_llm = args.llm or bool(os.getenv("LLM_API_KEY"))
        payload = run(use_llm=use_llm)

    with open(os.path.join(RESULTS, "metrics.json"), "w") as handle:
        json.dump(
            {"summary": payload["summary"], "records": payload["records"],
             "comparison": comparison},
            handle, indent=2, default=str,
        )

    report = write_report(payload, comparison)
    with open(os.path.join(RESULTS, "REPORT.md"), "w") as handle:
        handle.write(report)

    print(report)


if __name__ == "__main__":
    main()
