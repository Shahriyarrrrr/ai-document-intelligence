# AI-Powered Document Intelligence System

Automated extraction, validation, cross-verification and search over business
documents — PDFs (digital and scanned), images, Excel workbooks and CSVs.

Built as an assessment submission. **[Scope and trade-offs](#scope-and-trade-offs)
is the section to read first** — it states plainly what is production-grade,
what is deliberately simplified, and why.

---

## Quick start

```bash
git clone <repo-url> && cd doc-intelligence
cp .env.example .env            # optionally add LLM_API_KEY
docker compose up --build
```

- Web UI — http://localhost:3000
- API docs (OpenAPI) — http://localhost:8000/docs
- Health — http://localhost:8000/health

**It runs with no API key.** Without one, extraction uses a deterministic
rule-based extractor instead of an LLM. Accuracy is lower; the system does not
break. The measured difference is in the [evaluation report](backend/eval/results/REPORT.md).

### Running without Docker

```bash
# Postgres 16 with pgvector, and Redis, must be reachable.
cd backend
pip install -r requirements.txt
# System dependency: tesseract-ocr (apt install tesseract-ocr / brew install tesseract)
uvicorn app.main:app --reload                                   # API
celery -A app.worker.tasks.celery_app worker --loglevel=info     # worker

cd ../frontend && npm install && npm run dev                     # UI
```

No pgvector available? Set `VECTOR_ENABLED=false`. Search degrades to
keyword-only and the API reports `semantic_available: false` rather than
quietly returning keyword hits labelled as semantic.

---

## What it does

| # | Task | Where |
| --- | --- | --- |
| 1 | Multi-format ingestion with OCR | `app/ingestion/` |
| 2 | LLM field extraction, strict JSON, no invented values | `app/extraction/` |
| 3 | Validation + PDF↔PDF / PDF↔Excel cross-verification | `app/validation/` |
| 4 | REST API with background processing | `app/api/`, `app/worker/` |
| 5 | Postgres schema, keyword + semantic search | `app/models.py`, `app/api/search.py` |
| 6 | Next.js review interface | `frontend/` |
| 7 | Test suite + measured evaluation | `tests/`, `eval/` |

---

## Measured results

From `backend/eval/results/REPORT.md`, produced by `python eval/run_eval.py`
over a 38-document labelled dataset. **These are the rule-based baseline
(no LLM).** Re-run with a key to measure the LLM path.

| Metric | Result |
| --- | --- |
| Documents processed | 38 / 38 (100%) |
| Field-level accuracy | **96.8%** (271/280) |
| Documents with every field correct | 32 / 35 (91.4%) |
| Line-item count accuracy | 91.4% |
| Corrupt files correctly rejected | 6 / 6 (100%) |
| Seeded defects detected | 2 / 2 |
| PDF↔Excel cross-check | 5 matched, 0 mismatched, 0 unmatched |
| Mean time/document | 0.88s (digital 0.003s, image 1.2s, scanned 2.7s) |

Accuracy by document type — the honest breakdown:

| Type | Docs | Field accuracy |
| --- | ---: | ---: |
| Digital PDF | 14 | 100% |
| Image (JPG/PNG/TIFF) | 6 | 100% |
| **Scanned PDF (degraded)** | 8 | **85.9%** |

Scanned PDFs are where accuracy drops, which is the expected and interesting
result: OCR noise, not extraction logic, is the bottleneck.

### Large-PDF throughput

The brief specifies ~1000-page PDFs. Measured by `eval/bench_large_pdf.py`:

| | |
| --- | --- |
| 1000-page digital PDF | **0.52s** (1921 pages/s) |
| Peak RSS growth | **10 MB** |
| Time to first page | 0.02s |

Memory is flat because pages are opened and released one at a time rather than
rendered up front. Scanned pages are OCR-bound at roughly 1–3 s/page.

---

## Architecture

```
            ┌──────────────┐
            │  Next.js UI  │  upload · monitor · review · correct · search
            └──────┬───────┘
                   │ REST
            ┌──────▼───────┐
            │  FastAPI     │  validate → store → enqueue (returns immediately)
            └──────┬───────┘
                   │ Redis
            ┌──────▼───────┐
            │ Celery worker│
            └──────┬───────┘
                   │
   ┌───────────────┼───────────────┬───────────────┐
   ▼               ▼               ▼               ▼
 INGEST         EXTRACT        VALIDATE          INDEX
 route by       LLM → strict   arithmetic,       embed pages,
 magic bytes    JSON schema    duplicates,       FTS + pgvector
 PDF/OCR/XLSX   ↓ on failure   cross-doc
 layout rebuild rule extractor recon
   └───────────────┴───────────────┴───────────────┘
                   │
            ┌──────▼──────────────┐
            │ Postgres + pgvector │
            └─────────────────────┘
```

Full detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[docs/SCHEMA.md](docs/SCHEMA.md)

### Five decisions worth defending

**1. Layout is reconstructed from word coordinates, not taken as given.**
PyMuPDF emits one table cell per line and Tesseract returns a flat token
stream. Both destroy column structure, which makes line-item tables
unrecoverable. `app/ingestion/layout.py` rebuilds rows from positioned word
boxes for both sources. Before this, line-item extraction returned zero rows;
after, it matches ground truth.

**2. Null is a first-class answer.** Every field in the extraction schema is
`Optional`, the prompt states that null is correct, and the schema never
derives a missing `total` from `subtotal + tax`. A model that cannot say "not
present" will invent something, and an invented total is worse than a blank
one because nothing downstream flags it.

**3. Cross-document matching refuses edit distance.** Fuzzy string matching on
invoice numbers is actively harmful here, and measurably so:
`INV-2026-00001` vs `INV-2026-00002` — two different invoices — score **0.92**
similarity, while `INV-2O26-OOOO1` vs `INV-2026-00001` — the same invoice with
real OCR damage — scores **0.58**. The matcher instead canonicalises
OCR-confusable glyphs (`O→0`, `I→1`, `S→5`…), requires an exact match on the
canonical form, *and* requires corroborating evidence from the total or
supplier+date. Merging two real invoices hides a missing one, so that error is
treated as worse than failing to match.

**4. Money is `Decimal`, never `float`.** `0.1 + 0.2 != 0.3` in binary floating
point. A validator that reports a 0.0000001 discrepancy on a correct invoice
teaches people to ignore validators.

**5. Degradation is designed, not accidental.** No LLM key → rule-based
extractor. LLM returns malformed JSON → retry with the parse error fed back,
then fall back. No pgvector → keyword search, honestly labelled. No Redis →
in-process execution with a loud warning. Each path is tested.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/documents/upload` | Multi-file upload; per-file accept/reject |
| `GET` | `/api/documents` | List, filter by status / needs-review |
| `GET` | `/api/documents/status` | Aggregate counts for the progress view |
| `GET` | `/api/documents/{id}` | Full detail: fields, issues, stage timings |
| `POST` | `/api/documents/{id}/reprocess` | Re-run one document |
| `POST` | `/api/documents/reprocess-failed` | Re-run every failed document |
| `PATCH` | `/api/documents/invoices/{id}` | Apply corrections, then re-validate |
| `GET` | `/api/search?q=&mode=` | keyword / semantic / hybrid (RRF) |
| `GET` | `/api/search/invoices` | Structured filter: supplier, amount, date range |
| `POST` | `/api/search/crosscheck` | Reconcile two document sets |
| `GET` | `/health` | Liveness + dependency status |

Corrections re-run validation immediately, so fixing a subtotal clears the
arithmetic error it caused. A UI that leaves a stale error on screen after the
fix trains users to ignore errors.

---

## Testing

```bash
cd backend
pytest                      # 115 tests
pytest tests/test_api.py    # auto-skips if no database is reachable
```

**115 tests, all passing.** No network or API key required — the LLM client is
exercised through a scripted stub that covers clean JSON, markdown fences,
JSON embedded in prose, retry-then-succeed, and exhausted-retries fallback.

Several tests are explicitly labelled regressions and pin bugs found during
development: the `.docx`-parsed-as-CSV routing hole, `INVOICE\nNorthwind` read
as invoice number "rthwind", collapsed table columns, and the cross-check
false-merge described above.

---

## Evaluation

```bash
cd backend
python eval/make_dataset.py        # 38 labelled files + 6 corrupt, seeded
python eval/run_eval.py            # rule-based baseline, fully offline
LLM_PROVIDER=openai LLM_API_KEY=sk-... python eval/run_eval.py
python eval/run_eval.py --compare  # both modes, with a delta table
python eval/bench_large_pdf.py --pages 1000
```

The dataset is synthetic and generated from a fixed seed. That is a deliberate
choice: public invoice corpora are licence-encumbered and contain real company
data, and generating them means ground truth is exact rather than
hand-annotated. It includes degraded scans (noise, skew, uneven lighting, JPEG
artefacts), a semicolon-delimited latin-1 CSV, a multi-sheet workbook with an
empty sheet, an invoice whose total is deliberately wrong, a genuine duplicate,
and six corrupt files.

---

## Scope and trade-offs

Honest accounting of what is and is not production-ready.

### Fully implemented
Multi-format ingestion with per-page OCR routing · layout reconstruction ·
LLM extraction with strict schema, retries and fallback · nine validation rules ·
PDF↔PDF and PDF↔Excel reconciliation · full REST API · Celery background
processing · Postgres schema with audit trail · keyword, semantic and hybrid
search · review UI with inline correction · 115 tests · measured evaluation.

### Deliberately simplified

**No authentication.** The brief does not mention users or tenancy, and a
hand-rolled auth layer would be worse than none. Production needs OIDC/JWT,
per-tenant row isolation, and object-level authorisation on every endpoint.

**`init_db()` instead of Alembic.** For a greenfield project one idempotent
schema creation is clearer than a migration chain with a single revision.
Alembic is the first thing to add before a second deployment exists.

**One invoice per document.** The schema is one-to-many and the segmentation
prompt is written (`extraction/prompts.py`), but multi-invoice splitting is not
wired into the pipeline. A 1000-page batch is currently extracted as one
invoice from the first `LLM_PAGE_BUDGET` pages, while every page is still
indexed for search. This is the largest functional gap.

**Local filesystem storage.** Fine for one node, wrong for several. S3 with
presigned uploads is the change.

**Single-table vector index.** IVFFlat with `lists=100`, created once there
are ≥200 embedded pages. Beyond ~1M pages this needs partitioning and HNSW.

**Line-item correction is read-only in the UI.** Header fields are editable
and audited; line items display only.

### Known limitations

- OCR is English-only (`OCR_LANG`); other languages need the matching
  Tesseract language pack.
- Accuracy on heavily degraded scans is 85.9%, well below the 100% on clean
  documents. Table structure is what degrades first.
- Semantic search quality is untested in this report: no labelled relevance
  judgements were built, so no recall number is claimed. The harness reports
  whether embeddings are real rather than printing a meaningless figure.
- Handwritten annotations are not handled.
- The duplicate rule compares against all prior invoices with a simple index;
  at very high volume this wants a dedicated unique constraint and a blocking key.

---

## AI-assisted development

This project was built with AI assistance. Every prompt the running system
sends is in `app/extraction/prompts.py`, and the development process is
disclosed in **[docs/AI_DISCLOSURE.md](docs/AI_DISCLOSURE.md)**.

## Repository layout

```
backend/
  app/
    ingestion/     format routing, PDF, OCR, spreadsheets, layout rebuild
    extraction/    schema, prompts, LLM client, rule-based fallback
    validation/    rules engine, cross-document reconciliation
    search/        embeddings
    api/           documents, search
    worker/        pipeline + Celery tasks
  tests/           115 tests
  eval/            dataset generator, evaluation harness, benchmark
frontend/          Next.js 16 review interface
docs/              architecture, schema, AI disclosure, walkthrough script
```
