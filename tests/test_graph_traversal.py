import pytest

from brain_v0.graph.repository import GraphRepository
from brain_v0.graph.traversal import GraphQueries
from tests.conftest import make_chunk


@pytest.fixture
def small_graph(session):
    """Sotorasib -targets-> KRAS G12C -variant_of-> KRAS ; Sotorasib -developed_by-> Amgen ;
    Adagrasib -targets-> KRAS G12C ; isolated: Docetaxel"""
    repo = GraphRepository(session)
    chunk = make_chunk(session, "evidence chunk")
    ids = {n: repo.create_entity(n, t, None) for n, t in [
        ("Sotorasib", "Drug"), ("KRAS G12C", "Mutation"), ("KRAS", "Gene"),
        ("Amgen", "Organization"), ("Adagrasib", "Drug"), ("Docetaxel", "Drug")]}
    repo.add_alias(ids["Sotorasib"], "AMG 510", "extracted_alias")
    for s, t, typ in [("Sotorasib", "KRAS G12C", "targets"), ("KRAS G12C", "KRAS", "variant_of"),
                      ("Sotorasib", "Amgen", "developed_by"), ("Adagrasib", "KRAS G12C", "targets")]:
        rid, _ = repo.upsert_relationship(ids[s], ids[t], typ, None)
        repo.attach_evidence(rid, chunk.id, f"{s} {typ} {t}", 0.9, None, {})
    session.flush()
    return ids


def test_find_and_get_entity(session, small_graph):
    q = GraphQueries(session)
    assert q.find_entity("amg510")[0].canonical_name == "Sotorasib" or q.find_entity("AMG 510")[0].canonical_name == "Sotorasib"
    assert q.find_entity("AMG 510")[0].id == small_graph["Sotorasib"]
    e = q.get_entity(small_graph["Sotorasib"])
    assert e.aliases == ["AMG 510"] and e.entity_type == "Drug"


def test_neighbors_and_relationships(session, small_graph):
    q = GraphQueries(session)
    names = {e.canonical_name for e in q.get_neighbors(small_graph["KRAS G12C"])}
    assert names == {"Sotorasib", "Adagrasib", "KRAS"}
    out = q.get_relationships(small_graph["Sotorasib"], "out")
    assert {r.relationship_type for r in out} == {"targets", "developed_by"}
    assert q.get_relationships(small_graph["Docetaxel"]) == []


def test_find_path_multi_hop_and_direction(session, small_graph):
    q = GraphQueries(session)
    [path] = q.find_path(small_graph["Amgen"], small_graph["KRAS"], max_depth=3)
    assert [e.canonical_name for e in path.entities] == ["Amgen", "Sotorasib", "KRAS G12C", "KRAS"]
    assert path.describe() == "Amgen <-[developed_by]- Sotorasib -[targets]-> KRAS G12C -[variant_of]-> KRAS"
    assert q.find_path(small_graph["Amgen"], small_graph["KRAS"], max_depth=2) == []
    assert q.find_path(small_graph["Amgen"], small_graph["Docetaxel"]) == []


def test_expand_entities_depths(session, small_graph):
    q = GraphQueries(session)
    one = q.expand_entities([small_graph["Sotorasib"]], depth=1)
    assert set(one) == {small_graph[n] for n in ("Sotorasib", "KRAS G12C", "Amgen")}
    two = q.expand_entities([small_graph["Sotorasib"]], depth=2)
    assert two[small_graph["KRAS"]] == 2 and two[small_graph["Adagrasib"]] == 2
    assert small_graph["Docetaxel"] not in two


def test_explain_returns_evidence(session, small_graph):
    [edge] = GraphQueries(session).explain("AMG 510", "KRAS G12C")
    assert edge.triple() == "Sotorasib -[targets]-> KRAS G12C"
    assert edge.evidence[0].evidence_text == "Sotorasib targets KRAS G12C"
    assert edge.evidence[0].source_uri == "doc.md"


def test_explain_does_not_confuse_numbered_names(session, small_graph):
    repo = GraphRepository(session)
    chunk = make_chunk(session, "trials", "t.md")
    for name in ("CodeBreaK 100", "CodeBreaK 200"):
        trial = repo.create_entity(name, "ClinicalTrial", None)
        rid, _ = repo.upsert_relationship(small_graph["Sotorasib"], trial, "evaluated_in", None)
        repo.attach_evidence(rid, chunk.id, f"Sotorasib in {name}", 1.0, None, {})
    [edge] = GraphQueries(session).explain("Sotorasib", "CodeBreaK 100")
    assert edge.target_name == "CodeBreaK 100"
