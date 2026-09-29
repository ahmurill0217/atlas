"""Build: chunks -> embeddings + knowledge graph with provenance.

    embed chunks missing an embedding
    extract every pending chunk (thread pool; LLM calls are the slow part)
    for each chunk, in deterministic (document, chunk_index) order, in one transaction:
        resolve entities -> canonical ids, aliases, mentions
        validate relationships -> upsert edge, attach evidence
        store the raw extraction (cache + audit trail)
    prune edges without evidence / entities without mentions

Chunks already extracted with the same model/prompt/ontology are skipped
unless `force=True`, so `build` is idempotent and cheap to re-run.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from brain_v0.config import Settings, get_settings
from brain_v0.db.models import Chunk, ChunkExtraction, Document, Entity, ExtractionRun
from brain_v0.db.session import get_engine, session_scope
from brain_v0.embeddings.embedder import Embedder
from brain_v0.extraction.extractor import ExtractionResult, KnowledgeGraphExtractor
from brain_v0.extraction.models import ExtractedEntity, ExtractedKnowledgeGraph
from brain_v0.extraction.prompts import PROMPT_VERSION
from brain_v0.graph.repository import GraphRepository
from brain_v0.llm.base import StructuredLLM
from brain_v0.normalize import normalize_entity_type, normalize_name
from brain_v0.resolution.entity_resolver import (
    Adjudicator,
    Decision,
    EntityResolver,
    acceptable_alias,
    entity_embedding_text,
)
from brain_v0.resolution.relationship_resolver import ResolvedEndpoint, RelationshipValidator

log = logging.getLogger(__name__)


@dataclass
class PendingChunk:
    id: uuid.UUID
    content: str
    document_title: str | None


@dataclass
class BuildReport:
    run_id: uuid.UUID
    stats: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)


def embed_pending_chunks(session: Session, embedder: Embedder, batch_size: int = 64) -> int:
    """Embed chunks with no embedding, or one made by a different embedder."""
    model = Chunk.metadata_["embedding_model"].astext
    rows = session.execute(
        select(Chunk.id, Chunk.content, Chunk.metadata_)
        .where(Chunk.embedding.is_(None) | model.is_(None) | (model != embedder.name))
        .order_by(Chunk.id)
    ).all()
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        vectors = embedder.embed([r.content for r in batch])
        for row, vector in zip(batch, vectors):
            session.execute(update(Chunk).where(Chunk.id == row.id).values(
                embedding=vector, metadata_={**row.metadata_, "embedding_model": embedder.name}))
    return len(rows)


def pending_chunks(session: Session, cache_key: str, force: bool) -> list[PendingChunk]:
    query = (
        select(Chunk.id, Chunk.content, Document.title)
        .join(Document, Document.id == Chunk.document_id)
        .order_by(Document.source_uri, Chunk.chunk_index)
    )
    if not force:
        done = select(ChunkExtraction.chunk_id).where(ChunkExtraction.cache_key == cache_key)
        query = query.where(Chunk.id.not_in(done))
    return [PendingChunk(r.id, r.content, r.title) for r in session.execute(query)]


def _find_span(forms: list[str], text: str) -> tuple[str, int | None, int | None]:
    for form in forms:
        m = re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text, flags=re.IGNORECASE)
        if m:
            return text[m.start() : m.end()], m.start(), m.end()
    return forms[0], None, None


def apply_extraction(
    session: Session,
    chunk: PendingChunk,
    result: ExtractionResult,
    run_id: uuid.UUID,
    settings: Settings,
    embedder: Embedder | None = None,
    adjudicator: Adjudicator | None = None,
) -> Counter:
    """Resolve, validate and persist one chunk's extraction. Returns counters."""
    repo = GraphRepository(session)
    resolver = EntityResolver(repo, settings, embedder, adjudicator)
    validator = RelationshipValidator(settings)
    allowed_types = {normalize_entity_type(t) for t in settings.allowed_entity_types}
    stats: Counter = Counter()
    endpoints: dict[str, ResolvedEndpoint] = {}

    for ent in result.graph.entities:
        etype = normalize_entity_type(ent.entity_type)
        if allowed_types and etype not in allowed_types:
            repo.log(kind="entity", decision="REJECTED", method="type_not_allowed", subject=ent.name,
                     run_id=run_id, chunk_id=chunk.id, details={"entity_type": etype})
            stats["entities_rejected"] += 1
            continue
        entity_id = _resolve_and_persist_entity(repo, resolver, ent, etype, chunk, run_id, settings, embedder, stats)
        entity = session.get_one(Entity, entity_id)
        endpoints[ent.local_id] = ResolvedEndpoint(
            entity_id,
            entity.canonical_name,
            tuple(dict.fromkeys([ent.name, *ent.aliases, *repo.surface_forms(entity_id)])),
            entity.entity_type,
        )

    for dropped in result.dropped:
        rel = dropped["relationship"]
        repo.log(kind="relationship", decision="REJECTED", method=dropped["reason"], run_id=run_id,
                 chunk_id=chunk.id, subject=f"{rel['source_local_id']} -{rel['relationship_type']}-> {rel['target_local_id']}",
                 details=rel)
        stats[f"relationships_rejected:{dropped['reason']}"] += 1

    for rel in result.graph.relationships:
        d = validator.validate(rel, endpoints, chunk.content)
        src_name = endpoints[rel.source_local_id].canonical_name if rel.source_local_id in endpoints else rel.source_local_id
        tgt_name = endpoints[rel.target_local_id].canonical_name if rel.target_local_id in endpoints else rel.target_local_id
        subject = f"{src_name} -{d.relationship_type or rel.relationship_type}-> {tgt_name}"
        if not d.accepted:
            repo.log(kind="relationship", decision="REJECTED", method=d.reason, subject=subject, run_id=run_id,
                     chunk_id=chunk.id, details={**d.details, "evidence": rel.evidence})
            stats[f"relationships_rejected:{d.reason}"] += 1
            continue
        rel_id, created = repo.upsert_relationship(d.source_id, d.target_id, d.relationship_type, rel.description)
        _, start, end = _find_span([rel.evidence], chunk.content)
        details = {**d.details, "quote_start": start, "quote_end": end}
        attached = repo.attach_evidence(rel_id, chunk.id, rel.evidence, rel.confidence, run_id, details)
        decision = "CREATED" if created else ("EVIDENCE_ATTACHED" if attached else "DUPLICATE_EVIDENCE")
        repo.log(kind="relationship", decision=decision, method="validated", subject=subject, run_id=run_id,
                 chunk_id=chunk.id, relationship_id=rel_id, score=rel.confidence, details=details)
        stats[f"relationships_{decision.lower()}"] += 1
    return stats


def _resolve_and_persist_entity(
    repo: GraphRepository,
    resolver: EntityResolver,
    ent: ExtractedEntity,
    etype: str,
    chunk: PendingChunk,
    run_id: uuid.UUID,
    settings: Settings,
    embedder: Embedder | None,
    stats: Counter,
) -> uuid.UUID:
    decision = resolver.resolve(ent, etype)
    if decision.decision is Decision.MATCH_EXISTING:
        entity_id = decision.entity_id
        repo.vote_entity_type(entity_id, etype)
        repo.fill_description(entity_id, ent.description)
    else:
        properties = {}
        if decision.decision is Decision.AMBIGUOUS:
            # Never merge on uncertainty: keep a separate, flagged entity.
            properties = {"resolution_status": "ambiguous",
                          "ambiguous_with": [str(c.entity_id) for c in decision.candidates]}
        embedding = None
        if settings.resolution_embedding_enabled and embedder:
            [embedding] = embedder.embed([entity_embedding_text(ent.name, etype, ent.description)])
        entity_id = repo.create_entity(ent.name, etype, ent.description, properties, embedding)
    stats[f"entities_{decision.decision.value.lower()}"] += 1

    repo.add_alias(entity_id, ent.name, source="name")
    rejected_aliases = []
    for alias in ent.aliases:
        if not acceptable_alias(alias, ent.name):
            rejected_aliases.append({"alias": alias, "reason": "unsafe_alias"})
            continue
        owners = repo.alias_owners(normalize_name(alias)) - {entity_id}
        if owners:
            rejected_aliases.append({"alias": alias, "reason": "owned_by_other_entity",
                                     "owners": [str(o) for o in owners]})
            continue
        repo.add_alias(entity_id, alias, source="extracted_alias")

    mention, start, end = _find_span([ent.name, *ent.aliases], chunk.content)
    repo.add_mention(entity_id, chunk.id, mention, start, end, ent.confidence, run_id)
    repo.log(kind="entity", decision=decision.decision.value, method=decision.method, score=decision.score,
             subject=ent.name, run_id=run_id, chunk_id=chunk.id, entity_id=entity_id,
             details={"entity_type": etype, "reason": decision.reason, "aliases": ent.aliases,
                      "candidates": [c.as_dict() for c in decision.candidates],
                      "rejected_aliases": rejected_aliases})
    return entity_id


def build(
    llm: StructuredLLM,
    embedder: Embedder | None,
    *,
    engine: Engine | None = None,
    settings: Settings | None = None,
    force: bool = False,
    replay: bool = False,
    label: str | None = None,
    adjudicator: Adjudicator | None = None,
) -> BuildReport:
    """`force`: re-extract every chunk with the LLM.
    `replay`: re-apply every chunk, reusing cached LLM output where it exists
    (after `brain reset --keep-extractions`, this re-runs resolution and
    validation on identical extractions, isolating them from LLM variance)."""
    settings = settings or get_settings()
    engine = engine or get_engine()
    extractor = KnowledgeGraphExtractor(
        llm, settings.allowed_entity_types, settings.allowed_relationship_types
    )

    with session_scope(engine) as session:
        run = ExtractionRun(
            id=uuid.uuid4(), label=label, model=llm.model_name, prompt_version=PROMPT_VERSION,
            config={"cache_key": extractor.cache_key, "force": force, "replay": replay,
                    **settings.model_dump(exclude={"openai_api_key", "database_url"})},
        )
        session.add(run)
    report = BuildReport(run.id)

    if embedder is not None:
        with session_scope(engine) as session:
            report.stats["chunks_embedded"] = embed_pending_chunks(session, embedder)

    with session_scope(engine) as session:
        chunks = pending_chunks(session, extractor.cache_key, force or replay)
        cached = {}
        if replay:
            cached = dict(session.execute(
                select(ChunkExtraction.chunk_id, ChunkExtraction.output)
                .where(ChunkExtraction.cache_key == extractor.cache_key)
            ).all())
    report.stats["chunks_pending"] = len(chunks)
    report.stats["chunks_replayed"] = sum(1 for c in chunks if c.id in cached)
    log.info("extracting %d chunks with %s", len(chunks), llm.model_name)

    def _extract(chunk: PendingChunk) -> ExtractionResult | Exception:
        if chunk.id in cached:
            output = dict(cached[chunk.id])
            dropped = output.pop("dropped", [])
            return ExtractionResult(ExtractedKnowledgeGraph.model_validate(output), dropped)
        try:
            return extractor.extract(chunk.content, chunk.document_title)
        except Exception as exc:  # one bad chunk must not sink the build
            return exc

    # Extraction runs concurrently, but each chunk is applied (in document
    # order) and committed as soon as its result is ready, so progress is
    # durable: an interrupted build resumes from the cache on the next run.
    with ThreadPoolExecutor(max_workers=max(1, settings.extraction_workers)) as pool:
        for i, (chunk, result) in enumerate(zip(chunks, pool.map(_extract, chunks)), 1):
            if isinstance(result, Exception):
                report.errors.append(f"chunk {chunk.id}: {type(result).__name__}: {result}")
                report.stats["chunks_failed"] += 1
                continue
            with session_scope(engine) as session:
                report.stats.update(
                    apply_extraction(session, chunk, result, run.id, settings, embedder, adjudicator)
                )
                session.execute(
                    insert(ChunkExtraction)
                    .values(id=uuid.uuid4(), chunk_id=chunk.id, extraction_run_id=run.id,
                            cache_key=extractor.cache_key,
                            output={**result.graph.model_dump(), "dropped": result.dropped})
                    .on_conflict_do_update(
                        index_elements=["chunk_id", "cache_key"],
                        set_={"output": insert(ChunkExtraction).excluded.output,
                              "extraction_run_id": run.id, "created_at": func.now()},
                    )
                )
            report.stats["chunks_extracted"] += 1
            if i % 50 == 0:
                log.info("applied %d/%d chunks", i, len(chunks))

    with session_scope(engine) as session:
        report.stats.update(GraphRepository(session).prune_unsupported())
        session.execute(
            update(ExtractionRun).where(ExtractionRun.id == run.id)
            .values(stats=dict(report.stats) | {"errors": report.errors}, finished_at=func.now())
        )
    return report
