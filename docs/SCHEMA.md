# Database schema

Postgres 16 with `vector` (pgvector) and `pg_trgm`.

## Entity relationships

```
                    ┌──────────────────┐
                    │    documents     │  one row per uploaded file
                    │──────────────────│
                    │ id          PK   │
                    │ filename         │
                    │ checksum    IDX  │  sha256, detects re-uploads
                    │ storage_path     │
                    │ source_kind      │  pdf_digital|pdf_scanned|image|
                    │ status      IDX  │    spreadsheet|csv|unknown
                    │ page_count       │
                    │ ocr_page_count   │
                    │ error_message    │
                    │ metrics   JSONB  │  stage timings, warnings, tokens
                    │ processing_*_at  │
                    └───┬────┬─────┬───┘
            ┌───────────┘    │     └──────────────┐
            │ 1:N            │ 1:N                │ 1:N
   ┌────────▼───────┐ ┌──────▼─────────────┐ ┌────▼────────────────┐
   │     pages      │ │ extracted_invoices │ │  processing_events  │
   │────────────────│ │────────────────────│ │─────────────────────│
   │ id         PK  │ │ id             PK  │ │ id              PK  │
   │ document_id FK │ │ document_id    FK  │ │ document_id     FK  │
   │ page_number    │ │ invoice_number IDX │ │ stage               │
   │ label          │ │ invoice_date       │ │ status              │
   │ text      FTS  │ │ supplier_name      │ │ duration_ms         │
   │ used_ocr       │ │ customer_name      │ │ detail       JSONB  │
   │ ocr_confidence │ │ currency           │ │ at                  │
   │ embedding VEC  │ │ subtotal  NUM(18,2)│ └─────────────────────┘
   └────────────────┘ │ tax       NUM(18,2)│
                      │ total     NUM(18,2)│
                      │ line_items   JSONB │
                      │ field_conf   JSONB │
                      │ extraction_method  │
                      │ raw_model_out JSONB│
                      │ needs_review   IDX │
                      └───┬────────────┬───┘
                  1:N     │            │   1:N
        ┌─────────────────▼──┐  ┌──────▼──────────────┐
        │ validation_issues  │  │     corrections     │
        │────────────────────│  │─────────────────────│
        │ id             PK  │  │ id              PK  │
        │ invoice_id     FK  │  │ invoice_id      FK  │
        │ code          IDX  │  │ field               │
        │ severity           │  │ old_value           │
        │ field              │  │ new_value           │
        │ message            │  │ corrected_by        │
        │ context     JSONB  │  │ at                  │
        └────────────────────┘  └─────────────────────┘

        ┌──────────────────────────────────────┐
        │          crosscheck_results          │  pairwise reconciliation
        │──────────────────────────────────────│
        │ id, left_document_id, right_document_id (both FK → documents)
        │ matched, mismatched, only_left, only_right
        │ rows JSONB   per-invoice detail incl. field differences
        └──────────────────────────────────────┘
```

## Design decisions

**Pages are a separate table.** Keeps `documents` small, lets a 1000-page PDF
be searched page-precisely, and means an embedding is stored per page rather
than one averaged vector per document — averaging a thousand pages produces a
vector that means nothing.

**`extracted_invoices` is one-to-many with `documents`.** A batch PDF or a
register sheet may hold several invoices. The pipeline currently writes one
per document (see Scope and trade-offs in the README), but the schema does not
need migrating when segmentation is wired in.

**`NUMERIC(18,2)` for money, never `FLOAT`.** Exact decimal arithmetic. The
application layer reads these into `Decimal` for the same reason.

**Corrections never overwrite history.** The field is updated in place *and* a
`corrections` row records the previous value, who changed it and when. Without
the original, extraction accuracy can never be measured after the fact — the
corrections table is also the natural source of fine-tuning data.

**`raw_model_output` is retained.** When a field is wrong, the question is
always whether the model got it wrong or the parser did. Keeping the raw JSON
answers that in seconds.

**`processing_events` records failures too.** A stage that throws still writes
an event with its duration and error. A timing table that only contains
successes cannot explain a slow document.

**`metrics` and `context` are JSONB.** Stage timings, token counts and the
numbers behind a failed arithmetic check are genuinely variable; a column per
possible key would be churn. Anything queried in anger gets promoted to a
real column.

## Indexes

| Index | Type | Serves |
| --- | --- | --- |
| `documents.status` | btree | status filtering and the counts widget |
| `documents.checksum` | btree | re-upload detection |
| `pages(document_id, page_number)` | unique | ordering, idempotent reprocessing |
| `ix_pages_tsv` | GIN on `to_tsvector('english', text)` | keyword search |
| `ix_pages_text_fts` | GIN trigram | fuzzy `ILIKE` filtering |
| `ix_pages_embedding` | IVFFlat, `lists=100` | semantic search |
| `extracted_invoices.invoice_number` | btree | duplicate detection, lookup |
| `extracted_invoices.needs_review` | btree | the review queue |
| `validation_issues.code` | btree | issue-type analytics |

The IVFFlat index is created lazily by `ensure_vector_index()` once ≥200 pages
are embedded. Below that a sequential scan is faster and the index would be
trained on too few vectors to produce useful centroids.

## Migrations

Schema creation is a single idempotent `init_db()` rather than Alembic. For a
greenfield project with one revision, a migration chain is ceremony. This is
the first thing to replace before a second environment exists — noted in the
README under Scope and trade-offs.
