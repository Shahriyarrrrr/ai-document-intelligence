"""Throughput and memory benchmark for very large PDFs.

The scenario specifies PDFs of roughly 1000 pages. The thing that breaks at
that size is not accuracy, it is memory: the naive implementation renders
every page to an image up front and dies. This benchmark exists to prove the
streaming page-at-a-time reader holds flat memory, and to produce a real
pages/second figure for the evaluation report.

Usage:
    python eval/bench_large_pdf.py [--pages 1000]
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf  # noqa: E402

from app.ingestion.pdf_reader import iter_pdf_pages  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")


def peak_rss_mb() -> float:
    # ru_maxrss is KB on Linux, bytes on macOS.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024 if sys.platform != "darwin" else value / (1024 * 1024)


def build_large_pdf(path: str, pages: int) -> float:
    """A digital PDF with invoice-like text on every page."""
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((60, 70), "INVOICE", fontsize=18)
        page.insert_text((60, 110), f"Supplier {n % 50}, Trade Park", fontsize=10)
        page.insert_text((60, 130), f"Invoice Number: INV-BULK-{n:06d}", fontsize=10)
        page.insert_text((300, 130), "Invoice Date: 2026-04-11", fontsize=10)
        page.insert_text((60, 150), "Bill To: Cottage Home Care Services", fontsize=10)
        page.insert_text((60, 190), "Consumables, carton   4   $25.00   $100.00", fontsize=9)
        page.insert_text((60, 210), "Subtotal:   $100.00", fontsize=9)
        page.insert_text((60, 226), "Tax:   $20.00", fontsize=9)
        page.insert_text((60, 242), "TOTAL:   $120.00", fontsize=9)
    start = time.perf_counter()
    doc.save(path, deflate=True)
    doc.close()
    return time.perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=1000)
    parser.add_argument("--keep", action="store_true", help="keep the generated PDF")
    args = parser.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"_bulk_{args.pages}p.pdf")

    baseline_rss = peak_rss_mb()
    print(f"Building a {args.pages}-page PDF...", file=sys.stderr)
    build_s = build_large_pdf(path, args.pages)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    print(f"  built in {build_s:.1f}s, {size_mb:.1f} MB", file=sys.stderr)

    print("Streaming pages...", file=sys.stderr)
    start = time.perf_counter()
    page_count = 0
    chars = 0
    first_page_s = None
    for page in iter_pdf_pages(path, ocr=True):
        if first_page_s is None:
            first_page_s = time.perf_counter() - start
        page_count += 1
        chars += len(page.text)
        if page_count % 250 == 0:
            print(f"  {page_count} pages, rss {peak_rss_mb():.0f} MB", file=sys.stderr)
    elapsed = time.perf_counter() - start

    result = {
        "pages": page_count,
        "file_size_mb": round(size_mb, 2),
        "elapsed_s": round(elapsed, 2),
        "pages_per_second": round(page_count / elapsed, 1) if elapsed else None,
        "time_to_first_page_s": round(first_page_s or 0, 4),
        "chars_extracted": chars,
        "baseline_rss_mb": round(baseline_rss, 1),
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "rss_growth_mb": round(peak_rss_mb() - baseline_rss, 1),
        "note": (
            "Pages are opened and released one at a time, so peak RSS is "
            "independent of page count. Figures are for a digital text layer; "
            "scanned pages are dominated by OCR at roughly 1-3 s/page."
        ),
    }

    if not args.keep:
        os.remove(path)

    with open(os.path.join(RESULTS, "large_pdf_benchmark.json"), "w") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
