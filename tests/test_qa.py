from sqlalchemy.orm import Session

from brain_v0.embeddings import HashEmbedder
from brain_v0.pipeline.build_brain import build
from brain_v0.pipeline.ingest import ingest_path
from brain_v0.qa import GeneratedAnswer, ask
from tests.conftest import FakeLLM
from tests.test_idempotency import RESPONSES, corpus  # noqa: F401  (fixture)


class AnsweringLLM:
    model_name = "fake-answerer"

    def __init__(self):
        self.last_user = None

    def generate(self, system, user, schema):
        assert schema is GeneratedAnswer
        self.last_user = user
        return GeneratedAnswer(answer="Sotorasib targets KRAS G12C.", citations=["S1", "[F1]", "F99"], answerable=True)


def test_ask_builds_cited_context_and_checks_citations(db, settings, corpus):  # noqa: F811
    with Session(db) as s, s.begin():
        ingest_path(s, corpus, settings)
    embedder = HashEmbedder(settings.embedding_dim)
    build(FakeLLM(RESPONSES), embedder, engine=db, settings=settings)

    llm = AnsweringLLM()
    with Session(db) as s:
        hybrid = ask(s, embedder, llm, "What targets KRAS G12C?", mode="hybrid", top_k=2)
        assert "[S1]" in llm.last_user and "[F1]" in llm.last_user and 'evidence: "' in llm.last_user
        assert hybrid.citations == ["S1", "F1"] and hybrid.invalid_citations == ["F99"]
        assert [c.kind for c in hybrid.cited()] == ["source", "fact"]

        vector = ask(s, embedder, llm, "What targets KRAS G12C?", mode="vector", top_k=2)
        assert "[F1]" not in llm.last_user and all(c.kind == "source" for c in vector.context)
