"""SQLAlchemy models.

Design notes
------------
* `Document` is the unit of upload. One file -> one document.
* `Page` holds per-page extracted text plus how it was obtained (native text
  vs OCR) and an embedding for semantic search. Splitting pages out keeps the
  documents table small and lets a 1000-page PDF be searched page-precisely.
* `ExtractedInvoice` is the structured result. A document may contain more
  than one invoice (multi-invoice PDF, multi-sheet workbook), so this is a
  one-to-many.
* `ValidationIssue` records every rule failure with a severity so the UI can
  separate "needs manual review" from "informational".
* `ProcessingEvent` is the audit trail: every state transition, with timing,
  which feeds the evaluation report.
* `Correction` stores human overrides without destroying the original model
  output - essential for measuring accuracy later.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import settings


EMBEDDING_TYPE = Vector(settings.embedding_dim) if settings.vector_enabled else JSONB


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON}


class DocumentStatus(str, enum.Enum):
    PENDING = "pending"
    EXTRACTING_TEXT = "extracting_text"
    EXTRACTING_FIELDS = "extracting_fields"
    VALIDATING = "validating"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class SourceKind(str, enum.Enum):
    PDF_DIGITAL = "pdf_digital"
    PDF_SCANNED = "pdf_scanned"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"
    CSV = "csv"
    UNKNOWN = "unknown"


class IssueSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(128))
    byte_size: Mapped[int] = mapped_column(Integer)
    # SHA-256 of the file bytes. Used to detect re-uploads of the same file.
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    storage_path: Mapped[str] = mapped_column(String(1024))

    source_kind: Mapped[SourceKind] = mapped_column(
        Enum(SourceKind, native_enum=False), default=SourceKind.UNKNOWN
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False), default=DocumentStatus.PENDING, index=True
    )
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_page_count: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form counters: timings per stage, token usage, retry counts.
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    pages: Mapped[list[Page]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Page.page_number"
    )
    invoices: Mapped[list[ExtractedInvoice]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    events: Mapped[list[ProcessingEvent]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="ProcessingEvent.at"
    )

    @property
    def duration_s(self) -> float | None:
        if self.processing_started_at and self.processing_finished_at:
            return (self.processing_finished_at - self.processing_started_at).total_seconds()
        return None


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_page_per_doc"),
        Index("ix_pages_text_fts", "text", postgresql_using="gin",
              postgresql_ops={"text": "gin_trgm_ops"}),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # 1-based. For spreadsheets this is the sheet index and `label` the name.
    page_number: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    used_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    # Mean Tesseract word confidence 0-100, null for native text.
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    # Vector when pgvector is available, JSONB otherwise. The JSONB form keeps
    # the embeddings readable and the schema valid, but supports no ANN search
    # - see config.vector_enabled.
    embedding = mapped_column(EMBEDDING_TYPE, nullable=True)

    document: Mapped[Document] = relationship(back_populates="pages")


class ExtractedInvoice(Base):
    __tablename__ = "extracted_invoices"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    # Where in the document this invoice came from.
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)

    invoice_number: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    invoice_date: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ISO-8601
    supplier_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    subtotal: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    tax: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    total: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)

    # [{description, quantity, unit_price, amount}, ...]
    line_items: Mapped[list] = mapped_column(JSONB, default=list)
    # Per-field 0-1 confidence as reported by the extractor.
    field_confidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    extraction_method: Mapped[str] = mapped_column(String(32), default="llm")
    raw_model_output: Mapped[dict] = mapped_column(JSONB, default=dict)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped[Document] = relationship(back_populates="invoices")
    issues: Mapped[list[ValidationIssue]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    corrections: Mapped[list[Correction]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class ValidationIssue(Base):
    __tablename__ = "validation_issues"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("extracted_invoices.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[IssueSeverity] = mapped_column(Enum(IssueSeverity, native_enum=False))
    field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    # Supporting numbers so the UI can show the maths that failed.
    context: Mapped[dict] = mapped_column(JSONB, default=dict)

    invoice: Mapped[ExtractedInvoice] = relationship(back_populates="issues")


class Correction(Base):
    """A human override. The original value is never overwritten in place."""

    __tablename__ = "corrections"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("extracted_invoices.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_by: Mapped[str] = mapped_column(String(128), default="anonymous")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    invoice: Mapped[ExtractedInvoice] = relationship(back_populates="corrections")


class ProcessingEvent(Base):
    __tablename__ = "processing_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped[Document] = relationship(back_populates="events")


class CrossCheckResult(Base):
    """Result of comparing two documents against each other (pdf<->pdf or
    pdf<->excel). Kept separate from ValidationIssue because it is about a
    *pair* of documents, not one invoice."""

    __tablename__ = "crosscheck_results"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    left_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    right_document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    matched: Mapped[int] = mapped_column(Integer, default=0)
    mismatched: Mapped[int] = mapped_column(Integer, default=0)
    only_left: Mapped[int] = mapped_column(Integer, default=0)
    only_right: Mapped[int] = mapped_column(Integer, default=0)
    rows: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
