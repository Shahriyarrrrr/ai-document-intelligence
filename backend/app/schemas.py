"""API request/response models.

Kept separate from the extraction schema: the extraction schema is a contract
with the LLM, this is a contract with the frontend. Merging them means every
prompt tweak becomes a breaking API change.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LineItemOut(BaseModel):
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class IssueOut(BaseModel):
    id: str
    code: str
    severity: str
    field: str | None
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class CorrectionOut(BaseModel):
    id: str
    field: str
    old_value: str | None
    new_value: str | None
    corrected_by: str
    at: datetime


class InvoiceOut(BaseModel):
    id: str
    document_id: str
    invoice_number: str | None
    invoice_date: str | None
    supplier_name: str | None
    customer_name: str | None
    currency: str | None
    subtotal: float | None
    tax: float | None
    total: float | None
    line_items: list[LineItemOut] = Field(default_factory=list)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    extraction_method: str
    needs_review: bool
    issues: list[IssueOut] = Field(default_factory=list)
    corrections: list[CorrectionOut] = Field(default_factory=list)


class DocumentSummary(BaseModel):
    id: str
    filename: str
    status: str
    source_kind: str
    page_count: int
    ocr_page_count: int
    byte_size: int
    invoice_count: int = 0
    issue_count: int = 0
    needs_review: bool = False
    error_message: str | None = None
    duration_s: float | None = None
    created_at: datetime


class EventOut(BaseModel):
    stage: str
    status: str
    duration_ms: int | None
    detail: dict[str, Any] = Field(default_factory=dict)
    at: datetime


class DocumentDetail(DocumentSummary):
    metrics: dict[str, Any] = Field(default_factory=dict)
    invoices: list[InvoiceOut] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)


class UploadAccepted(BaseModel):
    document_id: str
    filename: str
    status: str
    duplicate_of: str | None = None


class UploadResponse(BaseModel):
    accepted: list[UploadAccepted] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)


class StatusCounts(BaseModel):
    pending: int = 0
    processing: int = 0
    completed: int = 0
    needs_review: int = 0
    failed: int = 0


class CorrectionIn(BaseModel):
    field: str
    value: Any = None
    corrected_by: str = "anonymous"


class SearchHit(BaseModel):
    document_id: str
    filename: str
    page_number: int | None = None
    snippet: str | None = None
    score: float
    invoice_number: str | None = None
    supplier_name: str | None = None
    total: float | None = None


class SearchResponse(BaseModel):
    mode: Literal["keyword", "semantic", "hybrid"]
    query: str
    total: int
    hits: list[SearchHit] = Field(default_factory=list)
    # True when embeddings are real rather than the hash placeholder.
    semantic_available: bool = True


class CrossCheckRequest(BaseModel):
    left_document_ids: list[str] = Field(min_length=1)
    right_document_ids: list[str] = Field(min_length=1)


class CrossCheckResponse(BaseModel):
    id: str
    counts: dict[str, int]
    rows: list[dict[str, Any]]
