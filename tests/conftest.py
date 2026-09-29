"""Shared fixtures. DB tests run against a separate `<db>_test` database that is
created and migrated automatically; `kg` truncates it before each test."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from atlas.config import Settings, get_settings
from atlas.db.admin import ensure_database, migrate, sibling_database_url
from atlas.extraction.candidates import CandidateEdge, CandidateEntity, CandidateSet, EvidenceRef
from atlas.ingestion.normalized import NormalizedDocument, document_id_for
from atlas.ontology import load_ontology

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "examples" / "business"
KG_TABLES = ["audit_log", "review_items", "candidate_edges", "candidate_entities", "edge_evidence", "edges",
             "entity_merge_history", "entity_external_ids", "entity_aliases", "entities", "document_processing",
             "document_sections", "document_versions", "documents", "ingestion_runs", "ontology_versions"]


PHASE1_FILES = ["emails/2026-09-01_atlas_rollout.json", "emails/2026-09-03_re_atlas.json",
                "emails/2026-09-04_gmail_contact.json", "meetings/2026-09-08_atlas_kickoff.json",
                "meetings/2026-09-15_atlas_followup.json", "docs/atlas_project_notes.md"]


@pytest.fixture
def phase1_corpus(tmp_path) -> Path:
    """The original six Phase 1 documents (tests asserting exact Phase 1 results use these)."""
    import shutil

    root = tmp_path / "phase1_corpus"
    for rel in PHASE1_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(CORPUS / rel, root / rel)
    return root


@pytest.fixture(scope="session")
def ontology():
    return load_ontology(str(ROOT / "ontology"))


def ontology_variant(tmp_path: Path, version: str, edit=None):
    """A copy of the ontology with every file's version set to `version`, after `edit(docs)`
    changes the parsed files ({"core.yaml": {...}, ...}). For tests of upgrades and policy."""
    import yaml

    target = tmp_path / f"ontology_{version}_{uuid.uuid4().hex[:6]}"
    target.mkdir()
    docs = {f.name: yaml.safe_load(f.read_text()) for f in (ROOT / "ontology").glob("*.yaml")}
    if edit:
        edit(docs)
    for name, doc in docs.items():
        doc["version"] = version
        (target / name).write_text(yaml.safe_dump(doc, sort_keys=False))
    return load_ontology(str(target))


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
