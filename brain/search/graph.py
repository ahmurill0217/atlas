"""Entity-centric lookup: name -> entity -> neighbourhood with evidence."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from brain.graph.models import EntityView, RelationshipView
from brain.graph.traversal import GraphQueries


@dataclass
class EntityNeighbourhood:
    entity: EntityView
    relationships: list[RelationshipView]
    other_matches: list[EntityView]


def graph_lookup(session: Session, name: str, depth: int = 1) -> EntityNeighbourhood | None:
    queries = GraphQueries(session)
    matches = queries.find_entity(name)
    if not matches:
        return None
    entity = matches[0]
    ids = queries.expand_entities([entity.id], depth)
    if depth == 1:
        rels = queries.get_relationships(entity.id)
    else:
        rels = queries.relationships_between(ids)
    return EntityNeighbourhood(entity, queries.with_evidence(rels), matches[1:])
