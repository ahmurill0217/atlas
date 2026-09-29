import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from atlas.graph.queries import GraphQueries
from atlas.ontology import OntologyError
from atlas.pipeline import KnowledgeIngestionPipeline
from tests.v1.conftest import CORPUS


def snapshot(engine) -> dict:
    """Canonical graph in UUID-free form, for comparing runs."""
    with Session(engine) as s:
        q = GraphQueries(s)
        key = {}
        for e in q.list_entities():
            ids = sorted(e["identifiers"].items())
            key[e["id"]] = (e["entity_type"], str(ids) if ids else f"name:{e['canonical_name']}")
        edges = sorted((key[g["source_id"]], g["relation_type"], key[g["target_id"]], g["evidence_count"])
                       for g in q.get_edges())
        reviews = sorted((r["review_type"], r["frequency"], r["reason"]) for r in q.list_reviews(None))
        return {"entities": sorted(key.values()), "edges": edges, "reviews": reviews, "stats": q.stats()}


def ingest(kg, settings, ontology, path):
    return KnowledgeIngestionPipeline(kg, settings, ontology).ingest(path)


def test_corpus_builds_expected_graph(kg, atlas_settings, ontology, phase1_corpus):
    report = ingest(kg, atlas_settings, ontology, phase1_corpus)
    assert report.stats["documents_processed"] == 6 and "documents_failed" not in report.stats
    assert report.stats["llm_calls"] == 0
    with Session(kg) as s:
        q = GraphQueries(s)
        people = {e["canonical_name"]: e for e in q.list_entities("Person")}
        assert len(q.find_entity(identifier=("email", "SARAH.CHEN@acme.com"))) == 1   # one Sarah across sources
        assert len(q.find_entity(identifier=("email", "mike.rodriguez@northwind.io"))) == 1
        assert not q.find_entity(identifier=("domain", "gmail.com"))
        rels = {(g["source_name"], g["relation_type"], g["target_name"]) for g in q.get_edges()}
        assert ("Priya Shah", "ATTENDED", "Atlas rollout kickoff") not in rels
        assert ("Atlas rollout kickoff", "HAS_PARTICIPANT", "Priya Shah") in rels
        assert ("Tom Becker", "ATTENDED", "Atlas rollout kickoff") in rels
        assert ("Mike Rodriguez", "MEMBER_OF", "AI Platform") in rels
        assert ("Sarah Chen", "WORKS_AT", "acme.com") in rels
        assert not any(r[1] == "WORKS_AT" and r[0] == "Dana Lee" for r in rels)
        assert people["Sarah Chen"]["identifiers"] == {"email": "sarah.chen@acme.com"}
        assert q.stats()["unsupported_edges"] == 0
        assert all(len(q.get_evidence(g["id"])) >= 1 for g in q.get_edges())
        reviews = {r["review_type"]: r for r in q.list_reviews()}
        assert reviews["NEW_ONTOLOGY_CANDIDATE"]["frequency"] >= 2
        amb = [r for r in q.list_reviews(review_type="AMBIGUOUS_ENTITY_MATCH")]
        assert len(amb) == 1 and amb[0]["candidate_payload"]["name"] == "Sarah Chen"  # name-only Sarah not merged
        kinds = {r["candidate_payload"].get("suggested_relation") or r["candidate_payload"].get("candidate_name")
                 for r in q.list_reviews(review_type="NEW_ONTOLOGY_CANDIDATE")}
        assert kinds == {"SENT_TO", "ActionItem"}


def test_reingest_is_a_noop(kg, atlas_settings, ontology, phase1_corpus):
    ingest(kg, atlas_settings, ontology, phase1_corpus)
    before = snapshot(kg)
    report = ingest(kg, atlas_settings, ontology, phase1_corpus)
    assert report.stats == {**{k: v for k, v in report.stats.items() if k.startswith("latency")},
                            "documents_unchanged": 6, "sections": report.stats["sections"],
                            "review_items_opened": 0, "llm_calls": 0, "llm_tokens": 0, "llm_cost_usd": 0.0}
    assert snapshot(kg) == before


def test_changed_document_adds_version_not_duplicates(kg, atlas_settings, ontology, phase1_corpus):
    corpus = phase1_corpus
    ingest(kg, atlas_settings, ontology, corpus)
    before = snapshot(kg)
    path = corpus / "emails" / "2026-09-01_atlas_rollout.json"
    payload = json.loads(path.read_text())
    payload["body"] += "\n\nP.S. Agenda attached."
    path.write_text(json.dumps(payload))
    report = ingest(kg, atlas_settings, ontology, corpus)
    assert report.stats["documents_processed"] == 1 and report.stats["documents_unchanged"] == 5
    after = snapshot(kg)
    assert after["entities"] == before["entities"]
    assert [e[:3] for e in after["edges"]] == [e[:3] for e in before["edges"]]   # same facts
    assert after["stats"]["document_versions"] == before["stats"]["document_versions"] + 1


def test_fresh_runs_produce_identical_graphs(kg, atlas_settings, ontology, engine, phase1_corpus):
    ingest(kg, atlas_settings, ontology, phase1_corpus)
    first = snapshot(kg)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE kg.audit_log, kg.review_items, kg.candidate_edges, kg.candidate_entities, "
                          "kg.edge_evidence, kg.edges, kg.entity_external_ids, kg.entity_aliases, kg.entities, "
                          "kg.document_processing, kg.document_sections, kg.document_versions, kg.documents, "
                          "kg.ingestion_runs CASCADE"))
    ingest(kg, atlas_settings, ontology, phase1_corpus)
    assert snapshot(kg) == first


def test_audit_log_is_append_only(kg, atlas_settings, ontology, phase1_corpus):
    ingest(kg, atlas_settings, ontology, phase1_corpus)
    with pytest.raises(DBAPIError, match="append-only"):
        with kg.begin() as conn:
            conn.execute(text("UPDATE kg.audit_log SET actor = 'someone'"))


def test_ontology_edited_in_place_is_refused(kg, atlas_settings, ontology, phase1_corpus):
    ingest(kg, atlas_settings, ontology, phase1_corpus)
    tampered = ontology.model_copy(update={"checksum": "0" * 64})
    with pytest.raises(OntologyError, match="bump the version"):
        KnowledgeIngestionPipeline(kg, atlas_settings, tampered).ingest(CORPUS)
