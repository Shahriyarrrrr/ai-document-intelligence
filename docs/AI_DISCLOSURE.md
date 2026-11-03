# AI disclosure

Deliverable 9 of the assignment asks for "AI prompts and disclosure of
AI-assisted development". This document covers both.

## Part 1 — Prompts the running system sends

All runtime prompts live in a single versioned module,
[`backend/app/extraction/prompts.py`](../backend/app/extraction/prompts.py),
rather than being scattered through the code. Current version:
`PROMPT_VERSION = "2026-11-03.1"`, recorded on every extraction in
`documents.metrics.extraction.prompt_version` so a result can always be traced
to the prompt that produced it.

### System prompt

Full text is in the module. The decisions behind it:

**Null is framed as a correct answer, not a failure.** The instruction reads
"null is a correct and expected answer… An honest null is always better than a
plausible guess." Models treat an empty field as something to avoid unless
explicitly told otherwise, and an invented total is worse than a blank one
because nothing downstream flags it. This is reinforced structurally: every
field in the Pydantic schema is `Optional`, so null is also valid at the type
level.

**The model is told the text may be OCR output,** with the measured mean OCR
confidence injected. Without this, models silently "correct" OCR noise into
plausible-looking invoice numbers. The instruction is to report what is
actually there and lower the confidence instead.

**Confidence is requested per field, not per document.** A document-level
score cannot tell a reviewer which of eight numbers to check. Per-field scores
drive the "flagged" highlighting in the review UI, with an explicit band
definition (1.0 explicit and labelled, 0.7–0.9 implicit label, 0.4–0.6
ambiguous or OCR-degraded, <0.4 needs human review).

**Arithmetic is explicitly out of scope for the model.** "Do not calculate a
missing total from other fields — that is the validation layer's job." Letting
the model compute `subtotal + tax` would make the validator's arithmetic check
tautological, since it would be checking the model's own arithmetic rather
than the document.

**The JSON Schema is generated from the Pydantic model** and injected into the
prompt, so the prompt and the parser cannot drift apart.

### Other prompts

- `SEGMENTATION_PROMPT` — splits a multi-invoice document into page ranges.
  Written and tested, but not yet wired into the pipeline (see README, Scope
  and trade-offs).
- **Retry prompt** — on a parse failure the original prompt is re-sent with
  the parse error appended. Feeding the error back is substantially more
  effective than a blind retry; models correct their own malformed JSON
  reliably when shown what broke.

### Model settings

`temperature=0` everywhere, and OpenAI's `response_format={"type":
"json_object"}` where available, which eliminates the most common class of
parse failure outright.

## Part 2 — AI assistance in building this

This project was built with Claude (Anthropic) as a pair-programming
assistant, over a single working session. Disclosed plainly because the
assignment asks, and because the interesting part is not *that* AI was used
but *where it was and was not load-bearing*.

### What AI generated

Most of the implementation code, docstrings and this documentation were
AI-drafted: the ingestion readers, OCR preprocessing, the validation rule set,
API handlers, React components, the test suite and the evaluation harness.

### What was human-directed

Architecture and scope decisions, the choice of which trade-offs to make and
which to document rather than hide, the decision to build a labelled dataset
and measure rather than assert, and the judgement calls recorded in the README
(for example: refusing to let the extractor derive a missing total; treating a
false invoice merge as worse than a missed match).

### What was verified by execution, not accepted on sight

This is the part that matters. Every claim in the README and the evaluation
report was produced by running code, not by asking a model whether the code
worked. Four substantive bugs were found this way, and each is now pinned by a
regression test:

1. **Collapsed table columns.** PyMuPDF emits one table cell per line and
   Tesseract returns a flat token stream. Line-item extraction returned zero
   rows on every document. Fixed by `ingestion/layout.py`, which rebuilds rows
   from positioned word boxes for both sources.

2. **`INVOICE\nNorthwind Traders` parsed as invoice number "rthwind".** The
   pattern `invoice\s*(?:number|no\.?)` matched across a newline, and the "No"
   inside "Northwind" was read as the abbreviation for "number". Fixed with
   `[ \t]*` instead of `\s*` and a word boundary after the keyword.

3. **A `.docx` parsed as a CSV.** OOXML files share the ZIP magic signature
   with `.xlsx`, and the router trusted the extension to disambiguate. The
   CSV parser then "succeeded" on binary junk. Fixed by inspecting the archive
   for `xl/` parts.

4. **Cross-document matching merged two different invoices.** This one was
   only visible because the numbers were measured. Edit-distance matching
   scored `INV-2026-00001` against `INV-2026-00002` — two genuinely different
   invoices — at **0.92**, above the acceptance threshold, while
   `INV-2O26-OOOO1` against `INV-2026-00001` — the same invoice with real OCR
   damage — scored **0.58**, below it. The heuristic was precisely inverted on
   its intended use case. Replaced with OCR-confusable glyph canonicalisation
   plus mandatory corroboration from the total or supplier+date.

A fifth issue was a performance defect rather than a correctness one: every
upload hung for roughly twenty seconds when no Redis was running, because
Celery's result backend retries a lost connection twenty times at one-second
intervals before the fallback path was reached. Replaced with a single cached
500 ms probe.

### Where AI was deliberately not trusted

- **Version pins.** The first `requirements.txt` was written from model
  memory; nineteen of twenty-one pins were wrong. The file is now generated
  from the versions actually installed and tested against.
- **Accuracy numbers.** No figure in the README or the evaluation report was
  written by hand. All of them come from `eval/run_eval.py` and
  `eval/bench_large_pdf.py`, and the harness itself was corrected once when it
  was found to be validating each document against an empty duplicate index,
  which made the duplicate rule structurally unable to fire and produced a
  misleading "1/2 defects detected".
- **Dependency security.** `npm audit` flagged a CVE in the initially chosen
  Next.js version; the project was moved to Next 16 and now reports zero
  vulnerabilities.

### Reproducibility

Nothing here requires taking the above on trust:

```bash
cd backend
pytest                              # 115 tests
python eval/make_dataset.py         # deterministic, fixed seed
python eval/run_eval.py             # regenerates REPORT.md from scratch
python eval/bench_large_pdf.py --pages 1000
cd ../frontend && npm audit && npm run build
```
