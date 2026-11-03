"""Spreadsheet and CSV ingestion.

Two outputs per file:
  * `ExtractedPage` per sheet - a text rendering used for search and for the
    LLM extractor, so spreadsheets flow through the same path as PDFs.
  * `tables` - structured rows, preserved with their real dtypes. The
    cross-verification engine needs actual numbers, not a text rendering of
    them, so this is kept separately rather than reparsed out of the text.
"""
from __future__ import annotations

import csv
import logging

import chardet
import pandas as pd

from .base import ExtractedPage, IngestionError

log = logging.getLogger(__name__)

# A sheet wider than this is almost certainly a data dump, not an invoice;
# rendering every column to text would blow the LLM context for no benefit.
MAX_TEXT_COLS = 40
MAX_TEXT_ROWS = 500


def _sniff_encoding(path: str, sample_bytes: int = 64_000) -> str:
    with open(path, "rb") as handle:
        raw = handle.read(sample_bytes)
    if not raw:
        return "utf-8"
    guess = chardet.detect(raw)
    encoding = guess.get("encoding") or "utf-8"
    # chardet reports ascii for plain files; utf-8 is a safe superset and
    # avoids failing later on a single accented supplier name.
    return "utf-8" if encoding.lower() == "ascii" else encoding


def _sniff_delimiter(path: str, encoding: str) -> str:
    try:
        with open(path, encoding=encoding, errors="replace") as handle:
            sample = handle.read(16_000)
        if not sample.strip():
            return ","
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except (csv.Error, UnicodeDecodeError):
        # Sniffer throws on single-column files, which are perfectly valid.
        return ","


def _frame_to_text(df: pd.DataFrame, label: str) -> str:
    """Render a dataframe as pipe-delimited text the LLM can read."""
    trimmed = df.iloc[:MAX_TEXT_ROWS, :MAX_TEXT_COLS]
    header = " | ".join(str(c) for c in trimmed.columns)
    lines = [f"[sheet: {label}]", header, "-" * len(header)]
    for row in trimmed.itertuples(index=False, name=None):
        lines.append(" | ".join("" if pd.isna(v) else str(v) for v in row))
    if len(df) > MAX_TEXT_ROWS:
        lines.append(f"... {len(df) - MAX_TEXT_ROWS} more rows truncated for text rendering")
    return "\n".join(lines)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Drop fully empty rows/columns and normalise column names.

    Exported invoice registers routinely carry a block of blank spacer columns;
    leaving them in produces useless 'Unnamed: 7' fields downstream.
    """
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    df.columns = [
        (str(c).strip() if not str(c).startswith("Unnamed:") else f"col_{i}")
        for i, c in enumerate(df.columns)
    ]
    return df.reset_index(drop=True)


def ingest_csv(path: str) -> tuple[list[ExtractedPage], list[dict]]:
    encoding = _sniff_encoding(path)
    delimiter = _sniff_delimiter(path, encoding)
    try:
        df = pd.read_csv(
            path,
            encoding=encoding,
            sep=delimiter,
            engine="python",       # tolerant of ragged rows
            on_bad_lines="warn",   # skip malformed lines instead of aborting
            dtype_backend="numpy_nullable",
        )
    except pd.errors.EmptyDataError as exc:
        raise IngestionError("CSV file is empty", code="csv_empty") from exc
    except Exception as exc:
        raise IngestionError(f"Cannot parse CSV: {exc}", code="csv_corrupt") from exc

    df = _clean(df)
    label = "csv"
    page = ExtractedPage(page_number=1, text=_frame_to_text(df, label), label=label)
    table = {
        "label": label,
        "columns": list(df.columns),
        "rows": df.to_dict(orient="records"),
        "row_count": int(len(df)),
    }
    return [page], [table]


def ingest_excel(path: str) -> tuple[list[ExtractedPage], list[dict]]:
    # openpyxl reads .xlsx/.xlsm; xlrd is required for legacy .xls. pandas
    # picks the engine, but we surface a clear error if the backing lib is
    # missing rather than a bare ImportError.
    try:
        book = pd.read_excel(path, sheet_name=None, dtype_backend="numpy_nullable")
    except ImportError as exc:  # pragma: no cover
        raise IngestionError(
            f"Missing spreadsheet engine ({exc}); install openpyxl and xlrd",
            code="engine_missing",
        ) from exc
    except Exception as exc:
        raise IngestionError(f"Cannot parse spreadsheet: {exc}", code="xlsx_corrupt") from exc

    pages: list[ExtractedPage] = []
    tables: list[dict] = []
    for index, (sheet_name, df) in enumerate(book.items(), start=1):
        df = _clean(df)
        if df.empty:
            # Keep the page so page numbering matches the workbook, but mark it.
            pages.append(
                ExtractedPage(
                    page_number=index,
                    text=f"[sheet: {sheet_name}] (empty)",
                    label=str(sheet_name),
                )
            )
            continue
        pages.append(
            ExtractedPage(
                page_number=index,
                text=_frame_to_text(df, str(sheet_name)),
                label=str(sheet_name),
            )
        )
        tables.append(
            {
                "label": str(sheet_name),
                "columns": list(df.columns),
                "rows": df.to_dict(orient="records"),
                "row_count": int(len(df)),
            }
        )

    if not pages:
        raise IngestionError("Workbook contains no sheets", code="xlsx_empty")
    return pages, tables
