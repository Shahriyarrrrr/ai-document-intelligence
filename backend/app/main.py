"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import documents, search
from .config import settings
from .db import ensure_vector_index, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.storage_dir, exist_ok=True)
    init_db()
    # The ANN index needs rows to train on, so this is a no-op on a fresh
    # database and takes effect on a later restart. Not fatal either way.
    try:
        ensure_vector_index()
    except Exception as exc:
        log.warning("Vector index not created: %s", exc)
    log.info("Ready. LLM provider=%s model=%s", settings.llm_provider, settings.llm_model)
    yield


app = FastAPI(
    title="Document Intelligence System",
    description=(
        "Automated extraction, validation and search over invoices and "
        "financial documents (PDF, image, spreadsheet, CSV)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents.router)
app.include_router(search.router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness plus dependency status.

    Reports degraded rather than failing when the LLM is unconfigured: the
    system is designed to run without it, and a health check that goes red on
    a supported configuration trains people to ignore health checks.
    """
    from sqlalchemy import text

    from .db import engine
    from .search.embeddings import is_semantic_available

    database_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("Health check: database unreachable: %s", exc)
        database_ok = False

    llm_configured = bool(settings.llm_api_key) and settings.llm_provider != "none"
    return {
        "status": "ok" if database_ok else "degraded",
        "database": database_ok,
        "llm_configured": llm_configured,
        "extraction_mode": "llm" if llm_configured else "rule_based_fallback",
        "semantic_search": is_semantic_available(),
    }
