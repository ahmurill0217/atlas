"""Deployment settings (environment / .env). Schema and policy live in the
ontology directory, not here: this file only says *where* things are."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]

PIPELINE_VERSION = "1.2.2-phase2"
NORMALIZER_VERSION = "1.1.0"
STRUCTURED_EXTRACTOR_VERSION = "1.2.1"
RESOLVER_VERSION = "1.1.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      env_prefix="ATLAS_", populate_by_name=True)

    database_url: str = Field(default="postgresql+psycopg://brain:brain@localhost:5433/brain",
                              validation_alias="DATABASE_URL")
    ontology_dir: Path = ROOT / "ontology" / "v1_2"
    # Our own email domains: organizations on these domains get is_internal = true.
    internal_domains: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
