"""Document upload, status, results, corrections and reprocessing."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import get_db
from ..ingestion.router import SUPPORTED_EXT
from ..models import (
    Correction,
    Document,
    DocumentStatus,
    ExtractedInvoice,
    ValidationIssue,
)
from ..schemas import (
    CorrectionIn,
    DocumentDetail,
    DocumentSummary,
    StatusCounts,
    UploadAccepted,
    UploadResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents", tags=["documents"])

# Fields a human is allowed to correct. Anything else is derived or internal.
CORRECTABLE = {
    "invoice_number", "invoice_date", "supplier_name", "customer_name",
    "currency", "subtotal", "tax", "total",
}
NUMERIC_FIELDS = {"subtotal", "tax", "total"}


@lru_cache(maxsize=1)
def _broker_available() -> bool:
    """Probe Redis once, with a short timeout.

    Checked directly rather than relying on `.delay()` to fail: Celery's
    result backend retries a lost connection 20 times with a 1s delay, so a
    missing broker turns every single upload into a 20-second hang before the
    inline fallback is reached. One 500ms probe, cached, instead.
    """
    try:
        import redis

        client = redis.from_url(
            settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        client.ping()
        return True
    except Exception as exc:
        log.warning(
            "Redis broker unreachable (%s); documents will be processed "
            "in-process. This is a fallback, not a production path.", exc
        )
        return False


def _enqueue(document_id: str, background: BackgroundTasks) -> None:
    """Hand the document to Celery, or run inline if no broker is reachable.

    The inline path is what lets the evaluation harness and the test suite run
    the full pipeline with no Redis. It is not a production path and logs
    loudly when taken.
    """
    if _broker_available():
        try:
            from ..worker.tasks import process_document_task

            process_document_task.delay(document_id)
            return
        except Exception as exc:
            log.warning("Celery dispatch failed (%s); processing inline", exc)

    def _run() -> None:
        from ..db import session_scope
        from ..worker.pipeline import process_document

        with session_scope() as session:
            process_document(session, document_id)

    background.add_task(_run)


def _summary(document: Document) -> DocumentSummary:
    issue_count = sum(len(i.issues) for i in document.invoices)
    return DocumentSummary(
        id=document.id,
        filename=document.filename,
        status=document.status.value,
        source_kind=document.source_kind.value,
        page_count=document.page_count,
        ocr_page_count=document.ocr_page_count,
        byte_size=document.byte_size,
        invoice_count=len(document.invoices),
        issue_count=issue_count,
        needs_review=any(i.needs_review for i in document.invoices),
        error_message=document.error_message,
        duration_s=document.duration_s,
        created_at=document.created_at,
    )


def _invoice_out(invoice: ExtractedInvoice) -> dict:
    return {
        "id": invoice.id,
        "document_id": invoice.document_id,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date,
        "supplier_name": invoice.supplier_name,
        "customer_name": invoice.customer_name,
        "currency": invoice.currency,
        "subtotal": float(invoice.subtotal) if invoice.subtotal is not None else None,
        "tax": float(invoice.tax) if invoice.tax is not None else None,
        "total": float(invoice.total) if invoice.total is not None else None,
        "line_items": invoice.line_items or [],
        "field_confidence": invoice.field_confidence or {},
        "extraction_method": invoice.extraction_method,
        "needs_review": invoice.needs_review,
        "issues": [
            {
                "id": issue.id, "code": issue.code, "severity": issue.severity.value,
                "field": issue.field, "message": issue.message, "context": issue.context or {},
            }
            for issue in invoice.issues
        ],
        "corrections": [
            {
                "id": c.id, "field": c.field, "old_value": c.old_value,
                "new_value": c.new_value, "corrected_by": c.corrected_by, "at": c.at,
            }
            for c in invoice.corrections
        ],
    }


@router.post("/upload", response_model=UploadResponse)
async def upload(
    background: BackgroundTasks,
    files: list[UploadFile],
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Accept one or many files. Each is validated, stored, then queued.

    Validation happens before anything is queued so the caller gets immediate,
    per-file feedback rather than discovering a rejected file minutes later in
    the status poll.
    """
    os.makedirs(settings.storage_dir, exist_ok=True)
    accepted: list[UploadAccepted] = []
    rejected: list[dict[str, str]] = []
    max_bytes = settings.max_upload_mb * 1024 * 1024

    for upload_file in files:
        name = upload_file.filename or "unnamed"
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_EXT:
            rejected.append({"filename": name, "reason": f"unsupported extension '{ext}'"})
            continue

        payload = await upload_file.read()
        if not payload:
            rejected.append({"filename": name, "reason": "file is empty"})
            continue
        if len(payload) > max_bytes:
            rejected.append({
                "filename": name,
                "reason": f"exceeds {settings.max_upload_mb}MB limit",
            })
            continue

        checksum = hashlib.sha256(payload).hexdigest()
        existing = db.scalar(
            select(Document).where(Document.checksum == checksum).limit(1)
        )

        document_id = str(uuid.uuid4())
        path = os.path.join(settings.storage_dir, f"{document_id}{ext}")
        with open(path, "wb") as handle:
            handle.write(payload)

        document = Document(
            id=document_id,
            filename=name,
            content_type=upload_file.content_type or "application/octet-stream",
            byte_size=len(payload),
            checksum=checksum,
            storage_path=path,
            status=DocumentStatus.PENDING,
        )
        db.add(document)
        db.commit()

        _enqueue(document_id, background)
        accepted.append(UploadAccepted(
            document_id=document_id,
            filename=name,
            status=DocumentStatus.PENDING.value,
            # Not a rejection: the same invoice may legitimately be re-sent.
            # The caller is told, and the duplicate rule flags it downstream.
            duplicate_of=existing.id if existing else None,
        ))

    return UploadResponse(accepted=accepted, rejected=rejected)


@router.get("", response_model=list[DocumentSummary])
def list_documents(
    db: Session = Depends(get_db),
    status: str | None = Query(None),
    needs_review: bool | None = Query(None),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
) -> list[DocumentSummary]:
    stmt = (
        select(Document)
        .options(selectinload(Document.invoices).selectinload(ExtractedInvoice.issues))
        .order_by(Document.created_at.desc())
    )
    if status:
        try:
            stmt = stmt.where(Document.status == DocumentStatus(status))
        except ValueError as exc:
            raise HTTPException(400, f"unknown status '{status}'") from exc

    documents = list(db.scalars(stmt.limit(limit).offset(offset)))
    summaries = [_summary(d) for d in documents]
    if needs_review is not None:
        summaries = [s for s in summaries if s.needs_review == needs_review]
    return summaries


@router.get("/status", response_model=StatusCounts)
def status_counts(db: Session = Depends(get_db)) -> StatusCounts:
    """Aggregate counts for the progress widget - one query, not N."""
    rows = db.execute(
        select(Document.status, func.count()).group_by(Document.status)
    ).all()
    counts = StatusCounts()
    processing_states = {
        DocumentStatus.EXTRACTING_TEXT,
        DocumentStatus.EXTRACTING_FIELDS,
        DocumentStatus.VALIDATING,
    }
    for status, count in rows:
        if status == DocumentStatus.PENDING:
            counts.pending += count
        elif status in processing_states:
            counts.processing += count
        elif status == DocumentStatus.COMPLETED:
            counts.completed += count
        elif status == DocumentStatus.NEEDS_REVIEW:
            counts.needs_review += count
        elif status == DocumentStatus.FAILED:
            counts.failed += count
    return counts


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(document_id: str, db: Session = Depends(get_db)) -> DocumentDetail:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "document not found")
    base = _summary(document).model_dump()
    return DocumentDetail(
        **base,
        metrics=document.metrics or {},
        invoices=[_invoice_out(i) for i in document.invoices],
        events=[
            {
                "stage": e.stage, "status": e.status, "duration_ms": e.duration_ms,
                "detail": e.detail or {}, "at": e.at,
            }
            for e in document.events
        ],
    )


@router.post("/{document_id}/reprocess", response_model=DocumentSummary)
def reprocess(
    document_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> DocumentSummary:
    """Re-run a document. Valid from any terminal state, not just failed -
    a prompt change is a perfectly good reason to reprocess a success."""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "document not found")
    if not os.path.exists(document.storage_path):
        raise HTTPException(410, "the stored file is no longer available")

    document.status = DocumentStatus.PENDING
    document.error_message = None
    document.processing_started_at = None
    document.processing_finished_at = None
    db.commit()

    _enqueue(document_id, background)
    db.refresh(document)
    return _summary(document)


@router.post("/reprocess-failed", response_model=list[str])
def reprocess_all_failed(
    background: BackgroundTasks, db: Session = Depends(get_db)
) -> list[str]:
    failed = list(db.scalars(
        select(Document).where(Document.status == DocumentStatus.FAILED)
    ))
    ids = []
    for document in failed:
        if not os.path.exists(document.storage_path):
            continue
        document.status = DocumentStatus.PENDING
        document.error_message = None
        ids.append(document.id)
    db.commit()
    for document_id in ids:
        _enqueue(document_id, background)
    return ids


@router.patch("/invoices/{invoice_id}")
def correct_invoice(
    invoice_id: str,
    payload: list[CorrectionIn],
    db: Session = Depends(get_db),
) -> dict:
    """Apply human corrections and re-run validation.

    Re-validating is the point: correcting a subtotal should clear the
    arithmetic error it caused, and a UI that leaves a stale error showing
    after the fix teaches users to ignore errors.
    """
    invoice = db.get(ExtractedInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(404, "invoice not found")

    applied = []
    for change in payload:
        if change.field not in CORRECTABLE:
            raise HTTPException(400, f"field '{change.field}' is not correctable")

        old = getattr(invoice, change.field)
        value = change.value
        if change.field in NUMERIC_FIELDS and value is not None and value != "":
            from ..extraction.schema import parse_amount

            value = parse_amount(value)
            if value is None:
                raise HTTPException(400, f"'{change.value}' is not a valid amount")
        if change.field == "invoice_date" and value:
            from ..extraction.schema import parse_date

            value = parse_date(value)
            if value is None:
                raise HTTPException(400, f"'{change.value}' is not a valid date")
        if value == "":
            value = None

        setattr(invoice, change.field, value)
        db.add(Correction(
            invoice_id=invoice.id,
            field=change.field,
            old_value=None if old is None else str(old),
            new_value=None if value is None else str(value),
            corrected_by=change.corrected_by,
        ))
        # A corrected field is known-good.
        confidence = dict(invoice.field_confidence or {})
        confidence[change.field] = 1.0
        invoice.field_confidence = confidence
        applied.append(change.field)

    db.flush()

    # Re-validate against the corrected values.
    from ..extraction.schema import InvoiceExtraction
    from ..validation.rules import needs_review, validate
    from ..worker.pipeline import _existing_invoice_index

    refreshed = InvoiceExtraction(
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        supplier_name=invoice.supplier_name,
        customer_name=invoice.customer_name,
        currency=invoice.currency,
        subtotal=invoice.subtotal,
        tax=invoice.tax,
        total=invoice.total,
        line_items=invoice.line_items or [],
        field_confidence=invoice.field_confidence or {},
    )
    issues = validate(
        refreshed, {"existing": _existing_invoice_index(db, invoice.document_id)}
    )
    for old_issue in list(invoice.issues):
        db.delete(old_issue)
    db.flush()
    for issue in issues:
        db.add(ValidationIssue(
            invoice_id=invoice.id, code=issue.code, severity=issue.severity,
            field=issue.field, message=issue.message, context=issue.context,
        ))
    invoice.needs_review = needs_review(issues)

    document = db.get(Document, invoice.document_id)
    if document.status in (DocumentStatus.COMPLETED, DocumentStatus.NEEDS_REVIEW):
        document.status = (
            DocumentStatus.NEEDS_REVIEW
            if any(i.needs_review for i in document.invoices)
            else DocumentStatus.COMPLETED
        )
    db.commit()
    db.refresh(invoice)
    return {"applied": applied, "invoice": _invoice_out(invoice)}
