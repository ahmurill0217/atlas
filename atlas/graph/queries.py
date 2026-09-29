"""Read API over the canonical graph. Callers get plain dicts, never table rows,
so storage can change without touching application code."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.resolution.normalize import normalize_identifier, normalize_name

_ENTITY = """
    SELECT e.id, e.entity_type, e.canonical_name, e.roles, e.properties, e.identity_strength,
           e.ontology_version, e.status,
           COALESCE((SELECT jsonb_object_agg(x.identifier_type, x.value) FROM kg.entity_external_ids x
                     WHERE x.entity_id = e.id), '{}') AS identifiers,
           COALESCE((SELECT array_agg(a.alias ORDER BY a.alias) FROM kg.entity_aliases a
                     WHERE a.entity_id = e.id AND a.normalized_alias <> e.normalized_name), '{}') AS aliases
    FROM kg.entities e
"""

_EDGE = """
    SELECT g.id, g.relation_type, g.provenance_class, g.confidence, g.first_seen_at, g.last_seen_at,
           g.valid_from, g.valid_to, g.ontology_version,
           s.id AS source_id, s.entity_type AS source_type, s.canonical_name AS source_name,
           t.id AS target_id, t.entity_type AS target_type, t.canonical_name AS target_name,
           (SELECT count(*) FROM kg.edge_evidence v WHERE v.edge_id = g.id) AS evidence_count
    FROM kg.edges g
    JOIN kg.entities s ON s.id = g.source_entity_id
    JOIN kg.entities t ON t.id = g.target_entity_id
"""


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


class GraphQueries:
    def __init__(self, session: Session):
        self.session = session

    def get_entity(self, entity_id: uuid.UUID) -> dict | None:
        rows = _rows(self.session.execute(text(_ENTITY + " WHERE e.id = :id"), {"id": entity_id}))
        return rows[0] if rows else None

    def find_entity(self, name: str | None = None, identifier: tuple[str, str] | None = None,
                    entity_type: str | None = None) -> list[dict]:
        where, params = ["e.status = 'active'"], {}
        if identifier:
            where.append("EXISTS (SELECT 1 FROM kg.entity_external_ids x WHERE x.entity_id = e.id "
                         "AND x.identifier_type = :it AND x.value = :iv)")
            params.update(it=identifier[0], iv=normalize_identifier(*identifier))
        if name:
            where.append("(e.normalized_name = :n OR EXISTS (SELECT 1 FROM kg.entity_aliases a "
                         "WHERE a.entity_id = e.id AND a.normalized_alias = :n))")
            params["n"] = normalize_name(name)
        if entity_type:
            where.append("e.entity_type = :t")
            params["t"] = entity_type
        sql = _ENTITY + " WHERE " + " AND ".join(where) + " ORDER BY e.entity_type, e.canonical_name"
        return _rows(self.session.execute(text(sql), params))

    def list_entities(self, entity_type: str | None = None) -> list[dict]:
        return self.find_entity(entity_type=entity_type)

    def get_outgoing_edges(self, entity_id: uuid.UUID) -> list[dict]:
        return _rows(self.session.execute(text(_EDGE + " WHERE g.source_entity_id = :id AND g.status = 'active' "
                                               "ORDER BY g.relation_type, t.canonical_name"), {"id": entity_id}))

    def get_incoming_edges(self, entity_id: uuid.UUID) -> list[dict]:
        return _rows(self.session.execute(text(_EDGE + " WHERE g.target_entity_id = :id AND g.status = 'active' "
                                               "ORDER BY g.relation_type, s.canonical_name"), {"id": entity_id}))

    def get_neighbors(self, entity_id: uuid.UUID) -> list[dict]:
        ids = {e["target_id"] for e in self.get_outgoing_edges(entity_id)} | \
              {e["source_id"] for e in self.get_incoming_edges(entity_id)}
        return [self.get_entity(i) for i in sorted(ids - {entity_id})]

    def get_edges(self, source_type: str | None = None, relation: str | None = None,
                  target_type: str | None = None) -> list[dict]:
        where, params = ["g.status = 'active'"], {}
        for column, key, value in (("s.entity_type", "st", source_type), ("g.relation_type", "r", relation),
                                   ("t.entity_type", "tt", target_type)):
            if value:
                where.append(f"{column} = :{key}")
                params[key] = value
        sql = _EDGE + " WHERE " + " AND ".join(where) + " ORDER BY g.relation_type, s.canonical_name, t.canonical_name"
        return _rows(self.session.execute(text(sql), params))

    def get_evidence(self, edge_id: uuid.UUID) -> list[dict]:
        return _rows(self.session.execute(text(
            """
            SELECT v.provenance_class, v.source_field, v.evidence_text, v.start_offset, v.end_offset,
                   v.page_number, v.observed_at, v.extractor, v.extractor_version, v.confidence,
                   d.id AS document_id, d.source_system, d.source_type, d.source_external_id, d.title, d.uri,
                   v.document_version_id
            FROM kg.edge_evidence v JOIN kg.documents d ON d.id = v.document_id
            WHERE v.edge_id = :id ORDER BY v.observed_at NULLS LAST, d.source_external_id
            """), {"id": edge_id}))

    def explain_edge(self, edge_id: uuid.UUID) -> dict | None:
        """Deterministic replay: evidence -> candidates -> mapping -> decision -> edge."""
        edges = _rows(self.session.execute(text(_EDGE + " WHERE g.id = :id"), {"id": edge_id}))
        if not edges:
            return None
        candidates = _rows(self.session.execute(text(
            """
            SELECT c.local_id, c.decision, c.reason, c.mapping, c.violations, c.payload, c.ingestion_run_id,
                   d.source_system, d.source_external_id
            FROM kg.candidate_edges c
            JOIN kg.document_versions v ON v.id = c.document_version_id
            JOIN kg.documents d ON d.id = v.document_id
            WHERE c.edge_id = :id ORDER BY c.created_at
            """), {"id": edge_id}))
        audit = _rows(self.session.execute(text(
            "SELECT at, actor, action, details FROM kg.audit_log WHERE object_id = :id ORDER BY id"), {"id": edge_id}))
        return {"edge": edges[0], "evidence": self.get_evidence(edge_id), "candidates": candidates, "audit": audit}

    def get_entity_history(self, entity_id: uuid.UUID) -> dict:
        audit = _rows(self.session.execute(text(
            "SELECT at, actor, action, details FROM kg.audit_log WHERE object_id = :id ORDER BY id"), {"id": entity_id}))
        candidates = _rows(self.session.execute(text(
            """
            SELECT c.local_id, c.decision, c.reason, c.mapped_type, d.source_system, d.source_external_id, c.created_at
            FROM kg.candidate_entities c
            JOIN kg.document_versions v ON v.id = c.document_version_id
            JOIN kg.documents d ON d.id = v.document_id
            WHERE c.entity_id = :id ORDER BY c.created_at
            """), {"id": entity_id}))
        return {"audit": audit, "resolutions": candidates}

    def list_reviews(self, status: str | None = "OPEN", review_type: str | None = None) -> list[dict]:
        where, params = ["TRUE"], {}
        if status:
            where.append("status = :s")
            params["s"] = status
        if review_type:
            where.append("review_type = :t")
            params["t"] = review_type
        return _rows(self.session.execute(text(
            "SELECT id, review_type, status, frequency, reason, candidate_payload, examples, related_entities, "
            "created_at FROM kg.review_items WHERE " + " AND ".join(where) +
            " ORDER BY frequency DESC, review_type, created_at"), params))

    def stats(self) -> dict:
        row = self.session.execute(text(
            """
            SELECT (SELECT count(*) FROM kg.documents) AS documents,
                   (SELECT count(*) FROM kg.document_versions) AS document_versions,
                   (SELECT count(*) FROM kg.entities WHERE status = 'active') AS entities,
                   (SELECT count(*) FROM kg.edges WHERE status = 'active') AS edges,
                   (SELECT count(*) FROM kg.edge_evidence) AS evidence,
                   (SELECT count(*) FROM kg.review_items WHERE status = 'OPEN') AS open_reviews,
                   (SELECT count(*) FROM kg.edges g WHERE NOT EXISTS
                       (SELECT 1 FROM kg.edge_evidence v WHERE v.edge_id = g.id)) AS unsupported_edges
            """)).one()
        return dict(row._mapping)
