"""Ontology upgrades: a new version closes the review items it now covers, reprocesses
documents, and leaves records created under the old version untouched. The "old" version
here is the current ontology without SENT_TO, built on the fly."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from atlas.db.models import Edge, Entity, ReviewItem
from atlas.graph.queries import GraphQueries
from atlas.ontology import MapStatus, map_relation
from atlas.pipeline import KnowledgeIngestionPipeline
from tests.conftest import ontology_variant


def _without_sent_to(docs):
    del docs["core.yaml"]["relations"]["SENT_TO"]
    docs["aliases.yaml"]["relations"].pop("SENT_TO", None)


def test_action_items_and_recipients_in_the_graph(kg, atlas_settings, ontology, phase1_corpus):
    report = KnowledgeIngestionPipeline(kg, atlas_settings, ontology).ingest(phase1_corpus)
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
        assert s.get(Edge, sent[("Atlas Rollout", "Priya Shah")]["id"]).properties == {"recipient_type": "cc"}
        item = q.find_entity(name="Send the Atlas implementation plan")[0]
        assert item["properties"]["status"] == "open" and item["properties"]["playback_url"].endswith("t=322")
        assigned = next(g for g in q.get_outgoing_edges(item["id"]) if g["relation_type"] == "ASSIGNED_TO")
        [ev] = q.get_evidence(assigned["id"])
        assert ev["evidence_text"] == "Send the Atlas implementation plan"
        assert q.list_reviews(review_type="NEW_ONTOLOGY_CANDIDATE") == []
        assert q.stats()["unsupported_edges"] == 0


def test_upgrade_closes_gap_reviews_and_keeps_history(kg, atlas_settings, ontology, phase1_corpus, tmp_path):
    old = ontology_variant(tmp_path, "0.9", _without_sent_to)
    assert map_relation(old, "SENT_TO", "Document", "Person").status is MapStatus.UNRESOLVED
    KnowledgeIngestionPipeline(kg, atlas_settings, old).ingest(phase1_corpus)
    with Session(kg) as s:
        gaps = s.execute(select(ReviewItem).where(ReviewItem.review_type == "NEW_ONTOLOGY_CANDIDATE")).scalars().all()
        assert [r.candidate_payload["suggested_relation"] for r in gaps] == ["SENT_TO"]
        people_before = {e.id for e in s.execute(select(Entity).where(Entity.entity_type == "Person")).scalars()}
        edges_before = {e.id: e.ontology_version for e in s.execute(select(Edge)).scalars()}

    report = KnowledgeIngestionPipeline(kg, atlas_settings, ontology).ingest(phase1_corpus)
    assert report.stats["reviews_auto_resolved_by_ontology"] == 1
    assert report.stats["documents_processed"] == 6           # reprocessed under the new ontology
    assert report.stats["review_items_opened"] == 0            # reprocessing never flags a doc against itself
    with Session(kg) as s:
        [gap] = s.execute(select(ReviewItem).where(ReviewItem.review_type == "NEW_ONTOLOGY_CANDIDATE")).scalars()
        assert (gap.status, gap.resolution["mapped_to"]) == ("AUTO_RESOLVED", "SENT_TO")
        people_after = {e.id for e in s.execute(select(Entity).where(Entity.entity_type == "Person")).scalars()}
        assert people_after == people_before                       # same canonical people, no duplicates
        edges = {e.id: e for e in s.execute(select(Edge)).scalars()}
        assert all(edges[i].ontology_version == v == "0.9" for i, v in edges_before.items())  # history intact
        new = [e for i, e in edges.items() if i not in edges_before]
        assert new and {e.relation_type for e in new} == {"SENT_TO"} and all(e.ontology_version == "1.1" for e in new)
