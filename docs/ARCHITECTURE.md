# Architecture

## System diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Next.js 16 (App Router)                        │
│   /            upload, live status, document table                      │
│   /documents/*  field review, confidence, issues, inline correction     │
│   /search       keyword · semantic · hybrid                             │
│   /crosscheck   two-sided reconciliation                                │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │ REST (JSON)
┌───────────────────────────────▼─────────────────────────────────────────┐
│                             FastAPI                                     │
│  upload ─ validate ext/size/magic ─ sha256 ─ write to storage ─ enqueue │
│  Returns 200 immediately. OCR never runs inside a request.              │
└───────────────┬────────────────────────────────────┬────────────────────┘
                │ Redis (broker)                     │ SQLAlchemy
┌───────────────▼─────────────────┐    ┌─────────────▼────────────────────┐
│       Celery worker             │    │      Postgres 16 + pgvector      │
│  acks_late, prefetch=1          │    │  documents · pages · invoices    │
│  30-min hard limit              │    │  validation_issues · corrections │
└───────────────┬─────────────────┘    │  processing_events · crosschecks │
                │                      └──────────────────────────────────┘
                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          PIPELINE (app/worker/pipeline.py)              │
│                                                                         │
│  1. INGEST ─────────────────────────────────────────────────────────    │
│     sniff magic bytes ──┬── PDF    → per-page: native text or OCR       │
│                         ├── image  → OCR (multi-frame TIFF aware)       │
│                         └── xlsx/csv → pandas, per-sheet + raw tables   │
│     all paths → layout.py rebuilds rows from positioned words           │
│                                                                         │
│  2. EXTRACT ────────────────────────────────────────────────────────    │
│     first N pages → LLM (strict JSON, temperature 0)                    │
│        ├─ parse ok        → Pydantic validate → done                    │
│        ├─ malformed       → retry with the parse error fed back (×3)    │
│        └─ still failing   → deterministic rule extractor                │
│                                                                         │
│  3. VALIDATE ───────────────────────────────────────────────────────    │
│     9 rules, Decimal arithmetic, severity-graded                        │
│     error ⇒ needs_review                                                │
│                                                                         │
│  4. INDEX ──────────────────────────────────────────────────────────    │
│     embed pages → pgvector; Postgres FTS built from the text column     │
│                                                                         │
│  Each stage is timed and written to processing_events, success or fail. │
└─────────────────────────────────────────────────────────────────────────┘
```

## Request lifecycle

1. **Upload.** The API checks extension, size and emptiness per file and
   returns accept/reject for each one immediately. A rejected file never
   enters the queue, so the caller learns about it now rather than from a
   status poll three minutes later.
2. **Store.** Bytes are written under a UUID filename; a SHA-256 is recorded.
   A re-upload of identical bytes is accepted and flagged `duplicate_of`
   rather than refused — the same invoice legitimately arrives twice, and
   that judgement belongs to the validation layer, not the upload endpoint.
3. **Enqueue.** Redis is probed once with a 500 ms timeout and the result
   cached. If no broker is present the document is processed in-process with
   a warning. Without that probe, Celery's result backend retries a lost
   connection twenty times at one second each, turning every upload into a
   twenty-second hang.
4. **Process.** The worker runs the four stages above, committing between
   each so the UI can show real progress.
5. **Review.** Documents with an `error`-severity issue land in
   `needs_review`. Corrections re-run validation synchronously.

## Ingestion detail

### Format detection
Magic bytes first, extension second. Users rename files and browsers send
`application/octet-stream`. OOXML containers (`.xlsx`, `.docx`, `.pptx`) all
share the ZIP signature, so the archive is inspected for `xl/` parts — without
that check a `.docx` routes to the CSV parser and "succeeds" on binary junk.

### Per-page OCR routing
The decision is per page, not per document. Real PDFs are routinely mixed: a
digital cover sheet followed by scanned attachments. A page whose native text
layer is under `OCR_CHAR_THRESHOLD` characters is rendered at `OCR_DPI` and
OCR'd; everything else uses the text layer.

### Memory under load
Pages are yielded from a generator. Each pixmap is released immediately after
conversion, so peak memory is independent of page count — measured at 10 MB
growth over a 1000-page document.

### Layout reconstruction
Both PyMuPDF's text mode and Tesseract's word output destroy column structure.
`layout.py` groups positioned words into rows by vertical overlap (not exact
`y` equality — glyphs on one visual line rarely share a `y` to the pixel),
sorts each row by `x`, and inserts a column separator where the horizontal gap
exceeds a multiple of the row's own median character width. Deriving the
threshold from the row rather than a global constant makes it adapt to font
size and OCR scaling.

### OCR preprocessing
Grayscale → resize toward ~300 DPI equivalent → deskew via minimum-area
rectangle (ignoring implausible angles beyond ±15°, which usually mean the
detector latched onto a logo) → adaptive Gaussian threshold (handles the
uneven lighting of a photographed invoice, where global Otsu produces a
half-black page) → median blur. Deliberately conservative: aggressive
denoising eats thin digits, and the worst place to lose a character is a
total.

## Extraction detail

The schema is the contract. Every field is `Optional`, string null-tokens
(`"N/A"`, `"not found"`, `"-"`) coerce to `None`, amounts parse both
`1,234.56` and `1.234,56` plus parenthesised and `CR` negatives, and dates
normalise to ISO-8601 — which is also what makes lexicographic date range
filtering correct in SQL.

The prompt is versioned in `extraction/prompts.py`. Three choices in it matter:
null is framed as a *correct* answer rather than a failure; confidence is
requested per field, because a document-level score cannot tell a reviewer
which number to check; and the model is told the text may be OCR output, since
otherwise it silently "corrects" OCR noise into plausible invoice numbers.

## Validation detail

Nine rules, each a pure function returning `Issue` objects, so they are unit
testable in isolation and a rule that throws cannot fail the document.

| Rule | Severity |
| --- | --- |
| Missing required field (`invoice_number`, `invoice_date`, `supplier_name`, `total`) | error |
| Missing recommended field | warning |
| `subtotal + tax ≠ total` | error |
| Line items do not sum to subtotal | error |
| `quantity × unit_price ≠ amount` | warning |
| Implausibly large amount (lost decimal separator) | error |
| Negative amount (possible credit note) | warning |
| Tax rate > 40% of subtotal | warning |
| Future or pre-1990 date | warning |
| Field confidence < 0.55 | warning |
| Duplicate invoice | error |

All money comparison uses `Decimal` with a configurable tolerance.

## Cross-verification

Four passes, in order of decreasing confidence:

1. **Exact match** on a normalised key (alphanumerics, uppercased), so
   `INV-2026-00001`, `inv 2026 00001` and `INV202600001` collapse together.
2. **OCR-canonical match with corroboration.** Confusable glyphs are mapped to
   digits (`O→0`, `I→1`, `S→5`, `B→8`, `Z→2`, `G→6`…), the canonical forms must
   match exactly, *and* either the totals must agree or supplier+date must.
3. **Content fingerprint** — supplier prefix, date and total — for records
   whose number is unusable.
4. Anything unmatched is reported as present on one side only.

Pass 2 replaced edit-distance matching, which was measured to be precisely
inverted on this data: two consecutive real invoices score 0.92 similarity
while a genuinely OCR-damaged number scores 0.58. Falsely merging two invoices
hides a missing one, which is the worse error, so corroboration is mandatory.

Field comparison is type-aware: amounts within tolerance, dates after
normalisation, and names ignoring case, punctuation and legal suffixes
("Acme Ltd" = "Acme Limited").

## Search

- **Keyword** — Postgres `to_tsvector`/`plainto_tsquery` with `ts_headline`
  snippets. Exact terms like an invoice number must match exactly; an
  embedding will not reliably do that.
- **Semantic** — pgvector cosine distance over per-page embeddings, for
  queries where no literal term appears in the document.
- **Hybrid** — Reciprocal Rank Fusion (`k=60`) rather than a weighted score
  sum. BM25-ish text ranks and cosine distances live on different scales, so
  any fixed weighting between them is arbitrary; RRF needs no normalisation.

Structured filtering (`/api/search/invoices`) is a separate endpoint on
purpose. "Every invoice from Acme over $5,000 in Q2" is a filter, and pushing
it through a text index would be both slower and wrong.

## Failure modes and responses

| Failure | Response |
| --- | --- |
| Corrupt / truncated PDF | `IngestionError` with a specific code, document marked `failed` |
| Password-protected PDF | Empty password tried first, then `pdf_encrypted` |
| `.docx` renamed to `.xlsx` | ZIP contents inspected, rejected as `unsupported_type` |
| One bad page in a long PDF | Logged, page yielded empty, rest continues |
| OCR unavailable | `ocr_unavailable` surfaced rather than silent empty text |
| LLM returns malformed JSON | Retry with the error fed back, ×3, then rule fallback |
| LLM rate-limited / down | Exponential backoff, then rule fallback |
| No API key | Rule-based extractor; `/health` reports `extraction_mode` |
| No pgvector | Keyword-only; API returns `semantic_available: false` |
| No Redis | In-process execution with a warning |
| Worker killed mid-task | `acks_late` returns the task to the queue |
| A validation rule throws | Captured as an `info` issue; other rules still run |

## Technology choices

| Choice | Why | Alternative considered |
| --- | --- | --- |
| FastAPI | Async, generates the OpenAPI docs, Pydantic native | Flask — no async, manual schemas |
| Celery + Redis | Mature, `acks_late` survives worker loss | RQ — simpler but weaker guarantees |
| Postgres + pgvector | One store for relational, FTS and vectors | Separate Pinecone/Qdrant — another service to run and keep consistent |
| PyMuPDF | Fastest text layer, exposes word coordinates | pdfplumber — slower on 1000 pages |
| Tesseract | Free, offline, no per-page cost | Cloud OCR — more accurate, but sends every invoice to a third party |
| Local embeddings | Invoices carry supplier and customer names | OpenAI embeddings — better recall, data leaves the host |
| Next.js | App Router, TypeScript, fast to build | Plain React + Vite — more wiring |
