"""Search: keyword, semantic, and a hybrid of the two.

Keyword search uses Postgres full-text (`to_tsvector`/`plainto_tsquery`) with
`ts_headline` for snippets - exact terms like an invoice number or a supplier
name must match exactly, and an embedding will not reliably do that.

Semantic search uses pgvector cosine distance over page embeddings, for
queries like "cleaning supplies for the Brooklyn office" where no literal term
appears in the document.

Hybrid fuses both with Reciprocal Rank Fusion rather than a weighted score
sum. RRF needs no score normalisation, which matters because BM25-ish text
ranks and cosine distances are on entirely different scales and any fixed
weighting between them is arbitrary.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Float, and_, cast, func, or_, select, text
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Document, ExtractedInvoice, Page
from ..schemas import CrossCheckRequest, CrossCheckResponse, SearchHit, SearchResponse
from ..search.embeddings import embed_one, is_semantic_available

router = APIRouter(prefix="/api/search", tags=["search"])

# RRF constant. 60 is the value from the original Cormack et al. paper and
# behaves well when neither ranker is clearly stronger.
RRF_K = 60


def _keyword_hits(db: Session, query: str, limit: int) -> list[tuple[str, int, str, float]]:
    tsv = func.to_tsvector("english", Page.text)
    tsq = func.plainto_tsquery("english", query)
    stmt = (
        select(
            Page.document_id,
            Page.page_number,
            func.ts_headline(
                "english", Page.text, tsq,
                text("'MaxWords=30, MinWords=10, MaxFragments=1'"),
            ),
            func.ts_rank(tsv, tsq).label("rank"),
        )
        .where(tsv.op("@@")(tsq))
        .order_by(text("rank DESC"))
        .limit(limit)
    )
    return [tuple(r) for r in db.execute(stmt).all()]


def _semantic_hits(db: Session, query: str, limit: int) -> list[tuple[str, int, str, float]]:
    # Without pgvector there is no distance operator, so semantic ranking is
    # simply unavailable. Returning nothing (and reporting it in the response)
    # is honest; silently returning keyword results labelled "semantic" is not.
    if not settings.vector_enabled:
        return []
    vector = embed_one(query)
    distance = Page.embedding.cosine_distance(vector).label("distance")
    stmt = (
        select(
            Page.document_id,
            Page.page_number,
            func.left(Page.text, 240),
            distance,
        )
        .where(Page.embedding.is_not(None))
        .order_by(distance)
        .limit(limit)
    )
    # Convert distance to a similarity so higher is better everywhere.
    return [(d, p, s, 1.0 - float(dist)) for d, p, s, dist in db.execute(stmt).all()]


def _decorate(db: Session, hits: list[tuple[str, int, str, float]]) -> list[SearchHit]:
    if not hits:
        return []
    document_ids = {h[0] for h in hits}
    documents = {
        d.id: d for d in db.scalars(select(Document).where(Document.id.in_(document_ids)))
    }
    invoices: dict[str, ExtractedInvoice] = {}
    for invoice in db.scalars(
        select(ExtractedInvoice).where(ExtractedInvoice.document_id.in_(document_ids))
    ):
        invoices.setdefault(invoice.document_id, invoice)

    out = []
    for document_id, page_number, snippet, score in hits:
        document = documents.get(document_id)
        if document is None:
            continue
        invoice = invoices.get(document_id)
        out.append(SearchHit(
            document_id=document_id,
            filename=document.filename,
            page_number=page_number,
            snippet=(snippet or "").strip(),
            score=round(float(score), 4),
            invoice_number=invoice.invoice_number if invoice else None,
            supplier_name=invoice.supplier_name if invoice else None,
            total=float(invoice.total) if invoice and invoice.total is not None else None,
        ))
    return out


@router.get("", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1),
    mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
) -> SearchResponse:
    if mode == "keyword":
        hits = _keyword_hits(db, q, limit)
    elif mode == "semantic":
        hits = _semantic_hits(db, q, limit)
    else:
        keyword = _keyword_hits(db, q, limit * 2)
        semantic = _semantic_hits(db, q, limit * 2)
        fused: dict[tuple[str, int], dict] = {}
        for rank, (document_id, page, snippet, _score) in enumerate(keyword, start=1):
            key = (document_id, page)
            fused.setdefault(key, {"snippet": snippet, "score": 0.0})
            fused[key]["score"] += 1.0 / (RRF_K + rank)
        for rank, (document_id, page, snippet, _score) in enumerate(semantic, start=1):
            key = (document_id, page)
            entry = fused.setdefault(key, {"snippet": snippet, "score": 0.0})
            entry["score"] += 1.0 / (RRF_K + rank)
            # Prefer the keyword snippet: it is query-highlighted.
            if not entry.get("snippet"):
                entry["snippet"] = snippet
        ranked = sorted(fused.items(), key=lambda kv: kv[1]["score"], reverse=True)[:limit]
        hits = [(doc, page, data["snippet"], data["score"]) for (doc, page), data in ranked]

    return SearchResponse(
        mode=mode, query=q, total=len(hits), hits=_decorate(db, hits),
        semantic_available=is_semantic_available(),
    )


@router.get("/invoices", response_model=list[dict])
def filter_invoices(
    db: Session = Depends(get_db),
    supplier: str | None = None,
    customer: str | None = None,
    invoice_number: str | None = None,
    currency: str | None = None,
    min_total: float | None = None,
    max_total: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    needs_review: bool | None = None,
    limit: int = Query(100, le=500),
) -> list[dict]:
    """Structured filtering over extracted fields.

    Separate from free-text search on purpose: "every invoice from Acme over
    $5,000 in Q2" is a filter, and forcing it through a text index would be
    both slower and wrong.
    """
    conditions = []
    if supplier:
        conditions.append(ExtractedInvoice.supplier_name.ilike(f"%{supplier}%"))
    if customer:
        conditions.append(ExtractedInvoice.customer_name.ilike(f"%{customer}%"))
    if invoice_number:
        conditions.append(ExtractedInvoice.invoice_number.ilike(f"%{invoice_number}%"))
    if currency:
        conditions.append(ExtractedInvoice.currency == currency.upper())
    if min_total is not None:
        conditions.append(cast(ExtractedInvoice.total, Float) >= min_total)
    if max_total is not None:
        conditions.append(cast(ExtractedInvoice.total, Float) <= max_total)
    # Dates are stored as ISO-8601 strings, so lexicographic comparison is
    # chronological - that is the whole reason for normalising to ISO.
    if date_from:
        conditions.append(ExtractedInvoice.invoice_date >= date_from)
    if date_to:
        conditions.append(ExtractedInvoice.invoice_date <= date_to)
    if needs_review is not None:
        conditions.append(ExtractedInvoice.needs_review.is_(needs_review))

    stmt = select(ExtractedInvoice)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    stmt = stmt.order_by(ExtractedInvoice.created_at.desc()).limit(limit)

    results = []
    for invoice in db.scalars(stmt):
        results.append({
            "id": invoice.id,
            "document_id": invoice.document_id,
            "invoice_number": invoice.invoice_number,
            "invoice_date": invoice.invoice_date,
            "supplier_name": invoice.supplier_name,
            "customer_name": invoice.customer_name,
            "currency": invoice.currency,
            "total": float(invoice.total) if invoice.total is not None else None,
            "needs_review": invoice.needs_review,
            "issue_count": len(invoice.issues),
        })
    return results


@router.post("/crosscheck", response_model=CrossCheckResponse)
def run_crosscheck(
    payload: CrossCheckRequest, db: Session = Depends(get_db)
) -> CrossCheckResponse:
    """Reconcile two sets of documents against each other.

    Either side may be PDFs or spreadsheets, which covers both the pdf<->pdf
    and pdf<->excel cases from the brief with one code path.
    """
    from ..ingestion.router import ingest
    from ..models import CrossCheckResult
    from ..validation import crosscheck as cc

    def build_records(document_ids: list[str]) -> list[cc.Record]:
        records: list[cc.Record] = []
        for document_id in document_ids:
            document = db.get(Document, document_id)
            if document is None:
                raise HTTPException(404, f"document {document_id} not found")
            if document.source_kind.value in ("spreadsheet", "csv"):
                # Tables are not persisted (they can be large and are cheap to
                # re-read); re-parse from the stored file.
                try:
                    result = ingest(document.storage_path)
                except Exception as exc:
                    raise HTTPException(
                        422, f"cannot re-read {document.filename}: {exc}"
                    ) from exc
                for table in result.tables:
                    records.extend(
                        cc.records_from_table(table, document.id, document.filename)
                    )
            else:
                for invoice in document.invoices:
                    records.append(
                        cc.record_from_invoice(invoice, document.id, document.filename)
                    )
        return records

    left = build_records(payload.left_document_ids)
    right = build_records(payload.right_document_ids)
    if not left or not right:
        raise HTTPException(422, "one side produced no comparable invoice records")

    report = cc.cross_check(left, right)
    counts = report.counts()
    rows = report.to_rows()

    stored = CrossCheckResult(
        left_document_id=payload.left_document_ids[0],
        right_document_id=payload.right_document_ids[0],
        matched=counts["match"],
        mismatched=counts["mismatch"],
        only_left=counts["only_left"],
        only_right=counts["only_right"],
        rows=rows,
    )
    db.add(stored)
    db.commit()

    return CrossCheckResponse(id=stored.id, counts=counts, rows=rows)
