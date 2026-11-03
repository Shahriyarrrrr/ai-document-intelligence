"""Layout-preserving text reconstruction.

Both PyMuPDF's text mode and Tesseract's word output destroy the one thing an
invoice parser needs most: column structure. PyMuPDF emits each table cell on
its own line; Tesseract hands back a flat token stream. In both cases
"Description  Qty  Unit Price  Amount" becomes four separate lines, and any
attempt to read a line-item table afterwards is guesswork.

This module rebuilds rows from positioned words, for both sources, using the
same algorithm:

  1. Group words into rows by vertical overlap, not exact y equality - glyphs
     on one visual line rarely share a y to the pixel.
  2. Sort each row left to right.
  3. Insert a column separator where the horizontal gap between two words is
     wide relative to the row's typical character width.

The output is plain text whose columns are separated by runs of spaces, which
is what both the regex extractor and the LLM read best.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionedWord:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


# A gap wider than this many times the estimated character width is treated as
# a column break rather than a word space. 1.8 was chosen empirically: below
# ~1.4 ordinary wide word spacing starts splitting phrases, above ~2.5 real
# column gaps in tight tables get missed.
COLUMN_GAP_RATIO = 1.8

# Two words belong to the same row if their vertical spans overlap by at least
# this fraction of the shorter span.
ROW_OVERLAP = 0.45


def _group_rows(words: list[PositionedWord]) -> list[list[PositionedWord]]:
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.y0, w.x0))
    rows: list[list[PositionedWord]] = [[ordered[0]]]

    for word in ordered[1:]:
        row = rows[-1]
        # Compare against the row's current vertical extent so a tall word
        # does not fragment the row.
        top = min(w.y0 for w in row)
        bottom = max(w.y1 for w in row)
        overlap = min(bottom, word.y1) - max(top, word.y0)
        shorter = min(bottom - top, word.y1 - word.y0)
        if shorter > 0 and overlap / shorter >= ROW_OVERLAP:
            row.append(word)
        else:
            rows.append([word])
    return rows


def _render_row(row: list[PositionedWord]) -> str:
    row = sorted(row, key=lambda w: w.x0)
    if not row:
        return ""

    # Estimate character width from the row itself rather than a global
    # constant, so the threshold adapts to font size and OCR scaling.
    widths = [(w.x1 - w.x0) / max(len(w.text), 1) for w in row if w.text]
    char_width = sorted(widths)[len(widths) // 2] if widths else 5.0
    threshold = char_width * COLUMN_GAP_RATIO * 2  # *2: a space is ~2 char units

    parts = [row[0].text]
    for previous, current in zip(row, row[1:], strict=False):
        gap = current.x0 - previous.x1
        parts.append("   " if gap > threshold else " ")
        parts.append(current.text)
    return "".join(parts).rstrip()


def render(words: list[PositionedWord]) -> str:
    """Positioned words -> layout-preserving text."""
    rows = _group_rows(words)
    lines = [_render_row(row) for row in rows]
    return "\n".join(line for line in lines if line.strip())
