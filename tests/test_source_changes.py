"""The graph is current-only: a changed document replaces its contribution, a deleted one
removes it, and nothing it alone supported survives (atlas.graph.sweep)."""

import json
import uuid
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.graph.sweep import DocumentSweep
from atlas.ingestion.normalized import document_id_for
from atlas.pipeline import KnowledgeIngestionPipeline
from atlas.retrieval.brain_bridge import drain_queue, iter_documents
from tests.conftest import ontology_variant


def write_email(root, name, message_id, frm, to, cc=(), date="2026-09-01T15:00:00Z", body="Hello."):
    path = root / f"{name}.json"
    path.write_text(json.dumps({"source_system": "gmail", "message_id": message_id, "thread_id": f"t-{message_id}",
                                "subject": name, "from": frm, "to": list(to), "cc": list(cc), "date": date,
                                "body": body, "attachments": []}))
    return path


def write_doc(root, name, updated_at, author, title="Plan", uri="https://docs.example.com/plan"):
    path = root / f"{name}.json"
    payload = {"atlas_document": "1.0", "source_system": "gdrive", "id": "plan-1", "source_type": "google_doc",
               "title": title, "created_at": "2026-09-01T10:00:00Z", "updated_at": updated_at,
               "author": author, "text": "The plan."}
    if uri:
        payload["uri"] = uri
    path.write_text(json.dumps(payload))
    return path


def pipeline(kg, settings, ontology):
    return KnowledgeIngestionPipeline(kg, settings, ontology)


def q(kg, sql, **params):
    with Session(kg) as s:
        return s.execute(text(sql), params).all()


def count(kg, table):
    return q(kg, f"SELECT count(*) FROM kg.{table}")[0][0]


def person(kg, email):
    rows = q(kg, "SELECT e.id, e.properties, e.property_sources FROM kg.entities e JOIN kg.entity_external_ids x "
                 "ON x.entity_id = e.id WHERE x.identifier_type = 'email' AND x.value = :v", v=email)
    return rows[0] if rows else None


def facts(kg):
    return set(q(kg, "SELECT s.canonical_name, g.relation_type, t.canonical_name FROM kg.edges g "
                     "JOIN kg.entities s ON s.id = g.source_entity_id JOIN kg.entities t ON t.id = g.target_entity_id"))


def test_edit_drops_what_the_new_version_no_longer_says(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"], cc=["Cy Fox <cy@zeta.io>"])
    p.ingest(tmp_path)
    ann, bob = person(kg, "ann@acme.com").id, person(kg, "bob@acme.com").id
    assert ("e1", "SENT_TO", "Cy Fox") in facts(kg) and person(kg, "cy@zeta.io")

    write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"])     # Cy taken off
    report = p.ingest(tmp_path)
    assert report.stats["documents_processed"] == 1
    assert report.stats["edges_removed"] >= 2 and report.stats["entities_removed"] >= 2       # Cy and zeta.io
    assert person(kg, "cy@zeta.io") is None
    assert not q(kg, "SELECT 1 FROM kg.entity_external_ids WHERE value = 'zeta.io'")
    assert not any("Cy Fox" in f for f in facts(kg))
    assert (person(kg, "ann@acme.com").id, person(kg, "bob@acme.com").id) == (ann, bob)       # ids are stable
    assert count(kg, "document_versions") == 1
    assert q(kg, "SELECT count(*) FROM kg.edge_evidence v JOIN kg.documents d ON d.id = v.document_id "
                 "WHERE v.document_version_id <> d.current_version_id")[0][0] == 0
    assert ("document_replaced",) in q(kg, "SELECT action FROM kg.audit_log")


def test_shared_fact_survives_until_its_last_document_goes(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"], date="2026-09-01T10:00:00Z")
    write_email(tmp_path, "e2", "m2", "Ann Lee <ann@acme.com>", ["Dee Kim <dee@acme.com>"], date="2026-09-20T10:00:00Z")
    p.ingest(tmp_path)
    works = "SELECT g.id, g.last_seen_at, (SELECT count(*) FROM kg.edge_evidence v WHERE v.edge_id = g.id) " \
            "FROM kg.edges g JOIN kg.entities s ON s.id = g.source_entity_id WHERE g.relation_type = 'WORKS_AT' " \
            "AND s.canonical_name = 'Ann Lee'"
    [(edge_id, last, n)] = q(kg, works)
    assert n == 2 and last.day == 20

    assert p.delete_document("gmail", "m2") is not None
    [(same_edge, last, n)] = q(kg, works)
    assert same_edge == edge_id and n == 1 and last.day == 1                    # recomputed from what is left
    assert person(kg, "dee@acme.com") is None and person(kg, "ann@acme.com")

    p.delete_document("gmail", "m1")
    for table in ("documents", "document_versions", "document_sections", "edge_evidence", "edges", "entities",
                  "entity_aliases", "entity_external_ids", "candidate_entities", "candidate_edges"):
        assert count(kg, table) == 0, table
    assert sorted(a for (a,) in q(kg, "SELECT action FROM kg.index_queue")) == ["delete", "delete"]
    assert p.delete_document("gmail", "m1") is None                              # deleting twice is a no-op


def test_delete_takes_the_document_out_of_review_items(kg, atlas_settings, ontology, phase1_corpus):
    p = pipeline(kg, atlas_settings, ontology)
    p.ingest(phase1_corpus)
    rows = q(kg, "SELECT r.id, x->>'document_id', d.source_system, d.source_external_id FROM kg.review_items r, "
                 "jsonb_array_elements(r.examples) x JOIN kg.documents d ON d.id::text = x->>'document_id'")
    assert rows, "the phase 1 corpus raises review items with examples"
    item_id, doc_id, system, external_id = rows[0]
    only_example = q(kg, "SELECT jsonb_array_length(examples) FROM kg.review_items WHERE id = :i", i=item_id)[0][0] == 1
    p.delete_document(system, external_id)
    assert not q(kg, "SELECT 1 FROM kg.review_items WHERE examples @> CAST(:x AS jsonb)",
                 x=json.dumps([{"document_id": doc_id}]))
    status = q(kg, "SELECT status FROM kg.review_items WHERE id = :i", i=item_id)[0][0]
    assert status == ("SOURCE_REMOVED" if only_example else "OPEN")
    detail = q(kg, "SELECT details::text FROM kg.audit_log WHERE action = 'document_deleted'")[0][0]
    assert "body" not in detail and "@" not in detail                             # ids and counts only


def test_property_set_by_a_deleted_document_comes_from_the_next_one(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    write_email(tmp_path, "e1", "m1", "Mike Rodriguez <mr@acme.com>", ["Bob Ray <bob@acme.com>"])
    write_email(tmp_path, "e2", "m2", "Michael Rodriguez <mr@acme.com>", ["Bob Ray <bob@acme.com>"])
    p.ingest(tmp_path)
    assert person(kg, "mr@acme.com").properties["first_name"] == "Mike"           # the first document wins
    p.delete_document("gmail", "m1")
    mr = person(kg, "mr@acme.com")
    assert mr.properties["first_name"] == "Michael"
    assert mr.property_sources["first_name"] == str(document_id_for("gmail", "m2"))
    aliases = {a for (a,) in q(kg, "SELECT alias FROM kg.entity_aliases WHERE entity_id = :e", e=mr.id)}
    assert "Mike Rodriguez" not in aliases and "Michael Rodriguez" in aliases


def test_dropped_property_is_cleared(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    author = {"name": "Ann Lee", "email": "ann@acme.com"}
    write_doc(tmp_path, "plan", "2026-09-10T10:00:00Z", author)
    p.ingest(tmp_path)
    props = lambda: q(kg, "SELECT properties FROM kg.entities WHERE entity_type = 'Document'")[0][0]   # noqa: E731
    assert props()["uri"] == "https://docs.example.com/plan"
    write_doc(tmp_path, "plan", "2026-09-11T10:00:00Z", author, title="Plan v2", uri=None)
    p.ingest(tmp_path)
    assert "uri" not in props() and props()["title"] == "Plan v2"


def test_an_older_version_arriving_late_is_skipped(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    (tmp_path / "new").mkdir()
    (tmp_path / "old").mkdir()
    write_doc(tmp_path / "new", "plan", "2026-09-16T10:00:00Z", {"name": "Priya Shah", "email": "priya@acme.com"})
    write_doc(tmp_path / "old", "plan", "2026-09-10T10:00:00Z", {"name": "Mike Rodriguez", "email": "mike@acme.com"})
    p.ingest(tmp_path / "new")
    report = p.ingest(tmp_path / "old")
    assert report.stats["documents_stale"] == 1
    authored = {t for _, r, t in facts(kg) if r == "AUTHORED_BY"}
    assert authored == {"Priya Shah"} and person(kg, "mike@acme.com") is None


def test_human_decisions_keep_an_orphaned_entity(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"])
    p.ingest(tmp_path)
    bob = person(kg, "bob@acme.com").id
    with Session(kg) as s:
        s.execute(text("INSERT INTO kg.entity_merge_history (id, source_entity_id, target_entity_id, reason, "
                       "algorithm_version) VALUES (:i, :e, :e, 'reviewer confirmed', 'test')"),
                  {"i": uuid.uuid4(), "e": bob})
        s.commit()
    p.delete_document("gmail", "m1")
    assert person(kg, "bob@acme.com").id == bob and person(kg, "ann@acme.com") is None


def test_reprocessing_under_a_new_ontology_drops_what_it_no_longer_produces(kg, atlas_settings, ontology, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    write_email(corpus, "e1", "m1", "Ann Lee <ann@acme.com>", ["Cy Fox <cy@zeta.io>"])
    pipeline(kg, atlas_settings, ontology).ingest(corpus)
    assert ("Cy Fox", "WORKS_AT", "zeta.io") in facts(kg)

    def exclude_zeta(docs):
        docs["validation.yaml"]["email_domain_employment"]["excluded_domains"].append("zeta.io")
    newer = ontology_variant(tmp_path, "1.2", exclude_zeta)
    report = pipeline(kg, atlas_settings, newer).ingest(corpus)
    assert report.stats["documents_processed"] == 1
    assert ("Cy Fox", "WORKS_AT", "zeta.io") not in facts(kg)
    assert not q(kg, "SELECT 1 FROM kg.entity_external_ids WHERE value = 'zeta.io'")
    assert ("Ann Lee", "WORKS_AT", "acme.com") in facts(kg)


def test_prune_deletes_missing_documents_but_refuses_a_mass_delete(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    for i in range(3):
        write_email(tmp_path, f"e{i}", f"m{i}", "Ann Lee <ann@acme.com>", [f"P{i} Q <p{i}@acme.com>"])
    p.ingest(tmp_path)
    (tmp_path / "e2.json").unlink()
    report = p.ingest(tmp_path, prune=True)                                       # 1 of 3 > 5%
    assert report.stats["prune_refused"] == 1 and count(kg, "documents") == 3
    report = p.ingest(tmp_path, prune=True, force_prune=True)
    assert report.stats["documents_deleted"] == 1 and count(kg, "documents") == 2
    assert person(kg, "p2@acme.com") is None

    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "e1.json").unlink()
    report = p.ingest(tmp_path, prune=True, force_prune=True)
    assert report.stats["prune_skipped_failed_files"] == 1 and count(kg, "documents") == 2


def test_review_closed_by_a_removed_source_reopens_when_raised_again(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    write_email(tmp_path, "e1", "m1", "Mike Rodriguez <mr@acme.com>", ["Bob Ray <bob@acme.com>"])
    write_email(tmp_path, "e2", "m2", "Michael Rodriguez <mr@acme.com>", ["Bob Ray <bob@acme.com>"])
    p.ingest(tmp_path)
    conflict = "SELECT status FROM kg.review_items WHERE review_type = 'CONFLICTING_FACT'"
    assert q(kg, conflict) == [("OPEN",)]
    p.delete_document("gmail", "m2")
    assert q(kg, conflict) == [("SOURCE_REMOVED",)]
    write_email(tmp_path, "e3", "m3", "Mikey Rodriguez <mr@acme.com>", ["Bob Ray <bob@acme.com>"])
    (tmp_path / "e2.json").unlink()
    p.ingest(tmp_path)
    assert q(kg, conflict) == [("OPEN",)]


class FakeBrain:
    def __init__(self):
        self.ingested, self.deleted = [], []

    def ensure_ready(self):
        pass

    def ingest(self, docs, force=False):
        self.ingested += docs
        return SimpleNamespace(indexed_documents=len(docs), skipped_documents=0, failed_documents=0,
                               total_chunks=0, failures=[])

    def delete(self, ids):
        self.deleted += ids
        return SimpleNamespace(deleted_documents=len(ids), deleted_chunks=0, failures=[])


def test_index_queue_carries_changes_and_deletions_to_brain(kg, atlas_settings, ontology, tmp_path):
    p = pipeline(kg, atlas_settings, ontology)
    write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"])
    write_doc(tmp_path, "plan", "2026-09-16T10:00:00Z", {"name": "Ann Lee", "email": "ann@acme.com"})
    p.ingest(tmp_path)
    p.delete_document("gmail", "m1")
    brain = FakeBrain()
    with Session(kg) as s:
        stats = drain_queue(s, atlas_settings, brain=brain)
    plan = str(document_id_for("gdrive", "plan-1"))
    assert brain.deleted == [str(document_id_for("gmail", "m1"))]
    assert [d.id for d in brain.ingested] == [plan]
    assert brain.ingested[0].doc_updated_at.day == 16                               # the source's update time
    assert stats["queued"] == 2 and count(kg, "index_queue") == 0
    with Session(kg) as s:
        assert [d.id for chunk in iter_documents(s, ids=[plan]) for d in chunk] == [plan]


def test_garbage_collection_matches_a_graph_built_with_replace(kg, atlas_settings, ontology, tmp_path, monkeypatch):
    p = pipeline(kg, atlas_settings, ontology)
    write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"], cc=["Cy Fox <cy@zeta.io>"])
    p.ingest(tmp_path)
    write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"])
    with monkeypatch.context() as m:                                             # the old, add-only behaviour
        m.setattr(DocumentSweep, "finish", lambda self, *a, **k: {})
        p.ingest(tmp_path)
    assert count(kg, "document_versions") == 2 and person(kg, "cy@zeta.io")
    stats = p.collect_garbage()
    assert stats["documents"] == 1 and stats["versions_removed"] == 1
    assert count(kg, "document_versions") == 1 and person(kg, "cy@zeta.io") is None
    assert not any("Cy Fox" in f for f in facts(kg))


def test_an_email_with_a_huge_subject_is_ingested(kg, atlas_settings, ontology, tmp_path):
    subject = "TIME SHEETS " + "TELEPHONE CALL WITH CLIENT RE DISCOVERY; " * 600
    path = write_email(tmp_path, "e1", "m1", "Ann Lee <ann@acme.com>", ["Bob Ray <bob@acme.com>"])
    payload = json.loads(path.read_text())
    payload["subject"] = subject
    path.write_text(json.dumps(payload))
    report = pipeline(kg, atlas_settings, ontology).ingest(tmp_path)
    assert report.stats["documents_processed"] == 1 and "documents_failed" not in report.stats
