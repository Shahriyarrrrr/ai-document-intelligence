"""Embeddings for semantic search.

Local sentence-transformers by default. The reasoning: an invoice corpus is
high-volume and low-variety, the documents are not sensitive-free (they carry
supplier and customer names), and sending every page of every invoice to a
third-party embedding API is both a cost and a data-governance problem. A
384-dimension MiniLM runs on CPU fast enough for this workload.

The OpenAI path is kept for when recall matters more than cost.

Model loading is lazy and cached: importing this module must stay cheap, since
the API process imports it but only the worker ever embeds.
"""
from __future__ import annotations

import hashlib
import logging
import struct
from functools import lru_cache

from ..config import settings

log = logging.getLogger(__name__)

# Pages longer than this are truncated before embedding. MiniLM's window is
# 256 word-pieces; feeding more just wastes time on silently dropped text.
MAX_EMBED_CHARS = 2_000


@lru_cache(maxsize=1)
def _local_model():
    from sentence_transformers import SentenceTransformer

    log.info("Loading embedding model %s", settings.embedding_model)
    return SentenceTransformer(settings.embedding_model)


@lru_cache(maxsize=1)
def _openai_client():
    from openai import OpenAI

    return OpenAI(api_key=settings.llm_api_key)


def _hash_embedding(text: str, dim: int) -> list[float]:
    """Deterministic pseudo-embedding used when no model is available.

    This is NOT semantic. It exists so the pipeline, the schema and the tests
    all work with embeddings present in environments with no model download
    and no API key. Semantic search quality in that mode is no better than
    random, and the evaluation report says so rather than quietly reporting a
    meaningless recall number.
    """
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).digest()
    # Stretch the digest to the required dimension.
    raw = (digest * ((dim * 4) // len(digest) + 1))[: dim * 4]
    values = struct.unpack(f"{dim}f", raw)
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    trimmed = [t[:MAX_EMBED_CHARS] for t in texts]

    if settings.embedding_provider == "local":
        try:
            model = _local_model()
            vectors = model.encode(trimmed, normalize_embeddings=True,
                                   show_progress_bar=False)
            return [v.tolist() for v in vectors]
        except Exception as exc:
            log.warning("Local embedding failed (%s); using hash fallback", exc)
            return [_hash_embedding(t, settings.embedding_dim) for t in trimmed]

    if settings.embedding_provider == "openai" and settings.llm_api_key:
        try:
            response = _openai_client().embeddings.create(
                model="text-embedding-3-small", input=trimmed, dimensions=settings.embedding_dim
            )
            return [item.embedding for item in response.data]
        except Exception as exc:
            log.warning("OpenAI embedding failed (%s); using hash fallback", exc)

    return [_hash_embedding(t, settings.embedding_dim) for t in trimmed]


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


def is_semantic_available() -> bool:
    """True when embeddings are real AND searchable. The eval report uses this
    to label whether reported semantic-search numbers are meaningful."""
    if not settings.vector_enabled:
        return False
    if settings.embedding_provider == "local":
        try:
            _local_model()
            return True
        except Exception:
            return False
    return settings.embedding_provider == "openai" and bool(settings.llm_api_key)
