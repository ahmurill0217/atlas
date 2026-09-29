"""Routing and citation plumbing of `atlas ask` (no model calls: the classifier and answerers are stubbed)."""

from atlas.retrieval import router
from atlas.retrieval.ask import GraphContext, LinkedEntity


def _ctx(with_entity: bool = True) -> GraphContext:
    ctx = GraphContext(entities=[LinkedEntity(id="e1", entity_type="Person", name="Keith Holst",
                                              matched="Keith Holst")] if with_entity else [])
    ctx.sources = {"G1": {"entity_id": "x1", "document_id": "doc-1", "title": "ICE Presentation", "date": "2001-05-14"},
                   "G2": {"entity_id": "x2", "document_id": "doc-2", "title": "W basis quotes", "date": "2000-02-09"}}
    return ctx


def test_no_entities_means_content_without_a_model_call():
    decision = router.classify("What happened with the Bishop's Corner partnership?", _ctx(False), settings=None)
    assert decision.route is router.Route.CONTENT


def test_graph_citations_are_the_markers_the_answer_used():
    cites = router.graph_citations("First 2000-02-09 [G2], last 2001-05-14 [G1]; again [G2]. Bogus [G9].", _ctx())
    assert [(c["marker"], c["document_id"], c["source"]) for c in cites] == [("G2", "doc-2", "graph"),
                                                                             ("G1", "doc-1", "graph")]


def test_mixed_questions_keep_both_answers_and_both_kinds_of_source(monkeypatch):
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: _ctx())
    monkeypatch.setattr(router, "classify", lambda q, ctx, st: router.RouteDecision(route=router.Route.MIXED, reason="r"))
    monkeypatch.setattr(router, "graph_answer", lambda q, ctx, st, part=False: "- Keith Holst, last contact 2001-05-14 [G1]")
    monkeypatch.setattr(router, "brain_answer", lambda q, ctx, st, **kw: {
        "answer": "They discussed ICE [[1]]().", "error": None, "tokens": None,
        "citations": [{"marker": "1", "source": "search", "document_id": "doc-9", "title": "RE: ICE", "date": None}]})
    out = router.ask("Prep me for a call with Keith Holst", settings=object())
    assert out["route"] == "mixed"
    assert out["answer"].startswith("**From the relationship graph**") and "**From the emails**" in out["answer"]
    assert [(c["source"], c["document_id"]) for c in out["citations"]] == [("graph", "doc-1"), ("search", "doc-9")]


def test_relationship_route_cites_graph_sources(monkeypatch):
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: _ctx())
    monkeypatch.setattr(router, "graph_answer", lambda q, ctx, st, part=False: "Last contact 2001-05-14 [G1].")
    out = router.ask("When did I last talk to Keith Holst?", mode="relationship", settings=object())
    assert out["route"] == "relationship" and [c["document_id"] for c in out["citations"]] == ["doc-1"]


def test_brain_prompt_carries_facts_without_graph_markers():
    from atlas.retrieval.ask import system_prompt
    ctx = _ctx()
    ctx.facts = "- Keith Holst (Person). last contact 2001-05-14 [G1]; first 2000-02-09 [G2]."
    prompt = system_prompt(ctx)
    assert "last contact 2001-05-14; first 2000-02-09." in prompt and "[G1]" not in prompt
