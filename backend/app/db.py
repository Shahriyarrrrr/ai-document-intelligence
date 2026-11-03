"""Database engine, session factory and first-run schema creation.

Alembic is deliberately omitted: for a greenfield evaluation project a single
idempotent `init_db()` is clearer than a migration chain with one revision in
it. The trade-off is documented in the README.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create extensions, tables and indexes. Safe to call repeatedly."""
    with engine.begin() as conn:
        if settings.vector_enabled:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as exc:
                raise RuntimeError(
                    "VECTOR_ENABLED is true but the pgvector extension could not be "
                    "created. Use the pgvector/pgvector image, or set "
                    f"VECTOR_ENABLED=false to run keyword-only. Original error: {exc}"
                ) from exc
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Full-text index for keyword search; separate from the trigram index
        # which serves fuzzy "contains" filtering.
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pages_tsv "
                "ON pages USING gin (to_tsvector('english', text))"
            )
        )
        # IVFFlat needs data to train on; it is created lazily by
        # `ensure_vector_index()` once there are enough rows.


def ensure_vector_index(min_rows: int = 200) -> bool:
    """Create the ANN index once the table is big enough to benefit.

    Below ~200 rows a sequential scan beats IVFFlat and the index would be
    trained on too few vectors to produce useful centroids.
    """
    if not settings.vector_enabled:
        return False
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM pages WHERE embedding IS NOT NULL")
        ).scalar_one()
        if count < min_rows:
            return False
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_pages_embedding "
                "ON pages USING ivfflat (embedding vector_cosine_ops) "
                "WITH (lists = 100)"
            )
        )
    return True


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for worker code."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
