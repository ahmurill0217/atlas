import uuid

from sqlalchemy import select

from brain_v0.db.models import Relationship, RelationshipEvidence, ResolutionLog
from brain_v0.extraction.extractor import ExtractionResult
from brain_v0.extraction.models import ExtractedRelationship
from brain_v0.pipeline.build_brain import apply_extraction
from brain_v0.resolution.relationship_resolver import RelationshipValidator, ResolvedEndpoint
from tests.conftest import ent, graph, make_chunk, rel

CHUNK_1 = "Sotorasib, previously known as AMG 510, is a covalent inhibitor targeting KRAS G12C."
CHUNK_2 = "Researchers note that AMG 510 selectively inhibits KRAS G12C while sparing wild-type KRAS."
SOTO, G12C = uuid.uuid4(), uuid.uuid4()
ENDPOINTS = {
    "s": ResolvedEndpoint(SOTO, "Sotorasib", ("Sotorasib", "AMG 510")),
    "t": ResolvedEndpoint(G12C, "KRAS G12C", ("KRAS G12C",)),
}


def _rel(**kw) -> ExtractedRelationship:
    return ExtractedRelationship(**{**rel("s", "t", "targets", "covalent inhibitor targeting KRAS G12C"), **kw})


def test_valid_relationship_accepted(settings):
    d = RelationshipValidator(settings).validate(_rel(), ENDPOINTS, CHUNK_1)
    assert d.accepted and d.source_id == SOTO and d.target_id == G12C
    assert d.details["target_mentioned"] == "in_quote"
    assert d.details["source_mentioned"] == "in_chunk"  # tolerated by default


def test_strict_mode_requires_endpoints_in_quote(settings):
    strict = settings.model_copy(update={"evidence_require_endpoints_in_quote": True})
    d = RelationshipValidator(strict).validate(_rel(), ENDPOINTS, CHUNK_1)
    assert not d.accepted and d.reason == "source_not_in_evidence"


def test_rejections(settings):
    v = RelationshipValidator(settings)
    assert v.validate(_rel(evidence=None), ENDPOINTS, CHUNK_1).reason == "missing_evidence"
    assert v.validate(_rel(evidence="Sotorasib cures all cancers."), ENDPOINTS, CHUNK_1).reason == "evidence_not_in_source"
    assert v.validate(_rel(target_local_id="s"), ENDPOINTS, CHUNK_1).reason == "self_relationship"
    assert v.validate(_rel(target_local_id="zz"), ENDPOINTS, CHUNK_1).reason == "unresolved_endpoint"
    other = {**ENDPOINTS, "t": ResolvedEndpoint(uuid.uuid4(), "EGFR", ("EGFR",))}
    assert v.validate(_rel(), other, CHUNK_1).reason == "target_not_in_chunk"


def test_type_normalization_and_inverse(settings):
    cfg = settings.model_copy(update={"relationship_type_inverses": {"targeted_by": "targets"},
                                      "allowed_relationship_types": ["targets"]})
    v = RelationshipValidator(cfg)
    d = v.validate(_rel(source_local_id="t", target_local_id="s", relationship_type="Targeted By"), ENDPOINTS, CHUNK_1)
    assert d.accepted and d.relationship_type == "targets" and d.source_id == SOTO and d.details["flipped"]
    assert v.validate(_rel(relationship_type="binds"), ENDPOINTS, CHUNK_1).reason == "type_not_allowed"


def _extract(chunk_text, evidence):
    return ExtractionResult(graph(
        [ent("e1", "Sotorasib", "Drug", ["AMG 510"]), ent("e2", "KRAS G12C", "Mutation")],
        [rel("e1", "e2", "targets", evidence)],
    ))


def test_duplicate_edges_prevented_and_evidence_accumulates(session, settings):
    c1 = make_chunk(session, CHUNK_1, "a.md")
    c2 = make_chunk(session, CHUNK_2, "b.md")
    s1 = apply_extraction(session, c1, _extract(CHUNK_1, "covalent inhibitor targeting KRAS G12C"), None, settings)
    s2 = apply_extraction(session, c2, _extract(CHUNK_2, "AMG 510 selectively inhibits KRAS G12C"), None, settings)
    assert s1["relationships_created"] == 1 and s2["relationships_evidence_attached"] == 1

    edge = session.scalars(select(Relationship)).one()
    assert edge.relationship_type == "targets" and edge.support_count == 2
    evidence = session.scalars(select(RelationshipEvidence).order_by(RelationshipEvidence.evidence_text)).all()
    assert {e.chunk_id for e in evidence} == {c1.id, c2.id}
    assert evidence[0].evidence_text == "AMG 510 selectively inhibits KRAS G12C"
    assert evidence[0].details["quote_start"] == CHUNK_2.index("AMG 510 selectively")

    # Re-applying the same chunk is a no-op for edges and evidence.
    s3 = apply_extraction(session, c1, _extract(CHUNK_1, "covalent inhibitor targeting KRAS G12C"), None, settings)
    assert s3["relationships_duplicate_evidence"] == 1
    assert session.scalars(select(Relationship)).one().support_count == 2
    assert len(session.scalars(select(RelationshipEvidence)).all()) == 2


def test_rejected_relationships_are_logged_not_persisted(session, settings):
    chunk = make_chunk(session, CHUNK_1)
    stats = apply_extraction(session, chunk, _extract(CHUNK_1, "Sotorasib is approved in Mars."), None, settings)
    assert stats["relationships_rejected:evidence_not_in_source"] == 1
    assert session.scalars(select(Relationship)).all() == []
    log = session.scalars(select(ResolutionLog).where(ResolutionLog.kind == "relationship")).one()
    assert log.decision == "REJECTED" and log.method == "evidence_not_in_source"


def test_plural_mentions_count_as_mentions(settings):
    chunk = "Lung adenocarcinomas are a subtype of non-small cell lung cancer."
    endpoints = {"s": ResolvedEndpoint(uuid.uuid4(), "lung adenocarcinoma", ("lung adenocarcinoma",)),
                 "t": ResolvedEndpoint(uuid.uuid4(), "non-small cell lung cancer", ("NSCLC", "non-small cell lung cancer"))}
    d = RelationshipValidator(settings).validate(_rel(evidence=chunk, relationship_type="subtype_of"), endpoints, chunk)
    assert d.accepted and d.details["source_mentioned"] == "in_quote"


def test_endpoint_substituted_by_same_type_entity_in_quote_is_rejected(settings):
    chunk = "Researchers note that AMG 510 selectively inhibits KRAS G12C. Adagrasib followed later."
    ada, soto, g12c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    endpoints = {
        "a": ResolvedEndpoint(ada, "adagrasib", ("adagrasib",), "Drug"),
        "s": ResolvedEndpoint(soto, "Sotorasib", ("Sotorasib", "AMG 510"), "Drug"),
        "t": ResolvedEndpoint(g12c, "KRAS G12C", ("KRAS G12C",), "Mutation"),
    }
    v = RelationshipValidator(settings)
    d = v.validate(_rel(source_local_id="a", evidence="AMG 510 selectively inhibits KRAS G12C"), endpoints, chunk)
    assert not d.accepted and d.reason == "source_substituted_in_evidence"
    assert d.details["source_substituted_by"] == ["Sotorasib"]
    # The right drug is accepted; pronoun-style quotes naming no rival are too.
    assert v.validate(_rel(source_local_id="s", evidence="AMG 510 selectively inhibits KRAS G12C"), endpoints, chunk).accepted
