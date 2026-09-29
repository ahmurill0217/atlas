"""Runtime configuration, read from environment variables / `.env`.

Every tunable threshold lives here so experiments can change behaviour without
code edits. Defaults match docker-compose.yml.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://brain:brain@localhost:5433/brain"

    # --- LLM -----------------------------------------------------------------
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_seed: int | None = None
    extraction_workers: int = 8
    # Retries with exponential backoff on rate limits / transient errors.
    llm_max_retries: int = 8

    # --- Embeddings ------------------------------------------------------------
    # "openai" for real embeddings, "hash" for an offline bag-of-words embedder
    # (useful for tests and for exploring the pipeline without an API key).
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    # Must match the VECTOR(n) columns created by the migration.
    embedding_dim: int = 1536

    # --- Chunking --------------------------------------------------------------
    chunk_max_tokens: int = 400
    # Sentences repeated between consecutive chunks. 0 by default: overlap makes
    # the same sentence count as evidence twice and inflates support counts.
    chunk_overlap_sentences: int = 0

    # --- Extraction ontology (Experiment B) ------------------------------------
    # Empty = fully dynamic, Cognee-like extraction (Experiment A).
    allowed_entity_types: list[str] = Field(default_factory=list)
    allowed_relationship_types: list[str] = Field(default_factory=list)

    # --- Entity resolution -----------------------------------------------------
    resolution_fuzzy_enabled: bool = True
    resolution_fuzzy_threshold: float = 92.0  # rapidfuzz ratio, 0-100
    resolution_embedding_enabled: bool = False  # Phase 2: needs entity embeddings
    resolution_embedding_match: float = 0.92  # cosine similarity -> MATCH_EXISTING
    resolution_embedding_ambiguous: float = 0.85  # between this and match -> AMBIGUOUS

    # --- Relationship validation -----------------------------------------------
    allow_self_relationships: bool = False
    # Evidence must be (approximately) a quote from the chunk.
    evidence_min_grounding: float = 90.0  # rapidfuzz partial_ratio, 0-100
    # If true, both endpoints must be named inside the evidence quote itself.
    # If false (default), they must be named somewhere in the chunk, which
    # tolerates pronouns ("It binds KRAS G12C...") inside the quote.
    evidence_require_endpoints_in_quote: bool = False
    # Optional mapping of relationship-type synonyms, e.g. {"inhibits": "targets"}.
    relationship_type_synonyms: dict[str, str] = Field(default_factory=dict)
    # Optional inverse mapping, e.g. {"targeted_by": "targets"}: the edge is
    # flipped so both phrasings land on the same canonical, directed edge.
    relationship_type_inverses: dict[str, str] = Field(default_factory=dict)


@lru_cache
def get_settings() -> Settings:
    return Settings()
