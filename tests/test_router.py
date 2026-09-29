"""Routing and citation plumbing of `atlas ask` (no model calls: the classifier and answerers are stubbed)."""

from atlas.retrieval import router
from atlas.retrieval.ask import GraphContext, LinkedEntity


def _ctx(with_entity: bool = True) -> GraphContext:
    ctx = GraphContext(entities=[LinkedEntity(id="e1", entity_type="Person", name="Keith Holst",
                                              matched="Keith Holst")] if with_entity else [])
    ctx.sources = {"G1": {"entity_id": "x1", "document_id": "doc-1", "title": "ICE Presentation", "date": "2001-05-14"},
                   "G2": {"entity_id": "x2", "document_id": "doc-2", "title": "W basis quotes", "date": "2000-02-09"}}
    return ctx


def _read(route, people=(), organizations=()):
    return lambda q, st: router.Understanding(route=route, reason="r", people=list(people),
                                              organizations=list(organizations))


def _never(*a, **kw):
    raise AssertionError("must not be called")


def _brain(q, ctx, st, **kw):
    return {"answer": "They discussed ICE [[1]]().", "error": None, "tokens": None,
            "citations": [{"marker": "1", "source": "search", "document_id": "doc-9", "title": "RE: ICE", "date": None}]}


def test_an_ambiguous_name_is_asked_about_not_answered(monkeypatch):
    ctx = GraphContext(ambiguous=[{"mention": "Sarah", "type": "Person", "relative_to_asker": True, "candidates": [
        {"name": "Sarah Chen", "email": "sarah.chen@acme.com", "organization": "acme.com", "emails": 4},
        {"name": "Sarah Park", "email": "sarah.park@globex.com", "organization": "globex.com", "emails": 3}]}])
    monkeypatch.setattr(router, "understand", _read(router.Route.MIXED))
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: ctx)
    monkeypatch.setattr(router, "graph_answer", _never)
    monkeypatch.setattr(router, "brain_answer", _never)
    out = router.ask("Prep me for my call with Sarah", settings=object(), asker="me@x.com")
    assert out["route"] == "clarify" and out["clarify"][0]["candidates"][1]["name"] == "Sarah Park"
    assert out["answer"].startswith('Which person do you mean by "Sarah"?')
    assert "1. Sarah Chen <sarah.chen@acme.com>, acme.com (4 emails with you)" in out["answer"]


def test_nobody_linked_falls_back_to_content_and_says_so(monkeypatch):
    monkeypatch.setattr(router, "understand", _read(router.Route.MIXED))
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: _ctx(False))
    monkeypatch.setattr(router, "graph_answer", _never)
    monkeypatch.setattr(router, "brain_answer", _brain)
    out = router.ask("How did the Bishop's Corner buyout go?", settings=object())
    assert out["route"] == "content" and out["fallback"].startswith("nobody the question names")
    assert out["answer"] == "They discussed ICE [[1]]()."          # a search answer, no graph notes


def test_a_relationship_question_about_unknown_people_says_so(monkeypatch):
    ctx = _ctx(False)
    ctx.unknown = ['No one called "Zed" is in the relationship graph.']
    monkeypatch.setattr(router, "understand", _read(router.Route.RELATIONSHIP))
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: ctx)
    monkeypatch.setattr(router, "graph_answer", _never)
    monkeypatch.setattr(router, "brain_answer", _brain)
    out = router.ask("When did I last email Zed?", settings=object())
    assert out["answer"].startswith('_Note: No one called "Zed" is in the relationship graph._\n'
                                    "_Note: Answered from search: nobody the question names")


def test_graph_that_cannot_answer_falls_back_to_content(monkeypatch):
    monkeypatch.setattr(router, "understand", _read(router.Route.RELATIONSHIP))
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: _ctx())
    monkeypatch.setattr(router, "graph_answer",
                        lambda q, ctx, st, part=False: router.GraphAnswer(answer="The facts do not say.", answered=False))
    monkeypatch.setattr(router, "brain_answer", _brain)
    out = router.ask("Who did Keith Holst introduce us to?", settings=object())
    assert out["route"] == "content" and "do not answer" in out["fallback"]
    assert [c["document_id"] for c in out["citations"]] == ["doc-9"]


def test_assumptions_are_stated_above_the_answer(monkeypatch):
    ctx = _ctx()
    ctx.notes = ['Took "Keith" to mean Keith Holst <keith.holst@enron.com>; ask again with a full name if not.']
    monkeypatch.setattr(router, "understand", _read(router.Route.RELATIONSHIP))
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: ctx)
    monkeypatch.setattr(router, "graph_answer",
                        lambda q, ctx, st, part=False: router.GraphAnswer(answer="Last 2001-05-14 [G1].", answered=True))
    out = router.ask("When did I last talk to Keith?", settings=object())
    assert out["answer"] == f"_Note: {ctx.notes[0]}_\n\nLast 2001-05-14 [G1]."


def test_graph_citations_are_the_markers_the_answer_used():
    cites = router.graph_citations("First 2000-02-09 [G2], last 2001-05-14 [G1]; again [G2]. Bogus [G9].", _ctx())
    assert [(c["marker"], c["document_id"], c["source"]) for c in cites] == [("G2", "doc-2", "graph"),
                                                                             ("G1", "doc-1", "graph")]


def test_mixed_questions_keep_both_answers_and_both_kinds_of_source(monkeypatch):
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: _ctx())
    monkeypatch.setattr(router, "understand", _read(router.Route.MIXED))
    monkeypatch.setattr(router, "graph_answer", lambda q, ctx, st, part=False: router.GraphAnswer(
        answer="- Keith Holst, last contact 2001-05-14 [G1]", answered=True))
    monkeypatch.setattr(router, "brain_answer", _brain)
    out = router.ask("Prep me for a call with Keith Holst", settings=object())
    assert out["route"] == "mixed"
    assert out["answer"].startswith("**From the relationship graph**") and "**From the emails**" in out["answer"]
    assert [(c["source"], c["document_id"]) for c in out["citations"]] == [("graph", "doc-1"), ("search", "doc-9")]


def test_relationship_route_cites_graph_sources(monkeypatch):
    monkeypatch.setattr(router, "understand", _read(router.Route.CONTENT))   # the mode overrides the route
    monkeypatch.setattr(router, "graph_context", lambda s, q, **kw: _ctx())
    monkeypatch.setattr(router, "graph_answer", lambda q, ctx, st, part=False: router.GraphAnswer(
        answer="Last contact 2001-05-14 [G1].", answered=True))
    out = router.ask("When did I last talk to Keith Holst?", mode="relationship", settings=object())
    assert out["route"] == "relationship" and [c["document_id"] for c in out["citations"]] == ["doc-1"]


def test_brain_prompt_carries_facts_without_graph_markers():
    from atlas.retrieval.ask import system_prompt
    ctx = _ctx()
    ctx.facts = "- Keith Holst (Person). last contact 2001-05-14 [G1]; first 2000-02-09 [G2]."
    prompt = system_prompt(ctx)
    assert "last contact 2001-05-14; first 2000-02-09." in prompt and "[G1]" not in prompt
