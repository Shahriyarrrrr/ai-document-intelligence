"""The processing pipeline, as a plain function.

Deliberately not a Celery task: `process_document()` takes a session and a
document id and runs synchronously. The Celery task in `tasks.py` is a thin
wrapper. That split means the whole pipeline is testable and runnable from the
evaluation harness without a broker, which is what makes the measured metrics
in the report reproducible.

Stages, each timed and recorded as a ProcessingEvent:
  ingest -> extract -> validate -> index
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..extraction import llm as llm_module
from ..ingestion.base import IngestionError
from ..ingestion.router import ingest
from ..models import (
    Document,
    DocumentStatus,
    ExtractedInvoice,
    Page,
    ProcessingEvent,
    SourceKind,
    ValidationIssue,
)
from ..search.embeddings import embed_texts
from ..validation.rules import needs_review, validate

log = logging.getLogger(__name__)


@contextmanager
def _stage(session: Session, document: Document, name: str):
    """Time a stage and record it, whether it succeeds or throws."""
    started = time.perf_counter()
    detail: dict = {}
    try:
        yield detail
    except Exception as exc:
        session.add(ProcessingEvent(
            document_id=document.id, stage=name, status="failed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={**detail, "error": f"{type(exc).__name__}: {exc}"},
        ))
        raise
    else:
        session.add(ProcessingEvent(
            document_id=document.id, stage=name, status="ok",
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail=detail,
        ))


def _existing_invoice_index(session: Session, exclude_document_id: str) -> list[tuple]:
    """Invoice numbers already known, for duplicate detection."""
    rows = session.execute(
        select(
            ExtractedInvoice.invoice_number,
            ExtractedInvoice.supplier_name,
            ExtractedInvoice.total,
            ExtractedInvoice.document_id,
        ).where(
            ExtractedInvoice.document_id != exclude_document_id,
            ExtractedInvoice.invoice_number.is_not(None),
        )
    ).all()
    return [tuple(r) for r in rows]


def process_document(session: Session, document_id: str) -> Document:
    """Run the full pipeline for one document. Commits as it goes."""
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"document {document_id} not found")

    document.processing_started_at = document.processing_started_at or _now()
    document.error_message = None
    metrics: dict = dict(document.metrics or {})

    try:
        # ---------------- Stage 1: ingest ----------------
        document.status = DocumentStatus.EXTRACTING_TEXT
        session.commit()

        with _stage(session, document, "ingest") as detail:
            result = ingest(document.storage_path)
            detail.update(
                pages=result.page_count,
                ocr_pages=result.ocr_page_count,
                source_kind=result.source_kind,
                warnings=result.warnings,
            )

        document.source_kind = SourceKind(result.source_kind)
        document.page_count = result.page_count
        document.ocr_page_count = result.ocr_page_count
        metrics["warnings"] = result.warnings
        metrics["tables"] = len(result.tables)

        # Replace any pages from a previous run (reprocessing).
        for old in list(document.pages):
            session.delete(old)
        session.flush()

        for page in result.pages:
            session.add(Page(
                document_id=document.id,
                page_number=page.page_number,
                label=page.label,
                text=page.text,
                used_ocr=page.used_ocr,
                ocr_confidence=page.ocr_confidence,
                char_count=page.char_count,
            ))
        session.commit()

        # ---------------- Stage 2: extract ----------------
        document.status = DocumentStatus.EXTRACTING_FIELDS
        session.commit()

        ocr_pages = [p for p in result.pages if p.used_ocr and p.ocr_confidence is not None]
        mean_ocr_conf = (
            sum(p.ocr_confidence for p in ocr_pages) / len(ocr_pages) if ocr_pages else None
        )

        # Only the first N pages go to the model: on a 1000-page batch the
        # invoice header and totals are near the front, and sending the whole
        # thing would blow the context window and the budget. Every page is
        # still indexed for search.
        budget_pages = result.pages[: settings.llm_page_budget]
        text_for_llm = "\n\n".join(
            f"[PAGE {p.page_number}]\n{p.text}" for p in budget_pages if p.text.strip()
        )
        if result.page_count > settings.llm_page_budget:
            metrics["llm_page_budget_applied"] = settings.llm_page_budget

        with _stage(session, document, "extract") as detail:
            extraction, meta = llm_module.extract_invoice(
                text=text_for_llm,
                filename=document.filename,
                source_kind=result.source_kind,
                ocr_confidence=mean_ocr_conf,
            )
            detail.update(method=meta.get("method"), attempts=meta.get("attempts"))

        metrics["extraction"] = {
            k: v for k, v in meta.items() if k in
            ("method", "attempts", "duration_s", "usage", "truncated", "prompt_version")
        }

        for old in list(document.invoices):
            session.delete(old)
        session.flush()

        invoice = ExtractedInvoice(
            document_id=document.id,
            page_from=budget_pages[0].page_number if budget_pages else None,
            page_to=budget_pages[-1].page_number if budget_pages else None,
            invoice_number=extraction.invoice_number,
            invoice_date=extraction.invoice_date,
            supplier_name=extraction.supplier_name,
            customer_name=extraction.customer_name,
            currency=extraction.currency,
            subtotal=extraction.subtotal,
            tax=extraction.tax,
            total=extraction.total,
            line_items=[i.model_dump() for i in extraction.line_items],
            field_confidence=extraction.field_confidence,
            extraction_method=meta.get("method", "unknown"),
            raw_model_output=meta.get("raw_response", {}),
        )
        session.add(invoice)
        session.flush()

        # ---------------- Stage 3: validate ----------------
        document.status = DocumentStatus.VALIDATING
        session.commit()

        with _stage(session, document, "validate") as detail:
            issues = validate(
                extraction,
                {"existing": _existing_invoice_index(session, document.id)},
            )
            detail.update(
                issue_count=len(issues),
                errors=sum(1 for i in issues if i.severity == "error"),
            )

        for issue in issues:
            session.add(ValidationIssue(
                invoice_id=invoice.id,
                code=issue.code,
                severity=issue.severity,
                field=issue.field,
                message=issue.message,
                context=issue.context,
            ))
        invoice.needs_review = needs_review(issues)
        session.commit()

        # ---------------- Stage 4: index ----------------
        with _stage(session, document, "index") as detail:
            pages = list(session.scalars(
                select(Page).where(Page.document_id == document.id).order_by(Page.page_number)
            ))
            texts = [p.text for p in pages]
            if any(t.strip() for t in texts):
                vectors = embed_texts(texts)
                for page, vector in zip(pages, vectors, strict=False):
                    page.embedding = vector
            detail.update(embedded_pages=len(pages))
        session.commit()

        document.status = (
            DocumentStatus.NEEDS_REVIEW if invoice.needs_review else DocumentStatus.COMPLETED
        )
        document.processing_finished_at = _now()
        document.metrics = metrics
        session.commit()
        return document

    except IngestionError as exc:
        session.rollback()
        document = session.get(Document, document_id)
        document.status = DocumentStatus.FAILED
        document.error_message = f"[{exc.code}] {exc}"
        document.processing_finished_at = _now()
        document.metrics = {**(document.metrics or {}), "failure_code": exc.code}
        session.add(ProcessingEvent(
            document_id=document.id, stage="ingest", status="failed",
            detail={"code": exc.code, "error": str(exc)},
        ))
        session.commit()
        return document

    except Exception as exc:
        log.exception("Pipeline failed for %s", document_id)
        session.rollback()
        document = session.get(Document, document_id)
        document.status = DocumentStatus.FAILED
        document.error_message = f"{type(exc).__name__}: {exc}"
        document.processing_finished_at = _now()
        session.commit()
        return document


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
