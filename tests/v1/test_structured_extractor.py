import uuid

from atlas.extraction.structured import StructuredExtractor
from atlas.ingestion.adapters import normalize_file
from tests.v1.conftest import CORPUS

V = uuid.uuid4()


def _edges(cs):
    return {(e.source_local_id, e.suggested_relation, e.target_local_id) for e in cs.edges}


def test_email_candidates(ontology):
    doc = normalize_file(CORPUS / "emails" / "2026-09-01_atlas_rollout.json")
    cs = StructuredExtractor(ontology, ["northwind.io"]).extract(doc, V)
    sarah, mike = "person:email:sarah.chen@acme.com", "person:email:mike.rodriguez@northwind.io"
    assert ("document", "AUTHORED_BY", sarah) in _edges(cs)
    assert ("document", "SENT_TO", mike) in _edges(cs)                       # proposed; no V1.0 home
    assert (sarah, "WORKS_AT", "org:domain:acme.com") in _edges(cs)
    orgs = {e.local_id: e.properties for e in cs.entities if e.suggested_type == "Organization"}
    assert orgs["org:domain:northwind.io"]["is_internal"] is True
    assert orgs["org:domain:acme.com"]["is_internal"] is False
    assert all(e.provenance_class == "STRUCTURED_SOURCE" and e.evidence.source_field for e in cs.edges)


def test_generic_domains_never_imply_employment(ontology):
    doc = normalize_file(CORPUS / "emails" / "2026-09-04_gmail_contact.json")
    cs = StructuredExtractor(ontology).extract(doc, V)
    assert not any(e.local_id == "org:domain:gmail.com" for e in cs.entities)
    assert not any(e.suggested_relation == "WORKS_AT" and "gmail" in e.source_local_id for e in cs.edges)


def test_domain_employment_can_be_disabled_by_policy(ontology):
    policies = ontology.policies.model_copy(update={"email_domain_employment": {"enabled": False}})
    strict = ontology.model_copy(update={"policies": policies})
    doc = normalize_file(CORPUS / "emails" / "2026-09-01_atlas_rollout.json")
    assert not any(e.suggested_relation == "WORKS_AT" for e in StructuredExtractor(strict).extract(doc, V).edges)


def test_meeting_candidates_distinguish_invited_from_attended(ontology):
    doc = normalize_file(CORPUS / "meetings" / "2026-09-08_atlas_kickoff.json")
    cs = StructuredExtractor(ontology, ["northwind.io"]).extract(doc, V)
    edges = _edges(cs)
    priya = "person:email:priya.shah@northwind.io"
    assert ("meeting", "HAS_PARTICIPANT", priya) in edges
    assert (priya, "ATTENDED", "meeting") not in edges                 # invited, never spoke
    for who in ("sarah.chen@acme.com", "tom.becker@acme.com", "mike.rodriguez@northwind.io"):
        assert (f"person:email:{who}", "ATTENDED", "meeting") in edges
    assert ("person:email:mike.rodriguez@northwind.io", "MEMBER_OF", "team:ai platform") in edges
    assert sum(e.suggested_type == "ActionItem" for e in cs.entities) == 2
    meeting = next(e for e in cs.entities if e.local_id == "meeting")
    assert meeting.identifiers == {"meeting_external_id": "fathom:120045"} and meeting.properties["is_external"]


def test_extraction_is_deterministic(ontology):
    doc = normalize_file(CORPUS / "meetings" / "2026-09-08_atlas_kickoff.json")
    ex = StructuredExtractor(ontology, ["northwind.io"])
    assert ex.extract(doc, V).model_dump() == ex.extract(doc, V).model_dump()
