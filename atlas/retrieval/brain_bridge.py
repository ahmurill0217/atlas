"""Atlas documents -> the `brain` library (OpenSearch hybrid search + cited answers).

Atlas owns documents, versions and the graph; brain owns chunks, embeddings and
the index. They share one id: brain's Document.id is Atlas's document UUID, so a
brain citation resolves straight back to an Atlas document and its graph facts.

What is indexed per document:
  - a header section: From / To / Cc / Date / Subject, with people named by their
    graph canonical name, so the model reading a chunk knows who wrote what;
  - the document's own text. For email that is the new text only: the quoted
    reply chain is somebody else's earlier message, indexed with that message.
    A pure forward (no text of its own) keeps its forwarded content.

Keeping the index current: every graph change queues its document in kg.index_queue
('upsert' or 'delete', same transaction), and `drain_queue` applies the queue to brain,
clearing an entry only after brain confirmed it and only if it was not queued again meanwhile.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from atlas.config import ROOT, Settings

HEADER_ROLES = [("sender", "From"), ("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"), ("invitee", "Invitees")]


def build_brain(settings: Settings):
    """A brain.Brain on the local stack (OpenSearch :9201, model server :9100)."""
    from brain import Brain, BrainSettings

    store = settings.brain_store_url or f"sqlite:///{ROOT}/runs/brain_store_{settings.brain_index}.db"
    llm = {}
    if settings.openai_api_key:
        llm = dict(llm_provider=settings.ask_provider, llm_model=settings.ask_model,
                   llm_api_key=settings.openai_api_key)
    return Brain.from_settings(BrainSettings(
        opensearch_port=settings.brain_opensearch_port, model_server_port=settings.brain_model_server_port,
        opensearch_index_name=settings.brain_index, opensearch_num_replicas=0, document_store_url=store, **llm))


def _names_by_email(s: Session) -> dict[str, str]:
    rows = s.execute(text("""SELECT x.value, e.canonical_name FROM kg.entity_external_ids x
                             JOIN kg.entities e ON e.id = x.entity_id
                             WHERE x.identifier_type = 'email' AND e.status = 'active'"""))
    return {r.value: r.canonical_name for r in rows}


def _person(p: dict, names: dict[str, str]) -> str:
    email = p.get("email")
    name = names.get(email) or p.get("name")
    return f"{name} <{email}>" if name and email and name != email.split("@")[0] else (email or name or "?")


def to_brain_document(row, names: dict[str, str]):
    from brain import Document, TextSection

    n = row.normalized
    header = []
    for role, label in HEADER_ROLES:
        people = [_person(p, names) for p in n.get("participants", []) if p.get("role") == role]
        if people:
            header.append(f"{label}: {', '.join(people)}")
    if row.source_created_at:
        header.append(f"Date: {row.source_created_at:%Y-%m-%d %H:%M} UTC")
    title = row.title or ("(no subject)" if row.source_type == "email" else row.source_external_id)
    header.append(f"{'Subject' if row.source_type == 'email' else 'Title'}: {title}")
    own = [sec["text"] for sec in n.get("sections", []) if not (sec.get("metadata") or {}).get("quoted")]
    if not any(t.strip() for t in own):                 # pure forward: its forwarded content is the message
        own = [sec["text"] for sec in n.get("sections", [])]
    participants = sorted({p["email"] for p in n.get("participants", []) if p.get("email")})
    return Document(
        id=str(row.id), source=row.source_system, semantic_identifier=title, title=title,
        doc_created_at=row.source_created_at,
        # brain re-indexes a document only when this advances, so it must be the source's update time
        doc_updated_at=row.source_updated_at or row.source_created_at,
        metadata={"source_type": row.source_type, "participants": participants},
        sections=[TextSection(text="\n".join(header))] + [TextSection(text=t) for t in own if t.strip()])


def iter_documents(s: Session, batch: int = 64, ids: list[str] | None = None) -> Iterator[list]:
    names = _names_by_email(s)
    rows = s.execute(text(f"""SELECT d.id, d.source_system, d.source_type, d.source_external_id, d.title,
                                     d.source_created_at, d.source_updated_at, v.normalized
                              FROM kg.documents d JOIN kg.document_versions v ON v.id = d.current_version_id
                              {"WHERE d.id::text = ANY(:ids)" if ids is not None else ""}
                              ORDER BY d.id"""), {"ids": ids} if ids is not None else {}).yield_per(batch)
    chunk = []
    for row in rows:
        chunk.append(to_brain_document(row, names))
        if len(chunk) == batch:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def index_all(s: Session, settings: Settings, force: bool = False, progress=None, brain=None) -> dict:
    """Every document, then the queue (a full pass does not see deletions)."""
    brain = brain or build_brain(settings)
    brain.ensure_ready()
    stats = {"documents": 0, "indexed": 0, "skipped": 0, "failed": 0, "chunks": 0, "seconds": 0.0}
    failures: list[str] = []
    t0 = time.perf_counter()
    for docs in iter_documents(s):
        result = brain.ingest(docs, force=force)
        stats["documents"] += len(docs)
        stats["indexed"] += result.indexed_documents
        stats["skipped"] += result.skipped_documents
        stats["failed"] += result.failed_documents
        stats["chunks"] += result.total_chunks
        failures += [f"{f.document_id}: {f.failure_message}" for f in result.failures]
        if progress:
            progress(stats)
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    stats["failures"] = failures[:20]
    stats["queue"] = drain_queue(s, settings, brain=brain)
    return stats


def drain_queue(s: Session, settings: Settings, brain=None, batch: int = 64) -> dict:
    """Apply kg.index_queue to brain: index upserted documents, drop deleted ones."""
    queued = s.execute(text("SELECT document_id::text AS id, action, enqueued_at FROM kg.index_queue "
                            "ORDER BY enqueued_at")).all()
    stats = {"queued": len(queued), "indexed": 0, "deleted": 0, "failed": 0}
    if not queued:
        return stats
    brain = brain or build_brain(settings)
    brain.ensure_ready()
    when = {r.id: r.enqueued_at for r in queued}

    def done(ids: list[str]) -> None:                     # unless queued again meanwhile
        for i in ids:
            s.execute(text("DELETE FROM kg.index_queue WHERE document_id = CAST(:i AS uuid) AND enqueued_at = :t"),
                      {"i": i, "t": when[i]})
        s.commit()

    deletes = [r.id for r in queued if r.action == "delete"]
    if deletes:
        result = brain.delete(deletes)
        failed = {f.document_id for f in result.failures}
        stats["deleted"] += result.deleted_documents
        stats["failed"] += len(failed)
        done([i for i in deletes if i not in failed])
    upserts = [r.id for r in queued if r.action == "upsert"]
    for start in range(0, len(upserts), batch):
        ids = upserts[start:start + batch]
        docs = [d for chunk in iter_documents(s, ids=ids) for d in chunk]
        failed = set()
        if docs:
            result = brain.ingest(docs)
            failed = {f.document_id for f in result.failures}
            stats["indexed"] += result.indexed_documents
            stats["failed"] += len(failed)
        done([i for i in ids if i not in failed])             # a document gone since it was queued is dropped
    return stats
