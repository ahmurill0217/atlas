"""V1 fixtures. Reuses the session-scoped `engine` (test database, migrated to head)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.config import Settings
from atlas.extraction.candidates import CandidateEdge, CandidateEntity, CandidateSet, EvidenceRef
from atlas.ingestion.normalized import NormalizedDocument, document_id_for
from atlas.ontology import load_ontology

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "examples" / "business"
KG_TABLES = ["audit_log", "review_items", "candidate_edges", "candidate_entities", "edge_evidence", "edges",
             "entity_merge_history", "entity_external_ids", "entity_aliases", "entities", "document_processing",
             "document_sections", "document_versions", "documents", "ingestion_runs", "ontology_versions"]


@pytest.fixture(scope="session")
def ontology():
    return load_ontology(str(ROOT / "ontology" / "v1_0"))


@pytest.fixture
def kg(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE " + ", ".join(f"kg.{t}" for t in KG_TABLES) + " CASCADE"))
    return engine


@pytest.fixture
def atlas_settings(engine) -> Settings:
    return Settings(database_url=engine.url.render_as_string(hide_password=False),
                    internal_domains=["northwind.io"])


def make_doc(session: Session, name: str = "doc-1", text_body: str = "") -> tuple[NormalizedDocument, uuid.UUID]:
    """Persist a minimal document + version so candidates can reference it."""
    from atlas.db.models import Document, DocumentVersion

    doc = NormalizedDocument(document_id=document_id_for("test", name), source_system="test", source_type="text",
                             source_external_id=name, title=name, raw_text=text_body)
    session.merge(Document(id=doc.document_id, source_system="test", source_type="text",
                           source_external_id=name, title=name, permissions={}))
    version_id = uuid.uuid4()
    session.add(DocumentVersion(id=version_id, document_id=doc.document_id, checksum=uuid.uuid4().hex,
                                normalizer_version="test", normalized=doc.model_dump(mode="json")))
    session.flush()
    return doc, version_id


def ent(local_id, suggested_type, name=None, provenance="EXPLICIT_TEXT", confidence=0.95, **identifiers):
    return CandidateEntity(local_id=local_id, suggested_type=suggested_type, name=name, identifiers=identifiers,
                           provenance_class=provenance, extractor="test", extractor_version="0", confidence=confidence)


def edge(doc, version_id, src, tgt, relation, quote="quoted evidence", confidence=0.95, provenance="EXPLICIT_TEXT"):
    return CandidateEdge(local_id=f"{src}|{relation}|{tgt}", source_local_id=src, target_local_id=tgt,
                         suggested_relation=relation, provenance_class=provenance, confidence=confidence,
                         extractor="test", extractor_version="0",
                         evidence=EvidenceRef(document_id=doc.document_id, document_version_id=version_id,
                                              evidence_text=quote, source_field=None))


def candidate_set(doc, version_id, entities, edges=()):
    return CandidateSet(document_id=doc.document_id, document_version_id=version_id, extractor="test",
                        extractor_version="0", entities=list(entities), edges=list(edges))
