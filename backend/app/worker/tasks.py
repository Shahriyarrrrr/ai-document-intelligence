"""Celery tasks - thin wrappers around the pipeline.

OCR on a multi-page scan is CPU-bound and can run for minutes, which is
exactly the work that must not happen inside a request. `acks_late` plus
`reject_on_worker_lost` means a document whose worker is killed mid-OCR goes
back on the queue rather than silently disappearing.
"""
from __future__ import annotations

import logging

from celery import Celery

from ..config import settings
from ..db import session_scope
from .pipeline import process_document

log = logging.getLogger(__name__)

celery_app = Celery("docint", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,       # long tasks: do not hoard the queue
    task_track_started=True,
    task_time_limit=1800,               # 30 min hard ceiling for huge PDFs
    task_soft_time_limit=1500,
    result_expires=86400,
)


@celery_app.task(
    name="docint.process_document",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def process_document_task(self, document_id: str) -> dict:
    try:
        with session_scope() as session:
            document = process_document(session, document_id)
            return {
                "document_id": document_id,
                "status": document.status.value,
                "pages": document.page_count,
            }
    except Exception as exc:
        # Infrastructure failures are worth a retry; the pipeline already
        # handles document-level failures internally and does not raise.
        log.exception("Task failed for %s", document_id)
        raise self.retry(exc=exc)


@celery_app.task(name="docint.reprocess_failed")
def reprocess_failed_task(document_ids: list[str]) -> dict:
    results = {}
    for document_id in document_ids:
        try:
            with session_scope() as session:
                document = process_document(session, document_id)
                results[document_id] = document.status.value
        except Exception as exc:
            results[document_id] = f"error: {exc}"
    return results
