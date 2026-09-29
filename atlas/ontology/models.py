"""Typed, immutable view of an ontology version. Built only by the loader."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

WILDCARD = "*"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EntityTypeDef(_Frozen):
    name: str
    module: str
    description: str = ""
    parent: str | None = None
    identity_fields: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()


class RoleDef(_Frozen):
    name: str
    module: str
    description: str = ""
    applies_to: tuple[str, ...]


class RelationDef(_Frozen):
    name: str
    module: str
    description: str = ""
    source: tuple[str, ...]
    target: tuple[str, ...]
    evidence_required: bool = True
    allow_self: bool = False
    max_targets_per_source: int | None = None
    min_targets_per_source: int | None = None
    implies_source_role: str | None = None
    implies_target_role: str | None = None


class TypeAlias(_Frozen):
    type: str
    role: str | None = None


class RelationMappingRule(_Frozen):
    suggested: str
    source: tuple[str, ...]
    target: tuple[str, ...]
    canonical: str


class Policies(_Frozen):
    provenance_classes: dict[str, dict] = Field(default_factory=dict)
    acceptance: dict[str, float] = Field(default_factory=dict)
    structured_trust: dict[str, float] = Field(default_factory=dict)
    email_domain_employment: dict = Field(default_factory=dict)
    email_identity: dict = Field(default_factory=dict)
    resolution: dict[str, float] = Field(default_factory=dict)
    normalization: dict = Field(default_factory=dict)


class Ontology(_Frozen):
    version: str
    checksum: str
    entity_types: dict[str, EntityTypeDef]
    roles: dict[str, RoleDef]
    relations: dict[str, RelationDef]
    entity_type_aliases: dict[str, TypeAlias]  # normalized phrase -> type (+role)
    relation_aliases: dict[str, str]           # normalized phrase -> canonical relation
    relation_mappings: tuple[RelationMappingRule, ...]
    policies: Policies

    # --- type hierarchy ------------------------------------------------------

    def ancestors(self, entity_type: str) -> list[str]:
        """entity_type itself followed by its parents, e.g. Meeting -> [Meeting, Event]."""
        chain, current = [], entity_type
        while current is not None and current in self.entity_types:
            chain.append(current)
            current = self.entity_types[current].parent
        return chain

    def is_a(self, entity_type: str, ancestor: str) -> bool:
        return ancestor == WILDCARD or ancestor in self.ancestors(entity_type)

    def type_allowed(self, entity_type: str, allowed: tuple[str, ...]) -> bool:
        return any(self.is_a(entity_type, a) for a in allowed)

    def identity_fields(self, entity_type: str) -> list[str]:
        """Identity fields of the type and its ancestors (Meeting inherits Event's)."""
        fields: list[str] = []
        for t in self.ancestors(entity_type):
            fields += [f for f in self.entity_types[t].identity_fields if f not in fields]
        return fields

    def properties(self, entity_type: str) -> list[str]:
        props: list[str] = []
        for t in self.ancestors(entity_type):
            props += [p for p in self.entity_types[t].properties if p not in props]
        return props

    def relation_allows(self, relation: str, source_type: str, target_type: str) -> bool:
        rel = self.relations.get(relation)
        return bool(rel) and self.type_allowed(source_type, rel.source) and self.type_allowed(target_type, rel.target)
