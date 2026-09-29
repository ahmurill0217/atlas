from sqlalchemy.orm import Session

from brain.embeddings import HashEmbedder
from brain.pipeline.build_brain import build
from brain.pipeline.ingest import ingest_path
from brain.search import hybrid_search, search_chunks
from tests.conftest import FakeLLM
from tests.test_idempotency import RESPONSES, corpus  # noqa: F401  (fixture)


def test_semantic_and_hybrid_search_preserve_provenance(db, settings, corpus):  # noqa: F811
    with Session(db) as s, s.begin():
        ingest_path(s, corpus, settings)
    embedder = HashEmbedder(settings.embedding_dim)
    build(FakeLLM(RESPONSES), embedder, engine=db, settings=settings)

    with Session(db) as s:
        hits = search_chunks(s, embedder, "covalent inhibitor previously known as AMG 510", top_k=2)
        assert hits[0].source_uri.endswith("a.md") and hits[0].score > hits[1].score
        assert {e["name"] for e in hits[0].entities} == {"Sotorasib", "KRAS G12C"}

        result = hybrid_search(s, embedder, "What compounds target KRAS G12C?", top_k=2)
        assert [e.canonical_name for e in result.query_entities] == ["KRAS G12C"]
        top = result.relationships[0]
        assert top.triple() == "Sotorasib -[targets]-> KRAS G12C"
        assert {ev.source_uri.rsplit("/", 1)[-1] for ev in top.evidence} == {"a.md", "b.txt"}
        assert all(ev.evidence_text for ev in top.evidence)


def test_changing_embedder_triggers_reembedding(db, settings, corpus):  # noqa: F811
    from brain.pipeline.build_brain import embed_pending_chunks

    other = HashEmbedder(settings.embedding_dim)
    other.name = "other-model"

    with Session(db) as s, s.begin():
        ingest_path(s, corpus, settings)
        assert embed_pending_chunks(s, HashEmbedder(settings.embedding_dim)) == 2
        assert embed_pending_chunks(s, HashEmbedder(settings.embedding_dim)) == 0
        assert embed_pending_chunks(s, other) == 2
