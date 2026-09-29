"""Spec scenarios (§3, §51-53) driven through the compiler with hand-made
candidates, standing in for the text extractors of later phases."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atlas.compiler.compiler import GraphCompiler
from atlas.db.models import Edge, Entity, ReviewItem
from tests.v1.conftest import candidate_set, edge, ent, make_doc


def _compile(session, ontology, doc, vid, entities, edges=()):
    return GraphCompiler(session, ontology).compile(doc, candidate_set(doc, vid, entities, edges))


def _reviews(session, review_type):
    return session.execute(select(ReviewItem).where(ReviewItem.review_type == review_type)).scalars().all()


def test_mapped_relation_is_committed_with_evidence(kg, ontology):
    with Session(kg) as s:
        doc, vid = make_doc(s, "mike-owns")
        report = _compile(s, ontology, doc, vid,
                          [ent("mike", "Person", "Mike Rodriguez", email="mike.rodriguez@northwind.io"),
                           ent("atlas", "Project", "Atlas rollout", project_key="atlas")],
                          [edge(doc, vid, "mike", "atlas", "owns", "Mike Rodriguez will own the implementation")])
        assert report.edges == {"mike|owns|atlas": "ACCEPTED"}
        [e] = s.execute(select(Edge)).scalars().all()
        assert e.relation_type == "OWNS_PROJECT" and e.provenance_class == "EXPLICIT_TEXT"


def test_invented_relation_goes_to_review_not_graph(kg, ontology):
    """§53: 'Sarah is the executive sponsor for Atlas' — no EXECUTIVE_SPONSOR_OF in V1.0."""
    with Session(kg) as s:
        doc, vid = make_doc(s, "sponsor")
        report = _compile(s, ontology, doc, vid,
                          [ent("sarah", "Person", "Sarah Chen", email="sarah.chen@acme.com"),
                           ent("atlas", "Project", "Atlas", project_key="atlas")],
                          [edge(doc, vid, "sarah", "atlas", "EXECUTIVE_SPONSOR_OF",
                                "Sarah is the executive sponsor for Atlas.")])
        assert report.edges == {"sarah|EXECUTIVE_SPONSOR_OF|atlas": "REVIEW"}
        assert s.execute(select(func.count()).select_from(Edge)).scalar_one() == 0
        [item] = _reviews(s, "NEW_ONTOLOGY_CANDIDATE")
        assert item.candidate_payload["suggested_relation"] == "EXECUTIVE_SPONSOR_OF"
        assert item.examples[0]["evidence_text"] == "Sarah is the executive sponsor for Atlas."


def test_impossible_edge_is_rejected(kg, ontology):
    with Session(kg) as s:
        doc, vid = make_doc(s, "bad")
        report = _compile(s, ontology, doc, vid,
                          [ent("d", "Document", "Memo", source_id="x:1"),
                           ent("m", "Meeting", "Sync", meeting_external_id="x:m1")],
                          [edge(doc, vid, "d", "m", "WORKS_AT")])
        assert report.edges == {"d|WORKS_AT|m": "REJECTED"}
        assert _reviews(s, "ONTOLOGY_VIOLATION")


def test_ambiguous_name_is_never_guessed(kg, ontology):
    """§52: several Sarahs and a bare 'Sarah Chen' mention -> review, no Sarah_2."""
    with Session(kg) as s:
        doc, vid = make_doc(s, "sarahs")
        _compile(s, ontology, doc, vid, [ent("a", "Person", "Sarah Chen", email="sarah.chen@acme.com"),
                                         ent("b", "Person", "Sarah Miller", email="sarah.miller@acme.com")])
        doc2, vid2 = make_doc(s, "bare-mention")
        report = _compile(s, ontology, doc2, vid2, [ent("x", "Person", "Sarah Chen")])
        assert report.entities == {"x": "REVIEW"}
        assert s.execute(select(func.count()).select_from(Entity)).scalar_one() == 2
        [item] = _reviews(s, "AMBIGUOUS_ENTITY_MATCH")
        assert len(item.related_entities) == 1


def test_same_identifier_resolves_to_same_entity(kg, ontology):
    """§52: a later mention with the same email resolves; no Mike_2."""
    with Session(kg) as s:
        doc, vid = make_doc(s, "one")
        _compile(s, ontology, doc, vid, [ent("m", "Person", "Mike Rodriguez", email="mike.rodriguez@northwind.io")])
        doc2, vid2 = make_doc(s, "two")
        report = _compile(s, ontology, doc2, vid2, [ent("m", "Person", "Mike", email="MIKE.RODRIGUEZ@northwind.io")])
        assert report.entities == {"m": "MATCHED"}
        assert s.execute(select(func.count()).select_from(Entity)).scalar_one() == 1


def test_low_confidence_and_cardinality_go_to_review(kg, ontology):
    with Session(kg) as s:
        doc, vid = make_doc(s, "org")
        people = [ent("a", "Person", "Ann", email="ann@x.com"), ent("b", "Person", "Bob", email="bob@x.com"),
                  ent("c", "Person", "Cy", email="cy@x.com")]
        report = _compile(s, ontology, doc, vid, people, [
            edge(doc, vid, "a", "b", "REPORTS_TO", "Ann reports to Bob"),
            edge(doc, vid, "a", "c", "REPORTS_TO", "Ann reports to Cy"),
            edge(doc, vid, "b", "c", "MANAGES", "Bob may manage Cy", confidence=0.5)])
        assert report.edges == {"a|REPORTS_TO|b": "ACCEPTED", "a|REPORTS_TO|c": "REVIEW", "b|MANAGES|c": "REVIEW"}
        assert _reviews(s, "CONFLICTING_FACT") and _reviews(s, "LOW_CONFIDENCE_RELATION")


def test_role_is_a_facet_not_a_node(kg, ontology):
    with Session(kg) as s:
        doc, vid = make_doc(s, "roles")
        _compile(s, ontology, doc, vid, [ent("acme", "customer company", "Acme Corp", domain="acme.com"),
                                         ent("nw", "Organization", "Northwind", domain="northwind.io")],
                 [edge(doc, vid, "acme", "nw", "CUSTOMER_OF", "Acme is a customer of Northwind")])
        orgs = {e.canonical_name: e for e in s.execute(select(Entity)).scalars()}
        assert len(orgs) == 2 and orgs["Acme Corp"].roles == ["Customer"]


def test_reprocessing_a_name_only_mention_is_not_ambiguous(kg, ontology):
    with Session(kg) as s:
        doc, vid = make_doc(s, "guest")
        assert _compile(s, ontology, doc, vid, [ent("g", "Person", "Guest 1")]).entities == {"g": "CREATED"}
        _, vid2 = make_doc(s, "guest")          # same document, reprocessed (new version)
        assert _compile(s, ontology, doc, vid2, [ent("g", "Person", "Guest 1")]).entities == {"g": "MATCHED"}
        other, vid3 = make_doc(s, "other-doc")  # same bare name elsewhere: still never guessed
        assert _compile(s, ontology, other, vid3, [ent("g", "Person", "Guest 1")]).entities == {"g": "REVIEW"}
        assert s.execute(select(Entity)).scalars().one().identity_strength == "name_only"


def test_source_ids_are_case_sensitive_but_emails_are_not(kg, ontology):
    with Session(kg) as s:
        doc, vid = make_doc(s, "ids")
        report = _compile(s, ontology, doc, vid, [
            ent("a", "Document", "Plan A", source_id="gdrive:1AbC"), ent("b", "Document", "Plan B", source_id="gdrive:1abc"),
            ent("p", "Person", "Pat", email="Pat@X.com"), ent("q", "Person", "Pat Q", email="pat@x.COM")])
        assert report.entities == {"a": "CREATED", "b": "CREATED", "p": "CREATED", "q": "MATCHED"}


def test_same_source_supersedes_its_own_property_but_others_conflict(kg, ontology):
    with Session(kg) as s:
        doc, vid = make_doc(s, "self")
        acme = ent("o", "Organization", "Acme", domain="acme.com")
        acme.properties = {"organization_type": "customer"}
        _compile(s, ontology, doc, vid, [acme])
        acme2 = acme.model_copy(update={"properties": {"organization_type": "enterprise customer"}})
        _, vid2 = make_doc(s, "self")            # same document, newer version / pipeline
        _compile(s, ontology, doc, vid2, [acme2])
        assert s.execute(select(Entity)).scalar_one().properties["organization_type"] == "enterprise customer"
        assert not _reviews(s, "CONFLICTING_FACT")
        other, vid3 = make_doc(s, "other")      # a different source disagrees
        _compile(s, ontology, other, vid3, [acme.model_copy(update={"properties": {"organization_type": "vendor"}})])
        assert s.execute(select(Entity)).scalar_one().properties["organization_type"] == "enterprise customer"
        assert _reviews(s, "CONFLICTING_FACT")


def test_governance_decision_on_review_is_audited(kg, ontology):
    from atlas.db.models import AuditLog
    from atlas.review.service import resolve_review

    with Session(kg) as s:
        doc, vid = make_doc(s, "gov")
        _compile(s, ontology, doc, vid, [ent("a", "Person", "A", email="a@x.com"), ent("b", "Project", "B", project_key="b")],
                 [edge(doc, vid, "a", "b", "EXECUTIVE_SPONSOR_OF")])
        [item] = _reviews(s, "NEW_ONTOLOGY_CANDIDATE")
        resolve_review(s, item.id, "REJECTED", "angel", "not modelled in V1")
        assert item.status == "REJECTED" and item.resolution["note"] == "not modelled in V1"
        assert s.execute(select(AuditLog).where(AuditLog.action == "review_rejected")).scalar_one().actor == "angel"
