"""Export the current canonical graph (plus raw extractions) as plain JSON."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from brain_v0.graph.traversal import GraphQueries


def export_snapshot(session: Session, include_raw: bool = True) -> dict:
    q = GraphQueries(session)
    entities = [
        {"name": e.canonical_name, "type": e.entity_type, "aliases": e.aliases,
         "mentions": e.mention_count, "type_votes": e.properties.get("type_votes", {}),
         "ambiguous": e.properties.get("resolution_status") == "ambiguous"}
        for e in q.list_entities()
    ]
    rels = q.with_evidence(q.list_relationships())
    relationships = [
        {"source": r.source_name, "type": r.relationship_type, "target": r.target_name,
         "support": r.support_count, "confidence": r.confidence,
         "evidence": [{"document": ev.source_uri, "chunk_index": ev.chunk_index, "text": ev.evidence_text}
                      for ev in r.evidence]}
        for r in rels
    ]
    snapshot = {"entities": entities, "relationships": relationships}
    rejections = session.execute(text(
        "SELECT kind, decision, method, count(*) AS n FROM resolution_log "
        "GROUP BY kind, decision, method ORDER BY kind, decision, method")).all()
    snapshot["decisions"] = [dict(r._mapping) for r in rejections]
    if include_raw:
        rows = session.execute(text(
            "SELECT d.source_uri, c.chunk_index, x.output FROM chunk_extractions x "
            "JOIN chunks c ON c.id = x.chunk_id JOIN documents d ON d.id = c.document_id "
            "ORDER BY d.source_uri, c.chunk_index")).all()
        snapshot["raw_extractions"] = [{"document": r.source_uri, "chunk_index": r.chunk_index,
                                        "output": r.output} for r in rows]
    return snapshot
