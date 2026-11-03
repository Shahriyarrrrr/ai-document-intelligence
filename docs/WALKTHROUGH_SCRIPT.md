# Walkthrough recording script

Deliverable 8 is a recorded walkthrough of the working application. This is
the shot list. **Target: 6–7 minutes.** Record at 1920×1080, browser zoom
100%, and keep the terminal font large enough to read on a phone.

## Before you start

```bash
cd doc-intelligence
cp .env.example .env
# Add LLM_API_KEY if you have one. If not, say so on camera - running
# without it is a designed mode, not a gap, and saying so is stronger
# than hoping nobody notices.
docker compose up --build        # wait for all services healthy
cd backend && python eval/make_dataset.py
```

Have ready in a file manager, for drag-and-drop:
`digital_INV-2026-00001.pdf`, `scanned_heavy_INV-2026-00017.pdf`,
`image_light_INV-2026-00023.jpg`, `register_multi_sheet.xlsx`,
`register.csv`, `inconsistent_INV-2026-99001.pdf`, and
`corrupt/truncated.pdf`.

---

## 0:00 — Opening (20s)

Face or voice only, no screen needed.

> "This is a document intelligence system for invoice processing. It ingests
> PDFs, scanned images, Excel and CSV, extracts structured fields, validates
> them, cross-verifies documents against each other, and makes everything
> searchable. I'll show it working, then show the measured numbers — including
> where it does badly."

## 0:20 — Upload (50s)

Screen: http://localhost:3000

1. Drag in all seven files at once, **including the corrupt one**.
2. Point out the corrupt file being rejected immediately with a reason.

> "Validation happens before anything is queued, so you find out now rather
> than three minutes later in a status poll."

3. Let the status counters tick. Point at the progress bars.

> "OCR on a scan takes seconds, so it never runs inside the request. The API
> returns straight away and a Celery worker picks it up."

## 1:10 — A clean digital PDF (40s)

Click `digital_INV-2026-00001.pdf`.

- Show all eight fields extracted, with per-field confidence badges.
- Show the line-item table.

> "Line items are the part most tools get wrong, because both PyMuPDF and
> Tesseract flatten table columns into separate lines. There's a layout
> reconstruction step that rebuilds rows from word coordinates — without it
> this table came back empty."

- Show the processing history table with per-stage timings.

## 1:50 — A degraded scan (45s)

Click `scanned_heavy_INV-2026-00017.pdf`.

- Point at `ocr_pages` and the OCR-confidence warning.
- Compare the confidence badges to the clean document's.

> "This is a deliberately degraded scan — noise, skew, uneven lighting. The
> fields still come out, but confidence is lower, and that's the signal that
> routes a document to a human."

## 2:35 — Validation and correction (70s)

Click `inconsistent_INV-2026-99001.pdf`.

- Show the red **needs review** badge and the `total_mismatch` error, reading
  out the actual arithmetic in the message.

> "The validator shows the maths that failed, not just that something failed."

- Edit the total to the correct value. Show the field turning amber as dirty.
- Click **Save correction**.
- Show the error clearing and the badge turning green.

> "Correcting a field re-runs validation immediately. A UI that leaves a stale
> error on screen after you've fixed it teaches people to ignore errors."

- Scroll to **Correction history**.

> "The original value is kept. Without it you can never measure extraction
> accuracy after the fact — and this table is also the natural training set
> for improving the model."

## 3:45 — Search (45s)

Go to **Search**.

1. `Harbourline` in keyword mode — show highlighted snippets across formats.
2. An invoice number — show exact matching.
3. Switch to hybrid.

> "Hybrid fuses keyword and vector results with Reciprocal Rank Fusion rather
> than a weighted sum, because text relevance scores and cosine distances are
> on completely different scales — any fixed weighting between them is just a
> guess."

4. If running without pgvector, show the honest warning banner.

> "It tells you semantic search is unavailable rather than silently returning
> keyword results labelled as semantic."

## 4:30 — Cross-verification (60s)

Go to **Cross-check**. Side A: the crosscheck PDFs. Side B: the xlsx register.
Run it.

- Show 5 matched, 0 mismatched.

> "This reconciles the supplier's PDFs against the finance team's register."

Then the important part:

> "Matching invoice numbers is harder than it looks. I originally used fuzzy
> string matching, measured it, and found it was exactly backwards:
> INV-2026-00001 against INV-2026-00002 — two different invoices — scored
> 0.92 similarity and got merged, while the same invoice with genuine OCR
> damage scored 0.58 and got missed. It now canonicalises OCR-confusable
> characters and requires corroborating evidence from the total or the
> supplier before accepting a non-exact match. Falsely merging two invoices
> hides a missing one, so that's the error worth being strict about."

## 5:30 — Tests and evaluation (60s)

Terminal, large font.

```bash
cd backend && pytest
```

> "115 tests. No network, no API key — the LLM client is tested through a
> scripted stub. Several of these are named regressions that pin real bugs
> found during development."

```bash
cat eval/results/REPORT.md | head -30
```

Walk the headline table, then deliberately point at the weak number:

> "96.8% field accuracy overall. But broken down by type: 100% on digital
> PDFs and images, and 85.9% on degraded scans. OCR is the bottleneck, not the
> extraction logic. I'd rather show you that number than average it away."

```bash
cat eval/results/large_pdf_benchmark.json
```

> "The brief mentioned 1000-page PDFs, so I measured it: 1000 pages in half a
> second, with 10 megabytes of memory growth. Pages are streamed one at a time
> rather than all rendered up front, so memory is flat regardless of page
> count."

## 6:30 — Close (30s)

> "The README has a Scope and trade-offs section listing what's deliberately
> simplified — no auth, no Alembic, and multi-invoice splitting is designed and
> prompted but not wired in, which is the biggest functional gap. AI assistance
> is disclosed in docs/AI_DISCLOSURE.md, including the four bugs that only
> surfaced because I measured instead of assuming. Thanks for watching."

---

## Recording notes

- **Do not skip the corrupt file or the 85.9% number.** A walkthrough where
  everything is perfect reads as rehearsed; showing a known weakness and
  explaining it reads as engineering judgement.
- Rehearse once, record once. A clean single take beats a polished edit.
- OBS Studio or QuickTime is fine. Keep it under 150 MB so it attaches to an
  email, or upload unlisted to YouTube/Drive and link it.
- Test your microphone before the real take.
