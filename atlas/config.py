"""Deployment settings (environment / .env). Schema and policy live in the
ontology directory, not here: this file only says *where* things are."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]

PIPELINE_VERSION = "1.3.0-phase2"
NORMALIZER_VERSION = "1.4.0"
STRUCTURED_EXTRACTOR_VERSION = "1.3.0"
RESOLVER_VERSION = "1.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_prefix="ATLAS_", populate_by_name=True)

    database_url: str = Field(default="postgresql+psycopg://brain:brain@localhost:5433/brain",
                              validation_alias="DATABASE_URL")
    ontology_dir: Path = ROOT / "ontology" / "v1_3"
    # Our own email domains: organizations on these domains get is_internal = true.
    internal_domains: list[str] = Field(default_factory=list)

    # Retrieval and answering: the `brain` library (OpenSearch + local embedding
    # model server). One index per Atlas database, e.g. ATLAS_BRAIN_INDEX=atlas_enron.
    brain_index: str = "atlas_chunks"
    brain_store_url: str | None = None          # default: runs/brain_store_<index>.db
    brain_opensearch_port: int = 9201
    brain_model_server_port: int = 9100
    ask_provider: str = "openai"
    ask_model: str = "gpt-4o-mini"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
