# Evaluation Report

Generated: `2026-11-03T07:49:56.165183+00:00`  
Extraction mode: **rule_based**  
Methods observed: `rules`

All figures below are measured by `eval/run_eval.py` against the labelled dataset in `eval/dataset/`. Nothing here is estimated.

## Headline

| Metric | Value |
| --- | --- |
| Documents processed | 38 / 38 |
| Document success rate | 100.0% |
| Field-level accuracy | 96.8% (271/280) |
| Documents with every field correct | 32/35 (91.4%) |
| Line-item count accuracy | 91.4% |
| Mean time per document | 0.878s |
| Median / max | 0.003s / 3.295s |

## Accuracy by field

| Field | N | Correct | Accuracy | Extraction rate |
| --- | ---: | ---: | ---: | ---: |
| `invoice_number` | 35 | 35 | 100.0% | 100.0% |
| `invoice_date` | 35 | 35 | 100.0% | 100.0% |
| `supplier_name` | 35 | 34 | 97.1% | 100.0% |
| `customer_name` | 35 | 33 | 94.3% | 100.0% |
| `currency` | 35 | 35 | 100.0% | 100.0% |
| `subtotal` | 35 | 33 | 94.3% | 94.3% |
| `tax` | 35 | 32 | 91.4% | 94.3% |
| `total` | 35 | 34 | 97.1% | 97.1% |

*Extraction rate* is how often a value was produced at all; *accuracy* is how often it was right. A field can be extracted often and still be wrong, and the two columns separate those.

## Accuracy by document type

| Type | Docs | Field accuracy | Mean time | p95 |
| --- | ---: | ---: | ---: | ---: |
| arithmetic | 1 | 100.0% | 0.004s | —s |
| crosscheck | 5 | 100.0% | 0.005s | 0.003s |
| digital | 14 | 100.0% | 0.002s | 0.003s |
| duplicate | 1 | 100.0% | 0.002s | —s |
| image | 6 | 100.0% | 1.29s | 1.354s |
| scanned | 8 | 85.9% | 2.867s | 3.247s |

## Failure handling

Deliberately corrupt/unsupported files correctly rejected: **6/6** (100.0%)

| File | Rejected | Error code |
| --- | --- | --- |
| `broken.xlsx` | yes | `xlsx_corrupt` |
| `empty.csv` | yes | `file_empty` |
| `empty.pdf` | yes | `file_empty` |
| `mislabeled.pdf` | yes | `image_unreadable` |
| `notes.docx` | yes | `unsupported_type` |
| `truncated.pdf` | yes | `pdf_corrupt` |

## Validation

- Documents flagged for manual review: **3**
- Seeded defects detected: **2/2** (the dataset contains a deliberately inconsistent invoice and a deliberate duplicate)

## Cross-verification (PDF ↔ Excel)

- PDFs compared: **5**
- Register rows: **22**
- Result: `{"match": 5, "mismatch": 0, "only_left": 0, "only_right": 0}`
- Expected matches: **5**

All PDFs in this set have a corresponding register row, so a correct run yields match == pdf_count with zero mismatches.

## How to reproduce

```bash
cd backend
python eval/make_dataset.py      # regenerate the labelled dataset
python eval/run_eval.py          # rule-based baseline, no network
LLM_PROVIDER=openai LLM_API_KEY=sk-... python eval/run_eval.py
python eval/run_eval.py --compare   # both, with the delta table
```
