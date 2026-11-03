"""Central configuration. Everything is env-driven so the same image runs
locally, in CI and in a container without code changes."""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure -------------------------------------------------
    database_url: str = "postgresql+psycopg://docint:docint@localhost:5432/docint"
    redis_url: str = "redis://localhost:6379/0"
    storage_dir: str = "./storage"

    # --- LLM ------------------------------------------------------------
    # Provider is pluggable; see extraction/llm.py. With no API key the system
    # degrades to the deterministic rule-based extractor instead of crashing,
    # which keeps the test suite runnable offline.
    llm_provider: Literal["openai", "anthropic", "none"] = "openai"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_max_retries: int = 3
    llm_timeout_s: int = 90

    embedding_provider: Literal["openai", "local", "none"] = "local"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # Semantic search needs the pgvector extension. docker-compose ships the
    # pgvector image so this is on by default, but the system must also run
    # against a managed Postgres that does not offer the extension - in that
    # case set VECTOR_ENABLED=false and search degrades to keyword-only
    # rather than failing to start.
    vector_enabled: bool = True

    # --- Ingestion limits ----------------------------------------------
    max_upload_mb: int = 100
    # Pages are processed in chunks so a 1000-page PDF never materialises
    # fully in memory.
    pdf_page_batch: int = 25
    # A page with fewer than this many extractable characters is treated as
    # scanned and routed to OCR.
    ocr_char_threshold: int = 40
    ocr_dpi: int = 300
    ocr_lang: str = "eng"
    # Only the first N pages of a huge PDF are sent to the LLM for invoice
    # extraction; the rest are still indexed for search.
    llm_page_budget: int = 8

    # --- Validation tolerances -----------------------------------------
    # Currency arithmetic is compared to the cent, with a small tolerance for
    # supplier rounding.
    amount_tolerance: float = 0.02

    api_cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
