"""End-to-end: ingest + build with a mocked LLM, repeated."""

import pytest
from sqlalchemy.orm import Session

from brain.embeddings import HashEmbedder
from brain.graph.traversal import GraphQueries
from brain.pipeline.build_brain import build
from brain.pipeline.ingest import ingest_path
from tests.conftest import FakeLLM, ent, rel

DOC_A = "# Sotorasib\n\nSotorasib, previously known as AMG 510, is a covalent inhibitor targeting KRAS G12C."
DOC_B = "# News\n\nResearchers note that AMG 510 selectively inhibits KRAS G12C. Amgen developed Lumakras."

RESPONSES = {
    "previously known as AMG 510": {
        "entities": [ent("e1", "Sotorasib", "Drug", ["AMG 510"]), ent("e2", "KRAS G12C", "Mutation")],
        "relationships": [rel("e1", "e2", "targets", "covalent inhibitor targeting KRAS G12C")],
    },
    "selectively inhibits": {
        "entities": [ent("a", "AMG 510", "Drug"), ent("b", "KRAS G12C", "Mutation"),
                     ent("c", "Amgen", "Organization"), ent("d", "Lumakras", "Drug", ["AMG 510"])],
        "relationships": [rel("a", "b", "targets", "AMG 510 selectively inhibits KRAS G12C"),
                          rel("d", "c", "developed_by", "Amgen developed Lumakras")],
    },
}


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "a.md").write_text(DOC_A)
    (tmp_path / "b.txt").write_text(DOC_B)
    (tmp_path / "ignored.docx").write_text("unsupported format")
    return tmp_path


def test_unreadable_file_is_reported_not_fatal(db, settings, corpus):
    (corpus / "broken.pdf").write_text("not really a pdf")
    stats = _ingest(db, corpus, settings)
    assert len(stats.added) == 2 and len(stats.failed) == 1 and "broken.pdf" in stats.failed[0]


def _ingest(db, path, settings):
    with Session(db) as s, s.begin():
        return ingest_path(s, path, settings)


def _snapshot(db):
    with Session(db) as s:
        q = GraphQueries(s)
        return q.stats(), sorted(r.triple() + f" x{r.support_count}" for r in q.list_relationships())


def test_repeated_ingest_and_build_are_idempotent(db, settings, corpus):
    first = _ingest(db, corpus, settings)
    assert len(first.added) == 2 and first.chunks_written == 2
    again = _ingest(db, corpus, settings)
    assert len(again.unchanged) == 2 and again.chunks_written == 0

    llm = FakeLLM(RESPONSES)
    r1 = build(llm, HashEmbedder(settings.embedding_dim), engine=db, settings=settings)
    assert r1.stats["chunks_extracted"] == 2 and not r1.errors
    snap1 = _snapshot(db)
    stats, edges = snap1
    # AMG 510 / Lumakras / Sotorasib collapse into one entity.
    assert stats["entities"] == 3 and stats["chunks_embedded"] == 2
    assert edges == ["Sotorasib -[developed_by]-> Amgen x1", "Sotorasib -[targets]-> KRAS G12C x2"]

    # Second build: everything cached, no LLM calls, identical graph.
    r2 = build(llm, HashEmbedder(settings.embedding_dim), engine=db, settings=settings)
    assert r2.stats["chunks_pending"] == 0 and llm.calls == 2
    assert _snapshot(db) == snap1

    # Forced rebuild re-extracts but constraints keep the graph identical.
    build(llm, HashEmbedder(settings.embedding_dim), engine=db, settings=settings, force=True)
    assert llm.calls == 4
    assert _snapshot(db) == snap1


def test_changed_document_replaces_chunks_and_prunes_stale_edges(db, settings, corpus):
    _ingest(db, corpus, settings)
    llm = FakeLLM(RESPONSES)
    build(llm, None, engine=db, settings=settings)

    (corpus / "b.txt").write_text("# News\n\nNothing relevant any more.")
    stats = _ingest(db, corpus, settings)
    assert len(stats.updated) == 1 and stats.chunks_written == 1
    report = build(llm, None, engine=db, settings=settings)
    assert report.stats["relationships_pruned"] == 1  # developed_by lost its only evidence
    assert report.stats["entities_pruned"] == 1      # Amgen no longer mentioned
    _, edges = _snapshot(db)
    assert edges == ["Sotorasib -[targets]-> KRAS G12C x1"]


def test_replay_reapplies_cached_extractions_without_llm_calls(db, settings, corpus):
    from brain.db.admin import GRAPH_TABLES, truncate

    _ingest(db, corpus, settings)
    llm = FakeLLM(RESPONSES)
    build(llm, None, engine=db, settings=settings)
    before = _snapshot(db)
    truncate(db, [t for t in GRAPH_TABLES if t not in ("chunk_extractions", "extraction_runs")])
    assert _snapshot(db)[0]["relationships"] == 0
    report = build(llm, None, engine=db, settings=settings, replay=True)
    assert report.stats["chunks_replayed"] == 2 and llm.calls == 2
    assert _snapshot(db) == before
