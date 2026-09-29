"""Ingest: files -> documents + chunks. Offline and idempotent.

- Unchanged file (same source_uri + content hash): skipped.
- Changed file: its chunks are replaced. Mentions/evidence on the old chunks
  cascade away; the next build prunes edges left without evidence.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from brain_v0.chunking import chunk_text
from brain_v0.config import Settings, get_settings
from brain_v0.db.models import Chunk, Document
from brain_v0.ingestion import ParsedDocument, discover_files, parse_file

NAMESPACE = uuid.UUID("6f1c1f0e-8a4e-4c55-9d7e-2b3f7a1d0c42")


def document_id_for(source_uri: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"document:{source_uri}")


def chunk_id_for(document_id: uuid.UUID, index: int, text: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"chunk:{document_id}:{index}:{hashlib.sha256(text.encode()).hexdigest()}")


@dataclass
class IngestStats:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "path: error"; one bad file must not abort ingest
    chunks_written: int = 0


def ingest_document(session: Session, doc: ParsedDocument, settings: Settings, stats: IngestStats) -> None:
    doc_id = document_id_for(doc.source_uri)
    existing = session.get(Document, doc_id)
    if existing and existing.content_hash == doc.content_hash:
        stats.unchanged.append(doc.source_uri)
        return
    if existing:
        session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        existing.content_hash, existing.title, existing.metadata_ = doc.content_hash, doc.title, doc.metadata
        stats.updated.append(doc.source_uri)
    else:
        session.add(Document(id=doc_id, source_uri=doc.source_uri, title=doc.title,
                             content_hash=doc.content_hash, metadata_=doc.metadata))
        stats.added.append(doc.source_uri)
    session.flush()

    for c in chunk_text(doc.text, settings.chunk_max_tokens, settings.chunk_overlap_sentences):
        session.add(Chunk(
            id=chunk_id_for(doc_id, c.index, c.text), document_id=doc_id, chunk_index=c.index,
            content=c.text,
            metadata_={"start": c.start, "end": c.end, "token_count": c.token_count},
        ))
        stats.chunks_written += 1
    session.flush()


def ingest_path(session: Session, path: str | Path, settings: Settings | None = None) -> IngestStats:
    settings = settings or get_settings()
    stats = IngestStats()
    for file in discover_files(path):
        try:
            doc = parse_file(file)
        except Exception as exc:
            stats.failed.append(f"{file}: {type(exc).__name__}: {exc}")
            continue
        ingest_document(session, doc, settings, stats)
    return stats

