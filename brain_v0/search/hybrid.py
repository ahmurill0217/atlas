"""Hybrid retrieval: vector search finds text, the graph adds structure.

    query -> top chunks (pgvector)
          -> seed entities: mentioned in those chunks + named in the query
          -> 1-hop expansion over relationships
          -> relationships ranked: touches an entity named in the query, relation
             type matches a query term, evidence in retrieved chunks, support
          -> every relationship returned with its evidence (document/chunk/quote)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from brain_v0.embeddings.embedder import Embedder
from brain_v0.graph.models import EntityView, RelationshipView
from brain_v0.graph.traversal import GraphQueries
from brain_v0.normalize import normalize_name
from brain_v0.search.semantic import ChunkHit, search_chunks


@dataclass
class HybridResult:
    query: str
    chunks: list[ChunkHit]
    query_entities: list[EntityView]  # entities named directly in the query
    entities: list[EntityView]
    relationships: list[RelationshipView] = field(default_factory=list)


def entities_named_in(session: Session, query: str) -> list[uuid.UUID]:
    """Entities whose name or alias occurs (as whole words) in the query.
    Longest match wins: in 'KRAS G12C inhibitors', 'KRAS G12C' consumes 'KRAS'."""
    padded = f" {normalize_name(query)} "
    rows = session.execute(
        text("SELECT entity_id, normalized_alias FROM entity_aliases "
             "WHERE length(normalized_alias) > 1 AND position(' ' || normalized_alias || ' ' IN :q) > 0"),
        {"q": padded},
    ).all()
    found: list[uuid.UUID] = []
    for entity_id, alias in sorted(rows, key=lambda r: -len(r.normalized_alias)):
        if f" {alias} " in padded:
            found.append(entity_id)
        elif entity_id not in found:
            continue
        padded = padded.replace(f" {alias} ", " | ")
    return list(dict.fromkeys(found))


def _type_matches_query(relationship_type: str, query_terms: set[str]) -> bool:
    """'targets' matches 'target', 'developed_by' matches 'developed'."""
    return any(len(t) > 3 and len(q) > 3 and (t.startswith(q) or q.startswith(t))
               for t in relationship_type.split("_") for q in query_terms)


def hybrid_search(
    session: Session,
    embedder: Embedder,
    query: str,
    top_k: int = 5,
    hops: int = 1,
    max_relationships: int = 25,
) -> HybridResult:
    queries = GraphQueries(session)
    chunks = search_chunks(session, embedder, query, top_k)
    named = entities_named_in(session, query)
    seeds = set(named) | {uuid.UUID(str(e["id"])) for c in chunks for e in c.entities}
    expanded = queries.expand_entities(seeds, hops)

    # Edges touching a seed (seed <-> seed or seed <-> 1-hop neighbour).
    candidates = [r for r in queries.relationships_between(expanded)
                  if r.source_id in seeds or r.target_id in seeds]
    queries.with_evidence(candidates)
    retrieved = {c.chunk_id for c in chunks}
    named_set = set(named)
    query_terms = set(normalize_name(query).split())

    def rank(rel: RelationshipView) -> tuple:
        in_retrieved = any(ev.chunk_id in retrieved for ev in rel.evidence)
        touches_query = rel.source_id in named_set or rel.target_id in named_set
        type_match = _type_matches_query(rel.relationship_type, query_terms)
        return (not touches_query, not type_match, not in_retrieved, -rel.support_count, -(rel.confidence or 0))

    relationships = sorted(candidates, key=rank)[:max_relationships]
    entity_ids = seeds | {i for r in relationships for i in (r.source_id, r.target_id)}
    return HybridResult(
        query=query,
        chunks=chunks,
        query_entities=queries.get_entities(named),
        entities=queries.get_entities(entity_ids),
        relationships=relationships,
    )
