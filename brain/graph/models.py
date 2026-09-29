"""Read-side views of the graph. Every relationship view can carry the
evidence that justifies it, so answers stay traceable to source text."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class EntityView:
    id: uuid.UUID
    canonical_name: str
    entity_type: str
    description: str | None = None
    aliases: list[str] = field(default_factory=list)
    mention_count: int = 0
    properties: dict = field(default_factory=dict)


@dataclass
class EvidenceView:
    chunk_id: uuid.UUID
    document_title: str | None
    source_uri: str
    chunk_index: int
    evidence_text: str
    confidence: float | None
    extraction_run_id: uuid.UUID | None


@dataclass
class RelationshipView:
    id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    relationship_type: str
    target_id: uuid.UUID
    target_name: str
    confidence: float | None
    support_count: int
    description: str | None = None
    evidence: list[EvidenceView] = field(default_factory=list)

    def triple(self) -> str:
        return f"{self.source_name} -[{self.relationship_type}]-> {self.target_name}"


@dataclass
class PathView:
    entities: list[EntityView]
    relationships: list[RelationshipView]  # relationships[i] links entities[i] and entities[i+1]

    def describe(self) -> str:
        parts = [self.entities[0].canonical_name]
        for rel, nxt in zip(self.relationships, self.entities[1:]):
            arrow = f"-[{rel.relationship_type}]->" if rel.target_id == nxt.id else f"<-[{rel.relationship_type}]-"
            parts += [arrow, nxt.canonical_name]
        return " ".join(parts)


def to_dict(obj) -> dict:
    return asdict(obj)
