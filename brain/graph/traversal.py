"""Graph queries in plain SQL (no Cypher). Tuned for 1-3 hop traversals:
adjacency lookups hit the (source, target, type) unique index and the target
index; multi-hop uses recursive CTEs with cycle checks and a depth bound."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from brain.graph.models import EntityView, EvidenceView, PathView, RelationshipView
from brain.normalize import normalize_name

_ENTITY_SQL = """
    SELECT e.id, e.canonical_name, e.entity_type, e.description, e.properties,
           COALESCE((SELECT array_agg(a.alias ORDER BY a.alias) FROM entity_aliases a
                     WHERE a.entity_id = e.id AND a.normalized_alias <> e.normalized_name), '{}') AS aliases,
           (SELECT count(*) FROM entity_mentions m WHERE m.entity_id = e.id) AS mention_count
    FROM entities e
"""

_RELATIONSHIP_SQL = """
    SELECT r.id, r.source_entity_id, s.canonical_name AS source_name, r.relationship_type,
           r.target_entity_id, t.canonical_name AS target_name, r.confidence, r.support_count,
           r.description
    FROM relationships r
    JOIN entities s ON s.id = r.source_entity_id
    JOIN entities t ON t.id = r.target_entity_id
"""


def _entity(row) -> EntityView:
    return EntityView(row.id, row.canonical_name, row.entity_type, row.description,
                      list(row.aliases), row.mention_count, row.properties)


def _relationship(row) -> RelationshipView:
    return RelationshipView(row.id, row.source_entity_id, row.source_name, row.relationship_type,
                            row.target_entity_id, row.target_name, row.confidence, row.support_count,
                            row.description)


class GraphQueries:
    def __init__(self, session: Session):
        self.session = session

    # --- entities ---------------------------------------------------------------

    def get_entity(self, entity_id: uuid.UUID) -> EntityView | None:
        row = self.session.execute(text(_ENTITY_SQL + " WHERE e.id = :id"), {"id": entity_id}).first()
        return _entity(row) if row else None

    def get_entities(self, entity_ids: Iterable[uuid.UUID]) -> list[EntityView]:
        rows = self.session.execute(
            text(_ENTITY_SQL + " WHERE e.id = ANY(:ids) ORDER BY e.canonical_name"),
            {"ids": list(entity_ids)},
        )
        return [_entity(r) for r in rows]

    def find_entity(self, name: str, limit: int = 10) -> list[EntityView]:
        """Exact name/alias matches first, then trigram-similar names."""
        rows = self.session.execute(
            text(
                f"""
                WITH hits AS (
                    SELECT a.entity_id AS id,
                           CASE WHEN a.normalized_alias = :n THEN 1.0
                                ELSE similarity(a.normalized_alias, :n) END AS score
                    FROM entity_aliases a
                    WHERE a.normalized_alias = :n OR similarity(a.normalized_alias, :n) > 0.35
                ), best AS (SELECT id, max(score) AS score FROM hits GROUP BY id)
                {_ENTITY_SQL} JOIN best b ON b.id = e.id
                ORDER BY b.score DESC, mention_count DESC LIMIT :limit
                """
            ),
            {"n": normalize_name(name), "limit": limit},
        )
        return [_entity(r) for r in rows]

    def list_entities(self, entity_type: str | None = None, ambiguous_only: bool = False) -> list[EntityView]:
        where = ["TRUE"]
        if entity_type:
            where.append("e.entity_type = :t")
        if ambiguous_only:
            where.append("e.properties->>'resolution_status' = 'ambiguous'")
        rows = self.session.execute(
            text(_ENTITY_SQL + f" WHERE {' AND '.join(where)} ORDER BY e.entity_type, e.canonical_name"),
            {"t": entity_type},
        )
        return [_entity(r) for r in rows]

    # --- relationships -------------------------------------------------------------

    def get_relationships(self, entity_id: uuid.UUID, direction: str = "both") -> list[RelationshipView]:
        cond = {"out": "r.source_entity_id = :id", "in": "r.target_entity_id = :id",
                "both": "(r.source_entity_id = :id OR r.target_entity_id = :id)"}[direction]
        rows = self.session.execute(
            text(_RELATIONSHIP_SQL + f" WHERE {cond} ORDER BY r.support_count DESC, r.relationship_type"),
            {"id": entity_id},
        )
        return [_relationship(r) for r in rows]

    def list_relationships(self, relationship_type: str | None = None) -> list[RelationshipView]:
        rows = self.session.execute(
            text(_RELATIONSHIP_SQL + (" WHERE r.relationship_type = :t" if relationship_type else "")
                 + " ORDER BY s.canonical_name, r.relationship_type, t.canonical_name"),
            {"t": relationship_type},
        )
        return [_relationship(r) for r in rows]

    def relationships_between(self, entity_ids: Iterable[uuid.UUID]) -> list[RelationshipView]:
        """All edges whose two endpoints are both in `entity_ids`."""
        rows = self.session.execute(
            text(_RELATIONSHIP_SQL + " WHERE r.source_entity_id = ANY(:ids) AND r.target_entity_id = ANY(:ids)"
                 " ORDER BY r.support_count DESC, s.canonical_name"),
            {"ids": list(entity_ids)},
        )
        return [_relationship(r) for r in rows]

    def get_evidence(self, relationship_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[EvidenceView]]:
        rows = self.session.execute(
            text(
                """
                SELECT ev.relationship_id, ev.chunk_id, d.title, d.source_uri, c.chunk_index,
                       ev.evidence_text, ev.confidence, ev.extraction_run_id
                FROM relationship_evidence ev
                JOIN chunks c ON c.id = ev.chunk_id
                JOIN documents d ON d.id = c.document_id
                WHERE ev.relationship_id = ANY(:ids)
                ORDER BY d.source_uri, c.chunk_index
                """
            ),
            {"ids": list(relationship_ids)},
        )
        out: dict[uuid.UUID, list[EvidenceView]] = {}
        for r in rows:
            out.setdefault(r.relationship_id, []).append(
                EvidenceView(r.chunk_id, r.title, r.source_uri, r.chunk_index, r.evidence_text,
                             r.confidence, r.extraction_run_id)
            )
        return out

    def with_evidence(self, relationships: list[RelationshipView]) -> list[RelationshipView]:
        evidence = self.get_evidence(r.id for r in relationships)
        for rel in relationships:
            rel.evidence = evidence.get(rel.id, [])
        return relationships

    # --- traversal ------------------------------------------------------------------

    def get_neighbors(self, entity_id: uuid.UUID) -> list[EntityView]:
        rows = self.session.execute(
            text(
                """
                SELECT target_entity_id AS id FROM relationships WHERE source_entity_id = :id
                UNION
                SELECT source_entity_id FROM relationships WHERE target_entity_id = :id
                """
            ),
            {"id": entity_id},
        ).scalars()
        return self.get_entities(set(rows) - {entity_id})

    def expand_entities(self, entity_ids: Iterable[uuid.UUID], depth: int = 1) -> dict[uuid.UUID, int]:
        """Entities within `depth` undirected hops of the seeds -> hop distance."""
        seeds = list(entity_ids)
        if not seeds:
            return {}
        rows = self.session.execute(
            text(
                """
                WITH RECURSIVE frontier(id, depth) AS (
                    SELECT unnest(CAST(:ids AS uuid[])), 0
                    UNION
                    SELECT n.id, f.depth + 1
                    FROM frontier f
                    CROSS JOIN LATERAL (
                        SELECT target_entity_id AS id FROM relationships WHERE source_entity_id = f.id
                        UNION ALL
                        SELECT source_entity_id FROM relationships WHERE target_entity_id = f.id
                    ) n
                    WHERE f.depth < :depth
                )
                SELECT id, min(depth) AS depth FROM frontier GROUP BY id
                """
            ),
            {"ids": seeds, "depth": depth},
        )
        return {r.id: r.depth for r in rows}

    def find_path(
        self, source_id: uuid.UUID, target_id: uuid.UUID, max_depth: int = 3, limit: int = 5
    ) -> list[PathView]:
        """Shortest undirected paths (edge direction is kept in the result)."""
        rows = self.session.execute(
            text(
                """
                WITH RECURSIVE walk(node, nodes, edges, depth) AS (
                    SELECT CAST(:src AS uuid), ARRAY[CAST(:src AS uuid)], ARRAY[]::uuid[], 0
                    UNION ALL
                    SELECT n.other, w.nodes || n.other, w.edges || n.edge_id, w.depth + 1
                    FROM walk w
                    CROSS JOIN LATERAL (
                        SELECT id AS edge_id, target_entity_id AS other FROM relationships WHERE source_entity_id = w.node
                        UNION ALL
                        SELECT id, source_entity_id FROM relationships WHERE target_entity_id = w.node
                    ) n
                    WHERE w.depth < :max_depth AND w.node <> CAST(:tgt AS uuid) AND NOT n.other = ANY(w.nodes)
                )
                SELECT nodes, edges FROM walk WHERE node = CAST(:tgt AS uuid)
                ORDER BY depth LIMIT :limit
                """
            ),
            {"src": source_id, "tgt": target_id, "max_depth": max_depth, "limit": limit},
        ).all()
        paths = []
        for row in rows:
            entities = {e.id: e for e in self.get_entities(row.nodes)}
            rels = {r.id: r for r in self._relationships_by_id(row.edges)}
            paths.append(PathView([entities[i] for i in row.nodes], [rels[i] for i in row.edges]))
        return paths

    def _relationships_by_id(self, ids: list[uuid.UUID]) -> list[RelationshipView]:
        rows = self.session.execute(text(_RELATIONSHIP_SQL + " WHERE r.id = ANY(:ids)"), {"ids": ids})
        return [_relationship(r) for r in rows]

    # --- provenance --------------------------------------------------------------------

    def explain(self, source_name: str, target_name: str, relationship_type: str | None = None) -> list[RelationshipView]:
        """'Why does the system believe X <type> Y?' -> matching edges (either
        direction) with every piece of supporting evidence."""
        sources, targets = self.resolve_name(source_name), self.resolve_name(target_name)
        if not sources or not targets:
            return []
        sql = (_RELATIONSHIP_SQL + " WHERE ((r.source_entity_id = ANY(:s) AND r.target_entity_id = ANY(:t))"
               " OR (r.source_entity_id = ANY(:t) AND r.target_entity_id = ANY(:s)))")
        if relationship_type:
            sql += " AND r.relationship_type = :type"
        rows = self.session.execute(text(sql + " ORDER BY r.support_count DESC"),
                                    {"s": sources, "t": targets, "type": relationship_type})
        return self.with_evidence([_relationship(r) for r in rows])

    def resolve_name(self, name: str) -> list[uuid.UUID]:
        """Exact name/alias matches; the best trigram match only if none exist.
        (Trigram matches alone would let 'CodeBreaK 100' pull in 'CodeBreaK 200'.)"""
        exact = self.session.execute(
            text("SELECT DISTINCT entity_id FROM entity_aliases WHERE normalized_alias = :n"),
            {"n": normalize_name(name)},
        ).scalars().all()
        if exact:
            return list(exact)
        return [e.id for e in self.find_entity(name, limit=1)]

    def stats(self) -> dict[str, int]:
        row = self.session.execute(
            text(
                """
                SELECT (SELECT count(*) FROM documents) AS documents,
                       (SELECT count(*) FROM chunks) AS chunks,
                       (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL) AS chunks_embedded,
                       (SELECT count(*) FROM entities) AS entities,
                       (SELECT count(*) FROM entity_aliases) AS aliases,
                       (SELECT count(*) FROM relationships) AS relationships,
                       (SELECT count(*) FROM relationship_evidence) AS evidence,
                       (SELECT count(*) FROM entity_mentions) AS mentions
                """
            )
        ).one()
        return dict(row._mapping)
