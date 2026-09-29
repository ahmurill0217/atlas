"""Shared fixtures. DB tests run against a separate `<db>_test` database that is
created and migrated automatically, and truncated before every test."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from brain_v0.config import Settings, get_settings
from brain_v0.db.admin import GRAPH_TABLES, SOURCE_TABLES, ensure_database, migrate, sibling_database_url, truncate
from brain_v0.extraction.models import ExtractedKnowledgeGraph
from brain_v0.pipeline.build_brain import PendingChunk


def _test_url() -> str:
    return sibling_database_url(get_settings().database_url, "test")


@pytest.fixture(scope="session")
def engine():
    try:
        ensure_database(_test_url())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not available ({exc}); run `docker compose up -d`")
    migrate(_test_url())
    eng = create_engine(_test_url())
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    truncate(engine, GRAPH_TABLES + SOURCE_TABLES)
    return engine


@pytest.fixture
def session(db):
    with Session(db) as s:
        yield s
        s.rollback()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url=_test_url(), embedding_provider="hash",
                    openai_api_key=None, extraction_workers=2)


class FakeLLM:
    """Returns canned extractions. `responses` maps a substring of the chunk
    text to the JSON-like dict the LLM would have produced."""

    model_name = "fake-llm"

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls = 0

    def generate(self, system, user, schema):
        self.calls += 1
        for key, payload in self.responses.items():
            if key in user:
                return schema.model_validate(payload)
        return schema.model_validate({"entities": [], "relationships": []})


def ent(local_id, name, entity_type, aliases=(), description=None, confidence=0.9) -> dict:
    return {"local_id": local_id, "name": name, "entity_type": entity_type, "description": description,
            "aliases": list(aliases), "confidence": confidence}


def rel(src, tgt, relationship_type, evidence, confidence=0.9, description=None) -> dict:
    return {"source_local_id": src, "target_local_id": tgt, "relationship_type": relationship_type,
            "description": description, "evidence": evidence, "confidence": confidence}


def graph(entities, relationships=()) -> ExtractedKnowledgeGraph:
    return ExtractedKnowledgeGraph.model_validate({"entities": entities, "relationships": list(relationships)})


def make_chunk(session: Session, content: str, source_uri: str = "doc.md", index: int = 0) -> PendingChunk:
    """Insert a document+chunk row and return it as a PendingChunk."""
    from brain_v0.db.models import Chunk, Document
    from brain_v0.pipeline.ingest import chunk_id_for, document_id_for

    doc_id = document_id_for(source_uri)
    if session.get(Document, doc_id) is None:
        session.add(Document(id=doc_id, source_uri=source_uri, title=source_uri, content_hash="x", metadata_={}))
        session.flush()
    chunk = Chunk(id=chunk_id_for(doc_id, index, content), document_id=doc_id, chunk_index=index,
                  content=content, metadata_={})
    session.add(chunk)
    session.flush()
    return PendingChunk(chunk.id, content, source_uri)
