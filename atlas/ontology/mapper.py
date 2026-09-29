"""Deterministic ontology mapping: suggestion -> canonical type/relation or UNRESOLVED.

Order for relations:
  1. the suggestion is already a canonical relation valid for these types
  2. a context mapping (mappings.yaml) for exactly these types
  3. a language alias (aliases.yaml), if valid for these types
Anything else is UNRESOLVED (or TYPE_VIOLATION when the relation exists but
not between these types). Nothing is ever invented.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from atlas.ontology.loader import normalize_label, to_relation_key
from atlas.ontology.models import Ontology


class MapStatus(str, Enum):
    MAPPED = "MAPPED"
    UNRESOLVED = "UNRESOLVED"
    TYPE_VIOLATION = "TYPE_VIOLATION"


class TypeMapping(BaseModel):
    suggested: str
    status: MapStatus
    entity_type: str | None = None
    role: str | None = None
    method: str | None = None


class RelationMapping(BaseModel):
    suggested: str
    source_type: str
    target_type: str
    status: MapStatus
    canonical: str | None = None
    method: str | None = None
    reason: str | None = None


def map_entity_type(ontology: Ontology, suggested: str) -> TypeMapping:
    label = normalize_label(suggested)
    for name in ontology.entity_types:
        if normalize_label(name) == label:
            return TypeMapping(suggested=suggested, status=MapStatus.MAPPED, entity_type=name, method="canonical")
    for name, role in ontology.roles.items():
        if normalize_label(name) == label and len(role.applies_to) == 1:
            return TypeMapping(suggested=suggested, status=MapStatus.MAPPED, entity_type=role.applies_to[0],
                               role=name, method="role")
    alias = ontology.entity_type_aliases.get(label)
    if alias:
        return TypeMapping(suggested=suggested, status=MapStatus.MAPPED, entity_type=alias.type,
                           role=alias.role, method="alias")
    return TypeMapping(suggested=suggested, status=MapStatus.UNRESOLVED)


def map_relation(ontology: Ontology, suggested: str, source_type: str, target_type: str) -> RelationMapping:
    base = dict(suggested=suggested, source_type=source_type, target_type=target_type)
    key = to_relation_key(suggested)

    if key in ontology.relations:
        if ontology.relation_allows(key, source_type, target_type):
            return RelationMapping(**base, status=MapStatus.MAPPED, canonical=key, method="canonical")
        # Fall through: a context mapping may still apply (e.g. OWNS is not a relation here).
    for rule in ontology.relation_mappings:
        if (rule.suggested == key and ontology.type_allowed(source_type, rule.source)
                and ontology.type_allowed(target_type, rule.target)):
            return RelationMapping(**base, status=MapStatus.MAPPED, canonical=rule.canonical, method="mapping")
    canonical = ontology.relation_aliases.get(normalize_label(suggested))
    if canonical and ontology.relation_allows(canonical, source_type, target_type):
        return RelationMapping(**base, status=MapStatus.MAPPED, canonical=canonical, method="alias")

    if key in ontology.relations or canonical:
        rel = key if key in ontology.relations else canonical
        return RelationMapping(**base, status=MapStatus.TYPE_VIOLATION, canonical=rel,
                               reason=f"{rel} does not allow {source_type} -> {target_type}")
    return RelationMapping(**base, status=MapStatus.UNRESOLVED,
                           reason=f"no canonical relation for {suggested!r} between {source_type} and {target_type}")
