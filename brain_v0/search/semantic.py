"""pgvector chunk retrieval (cosine distance, HNSW index)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from brain_v0.embeddings.embedder import Embedder


@dataclass
class ChunkHit:
    chunk_id: uuid.UUID
    document_title: str | None
    source_uri: str
    chunk_index: int
    text: str
    score: float  # cosine similarity
    entities: list[dict] = field(default_factory=list)  # {id, name, type}


def search_chunks(session: Session, embedder: Embedder, query: str, top_k: int = 10) -> list[ChunkHit]:
    [vector] = embedder.embed([query])
    rows = session.execute(
        text(
            """
            SELECT c.id, d.title, d.source_uri, c.chunk_index, c.content,
                   1 - (c.embedding <=> CAST(:q AS vector)) AS score,
                   COALESCE((
                       SELECT jsonb_agg(DISTINCT jsonb_build_object(
                                  'id', e.id, 'name', e.canonical_name, 'type', e.entity_type))
                       FROM entity_mentions m JOIN entities e ON e.id = m.entity_id
                       WHERE m.chunk_id = c.id), '[]') AS entities
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL
            ORDER BY c.embedding <=> CAST(:q AS vector)
            LIMIT :k
            """
        ),
        {"q": str(vector), "k": top_k},
    )
    return [ChunkHit(r.id, r.title, r.source_uri, r.chunk_index, r.content, float(r.score),
                     sorted(r.entities, key=lambda e: e["name"])) for r in rows]
