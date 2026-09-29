"""Ontology 1.1 adds ActionItem (+ ASSIGNED_TO, ORIGINATED_IN) and SENT_TO,
without changing 1.0 or its historical records."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from atlas.db.models import Edge, Entity, ReviewItem
from atlas.graph.queries import GraphQueries
from atlas.ontology import MapStatus, load_ontology, map_entity_type, map_relation
from atlas.pipeline import KnowledgeIngestionPipeline
from tests.v1.conftest import ROOT


@pytest.fixture(scope="session")
def ontology_v11():
    return load_ontology(str(ROOT / "ontology" / "v1_1"))


def test_v11_adds_concepts_and_leaves_v10_alone(ontology, ontology_v11):
    assert ontology_v11.version == "1.1" and ontology_v11.checksum != ontology.checksum
    assert "ActionItem" in ontology_v11.entity_types and "ActionItem" not in ontology.entity_types
    for rel in ("SENT_TO", "ASSIGNED_TO", "ORIGINATED_IN"):
        assert rel in ontology_v11.relations and rel not in ontology.relations
    assert map_entity_type(ontology_v11, "task").entity_type == "ActionItem"
    assert map_relation(ontology_v11, "SENT_TO", "Document", "Person").status is MapStatus.MAPPED
    assert map_relation(ontology_v11, "SENT_TO", "Person", "Document").status is MapStatus.TYPE_VIOLATION


def test_v11_puts_action_items_and_recipients_in_the_graph(kg, atlas_settings, ontology_v11, phase1_corpus):
    report = KnowledgeIngestionPipeline(kg, atlas_settings, ontology_v11).ingest(phase1_corpus)
    assert report.stats["documents_processed"] == 6
    with Session(kg) as s:
        q = GraphQueries(s)
        rels = {(g["source_name"], g["relation_type"], g["target_name"]) for g in q.get_edges()}
        assert ("Send the Atlas implementation plan", "ASSIGNED_TO", "Mike Rodriguez") in rels
        assert ("Share security questionnaire", "ASSIGNED_TO", "Tom Becker") in rels
        assert ("Send the Atlas implementation plan", "ORIGINATED_IN", "Atlas rollout kickoff") in rels
        sent = {(g["source_name"], g["target_name"]): g for g in q.get_edges(relation="SENT_TO")}
        assert set(sent) == {("Atlas Rollout", "Mike Rodriguez"), ("Atlas Rollout", "Priya Shah"),
                             ("Re: Atlas Rollout", "Mike Rodriguez"),
                             ("Intro: consultant for the Atlas data migration", "Mike Rodriguez")}
        priya = s.get(Edge, sent[("Atlas Rollout", "Priya Shah")]["id"])
        assert priya.properties == {"recipient_type": "cc"}
        item = q.find_entity(name="Send the Atlas implementation plan")[0]
        assert item["properties"]["status"] == "open" and item["properties"]["playback_url"].endswith("t=322")
        assigned = next(g for g in q.get_outgoing_edges(item["id"]) if g["relation_type"] == "ASSIGNED_TO")
        [ev] = q.get_evidence(assigned["id"])
        assert ev["evidence_text"] == "Send the Atlas implementation plan"
        assert q.list_reviews(review_type="NEW_ONTOLOGY_CANDIDATE") == []
        assert q.stats()["unsupported_edges"] == 0


def test_upgrade_from_v10_to_v11_keeps_history(kg, atlas_settings, ontology, ontology_v11, phase1_corpus):
    KnowledgeIngestionPipeline(kg, atlas_settings, ontology).ingest(phase1_corpus)
    with Session(kg) as s:
        people_before = {e.id for e in s.execute(select(Entity).where(Entity.entity_type == "Person")).scalars()}
        edges_before = {e.id: e.ontology_version for e in s.execute(select(Edge)).scalars()}
    report = KnowledgeIngestionPipeline(kg, atlas_settings, ontology_v11).ingest(phase1_corpus)
    assert report.stats["reviews_auto_resolved_by_ontology"] == 2
    assert report.stats["documents_processed"] == 6           # reprocessed under the new ontology
    assert report.stats["review_items_opened"] == 0            # reprocessing never flags a doc against itself
    with Session(kg) as s:
        items = {r.candidate_payload.get("suggested_relation") or r.candidate_payload.get("candidate_name"): r
                 for r in s.execute(select(ReviewItem).where(
                     ReviewItem.review_type == "NEW_ONTOLOGY_CANDIDATE")).scalars()}
        assert {k: (v.status, v.resolution["mapped_to"]) for k, v in items.items()} == {
            "SENT_TO": ("AUTO_RESOLVED", "SENT_TO"), "ActionItem": ("AUTO_RESOLVED", "ActionItem")}
        people_after = {e.id for e in s.execute(select(Entity).where(Entity.entity_type == "Person")).scalars()}
        assert people_after == people_before                       # same canonical people, no duplicates
        edges = {e.id: e for e in s.execute(select(Edge)).scalars()}
        assert all(edges[i].ontology_version == v == "1.0" for i, v in edges_before.items())  # history intact
        new = [e for i, e in edges.items() if i not in edges_before]
        assert new and all(e.ontology_version == "1.1" for e in new)
        assert {e.relation_type for e in new} == {"SENT_TO", "ASSIGNED_TO", "ORIGINATED_IN"}
