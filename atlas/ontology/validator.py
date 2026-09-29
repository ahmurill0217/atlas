"""Deterministic schema checks for entities and edges (compile-time type checking)."""

from __future__ import annotations

from pydantic import BaseModel

from atlas.extraction.candidates import EvidenceRef
from atlas.ontology.models import Ontology


class Violation(BaseModel):
    code: str
    message: str


def validate_entity(ontology: Ontology, entity_type: str, name: str | None, identifiers: dict[str, str],
                    properties: dict, roles: list[str]) -> list[Violation]:
    if entity_type not in ontology.entity_types:
        return [Violation(code="UNKNOWN_ENTITY_TYPE", message=f"{entity_type!r} is not in ontology {ontology.version}")]
    out: list[Violation] = []
    if not identifiers and not (name and name.strip()):
        out.append(Violation(code="MISSING_IDENTITY", message="entity has neither identifiers nor a name"))
    allowed_ids = ontology.identity_fields(entity_type)
    for id_type in identifiers:
        if id_type not in allowed_ids:
            out.append(Violation(code="UNKNOWN_IDENTIFIER_TYPE",
                                 message=f"{id_type!r} is not an identity field of {entity_type} ({allowed_ids})"))
    allowed_props = ontology.properties(entity_type)
    for prop in properties:
        if prop not in allowed_props:
            out.append(Violation(code="UNKNOWN_PROPERTY", message=f"{prop!r} is not a property of {entity_type}"))
    for role in roles:
        spec = ontology.roles.get(role)
        if spec is None or not ontology.type_allowed(entity_type, spec.applies_to):
            out.append(Violation(code="ROLE_NOT_APPLICABLE", message=f"role {role!r} does not apply to {entity_type}"))
    return out


def validate_edge(ontology: Ontology, relation: str, source_type: str, target_type: str,
                  same_entity: bool, provenance_class: str, evidence: EvidenceRef | None) -> list[Violation]:
    rel = ontology.relations.get(relation)
    if rel is None:
        return [Violation(code="UNKNOWN_RELATION", message=f"{relation!r} is not in ontology {ontology.version}")]
    out: list[Violation] = []
    if not ontology.type_allowed(source_type, rel.source):
        out.append(Violation(code="SOURCE_TYPE_NOT_ALLOWED", message=f"{relation} source must be {list(rel.source)}, got {source_type}"))
    if not ontology.type_allowed(target_type, rel.target):
        out.append(Violation(code="TARGET_TYPE_NOT_ALLOWED", message=f"{relation} target must be {list(rel.target)}, got {target_type}"))
    if same_entity and not rel.allow_self:
        out.append(Violation(code="SELF_EDGE", message=f"{relation} may not connect an entity to itself"))
    spec = ontology.policies.provenance_classes.get(provenance_class)
    if spec is None:
        out.append(Violation(code="UNKNOWN_PROVENANCE_CLASS", message=f"{provenance_class!r}"))
    elif rel.evidence_required:
        missing = [f for f in spec.get("requires", []) if evidence is None or getattr(evidence, f, None) in (None, "")]
        if missing:
            out.append(Violation(code="MISSING_EVIDENCE", message=f"{provenance_class} evidence requires {missing}"))
    return out
