"""KnowledgeIngestionPipeline: explicit, inspectable stages.

    1 Acquire + 2 Normalize   source adapter -> NormalizedDocument
    3 Segment                 source-aware sections (validated offsets)
      Persist                 document / version (by checksum) / sections
    4 Extract                 candidates (Phase 1: structured fields only, no model)
    5-9 Resolve/Map/Validate/Score/Commit-or-Review   GraphCompiler
    10 Sweep                  what the document supported before and no longer does is
                              removed (atlas.graph.sweep); the search index is queued

Idempotent: a document version is processed at most once per
(pipeline version, ontology checksum); re-running the same corpus is a no-op.
The graph is current-only: a changed document replaces its contribution, a deleted one
(`delete_document`, or `ingest(..., prune=True)`) removes it, and a version older than
the stored one (by the source's own updated time) is skipped as stale.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from atlas.compiler.compiler import CompileReport, GraphCompiler
from atlas.config import (
    NORMALIZER_VERSION,
    PIPELINE_VERSION,
    RESOLVER_VERSION,
    STRUCTURED_EXTRACTOR_VERSION,
    Settings,
    get_settings,
)
from atlas.db.models import (
    AuditLog,
    Document,
    DocumentProcessing,
    DocumentSection,
    DocumentVersion,
    EdgeEvidence,
    IngestionRun,
    OntologyVersion,
)
from atlas.db.session import get_engine, session_scope
from atlas.extraction.candidates import CandidateSet
from atlas.extraction.structured import StructuredExtractor
from atlas.graph.sweep import DocumentSweep, enqueue_index, lock_document
from atlas.ingestion.adapters import discover, normalize_file
from atlas.ingestion.normalized import NormalizedDocument, document_id_for
from atlas.ontology.loader import OntologyError, load_ontology
from atlas.ontology.models import Ontology
from atlas.provenance.audit import audit
from atlas.review.service import reconcile_with_ontology


class DocumentTrace(BaseModel):
    path: str
    status: str                                   # processed | unchanged | stale | failed
    document_id: uuid.UUID | None = None
    source_system: str | None = None
    document_version_id: uuid.UUID | None = None
    sections: int = 0
    candidates: CandidateSet | None = None
    report: CompileReport | None = None
    error: str | None = None
    sweep: dict[str, int] = Field(default_factory=dict)          # what replacing the document removed
    timings_ms: dict[str, float] = Field(default_factory=dict)


class RunReport(BaseModel):
    run_id: uuid.UUID
    stats: dict[str, float] = Field(default_factory=dict)
    traces: list[DocumentTrace] = Field(default_factory=list)


def segment(doc: NormalizedDocument) -> NormalizedDocument:
    """Stage 3. Adapters segment along source boundaries (email body vs quoted,
    transcript turns, paragraphs); here we enforce the invariants every
    downstream evidence pointer relies on."""
    for i, s in enumerate(doc.sections):
        if s.ordinal != i:
            raise ValueError(f"section ordinals not sequential at {i}")
        if s.start_char is not None and doc.raw_text[s.start_char:s.end_char] != s.text:
            raise ValueError(f"section {i} offsets do not match raw_text")
    return doc


class KnowledgeIngestionPipeline:
    def __init__(self, engine: Engine | None = None, settings: Settings | None = None,
                 ontology: Ontology | None = None):
        self.settings = settings or get_settings()
        self.engine = engine or get_engine(self.settings.database_url)
        self.ontology = ontology or load_ontology(str(self.settings.ontology_dir))
        self.extractor = StructuredExtractor(self.ontology, self.settings.internal_domains)

    def component_versions(self) -> dict[str, str]:
        return {"pipeline": PIPELINE_VERSION, "normalizer": NORMALIZER_VERSION,
                "structured_extractor": STRUCTURED_EXTRACTOR_VERSION, "entity_resolver": RESOLVER_VERSION,
                "ontology": self.ontology.version, "ontology_checksum": self.ontology.checksum}

    def ingest(self, path: str | Path, keep_traces: bool = False, prune: bool = False,
               prune_limit: float = 0.05, force_prune: bool = False) -> RunReport:
        """Process every file under `path`. With `prune`, `path` is the complete listing of its
        sources: their stored documents it no longer contains are deleted. A prune is skipped
        when any file failed (its document cannot be told apart from a deleted one) and refused
        when it would delete more than `prune_limit` of a source's documents, unless `force_prune`."""
        run_id = uuid.uuid4()
        with session_scope(self.engine) as s:
            s.add(IngestionRun(id=run_id, pipeline_version=PIPELINE_VERSION,
                               ontology_version=self.ontology.version, component_versions=self.component_versions()))
            s.flush()
            auto_resolved = self._register_ontology(s, run_id)
        report = RunReport(run_id=run_id)
        stats: dict[str, float] = {"reviews_auto_resolved_by_ontology": auto_resolved} if auto_resolved else {}
        seen: dict[str, set[uuid.UUID]] = {}
        for file in discover(path):
            trace = self.process_file(file, run_id)
            if trace.document_id and trace.source_system:
                seen.setdefault(trace.source_system, set()).add(trace.document_id)
            _add(stats, f"documents_{trace.status}")
            _add(stats, "sections", trace.sections)
            if trace.candidates:
                _add(stats, "candidate_entities", len(trace.candidates.entities))
                _add(stats, "candidate_edges", len(trace.candidates.edges))
            if trace.report:
                for key, value in trace.report.stats.items():
                    _add(stats, key, value)
            for key, value in trace.sweep.items():
                _add(stats, key, value)
            for stage, ms in trace.timings_ms.items():
                _add(stats, f"latency_ms_{stage}", round(ms, 2))
            if keep_traces or trace.status == "failed":
                report.traces.append(trace)
        if prune:
            stats.update(self._prune(seen, stats, run_id, prune_limit, force_prune))
        with session_scope(self.engine) as s:
            stats["review_items_opened"] = s.execute(select(func.count()).select_from(AuditLog).where(
                AuditLog.ingestion_run_id == run_id, AuditLog.action == "review_created")).scalar_one()
            stats.update(llm_calls=0, llm_tokens=0, llm_cost_usd=0.0)
            s.execute(update(IngestionRun).where(IngestionRun.id == run_id)
                      .values(stats=stats, status="completed", finished_at=func.now()))
        report.stats = stats
        return report

    def process_file(self, file: Path, run_id: uuid.UUID) -> DocumentTrace:
        trace = DocumentTrace(path=str(file), status="failed")
        clock = time.perf_counter()

        def lap(stage: str) -> None:
            nonlocal clock
            now = time.perf_counter()
            trace.timings_ms[stage] = (now - clock) * 1000
            clock = now

        try:
            doc = normalize_file(file)
            lap("normalize")
            doc = segment(doc)
            lap("segment")
            trace.document_id, trace.source_system, trace.sections = doc.document_id, doc.source_system, len(doc.sections)
            with session_scope(self.engine) as s:
                lock_document(s, doc.document_id)
                stored = s.get(Document, doc.document_id)
                if stored and stored.source_updated_at and doc.updated_at and doc.updated_at < stored.source_updated_at:
                    trace.status = "stale"                 # an older version arriving late
                    return trace
                version_id = self._persist(s, doc, run_id)
                trace.document_version_id = version_id
                done = s.get(DocumentProcessing, (version_id, PIPELINE_VERSION, self.ontology.checksum))
                if done:
                    trace.status = "unchanged"
                    return trace
                lap("persist")
                sweep = DocumentSweep(s, self.ontology, run_id)
                prior = sweep.begin(doc.document_id) if stored else None   # a new document has nothing to replace
                trace.candidates = self.extractor.extract(doc, version_id)
                lap("extract")
                compiler = GraphCompiler(s, self.ontology, run_id)
                trace.report = compiler.compile(doc, trace.candidates)
                lap("compile")
                if prior:
                    sweep.finish(doc.document_id, prior, keep_version=version_id,
                                 kept_evidence=compiler.kept_evidence, raised=compiler.reviews.raised)
                    if sweep.stats:
                        audit(s, "document_replaced", "document", doc.document_id, run_id, dict(sweep.stats))
                    trace.sweep = dict(sweep.stats)
                    lap("sweep")
                s.add(DocumentProcessing(document_version_id=version_id, pipeline_version=PIPELINE_VERSION,
                                         ontology_checksum=self.ontology.checksum, ingestion_run_id=run_id))
                enqueue_index(s, doc.document_id, "upsert")
            trace.status = "processed"
        except Exception as exc:  # one bad document must not sink the run
            trace.status, trace.error = "failed", f"{type(exc).__name__}: {exc}"
        return trace

    # --- deletion ------------------------------------------------------------------------

    def delete_document(self, source_system: str, source_external_id: str,
                        run_id: uuid.UUID | None = None) -> dict[str, int] | None:
        """Remove a document the source deleted (or the user can no longer see): its versions,
        text, evidence, the edges and entities only it supported, and its review examples; the
        search index is queued to drop it. None if Atlas does not have it."""
        return self._delete(document_id_for(source_system, source_external_id), run_id)

    def _delete(self, document_id: uuid.UUID, run_id: uuid.UUID | None) -> dict[str, int] | None:
        with session_scope(self.engine) as s:
            lock_document(s, document_id)
            if s.get(Document, document_id) is None:
                return None
            sweep = DocumentSweep(s, self.ontology, run_id)
            stats = dict(sweep.finish(document_id, sweep.begin(document_id)))
            s.execute(delete(Document).where(Document.id == document_id))
            enqueue_index(s, document_id, "delete")
            audit(s, "document_deleted", "document", document_id, run_id, stats)
        return stats

    def _prune(self, seen: dict[str, set[uuid.UUID]], stats: dict, run_id: uuid.UUID,
               limit: float, force: bool) -> dict[str, float]:
        if stats.get("documents_failed"):
            return {"prune_skipped_failed_files": stats["documents_failed"]}
        with session_scope(self.engine) as s:
            stored = {system: set(s.execute(select(Document.id).where(Document.source_system == system)).scalars())
                      for system in seen}
        gone = {system: sorted(ids - seen[system]) for system, ids in stored.items()}
        refused = {system: len(ids) for system, ids in gone.items()
                   if ids and not force and len(ids) > limit * len(stored[system])}
        if refused:
            return {"prune_refused": sum(refused.values())}
        out: dict[str, float] = {}
        for ids in gone.values():
            for document_id in ids:
                removed = self._delete(document_id, run_id)
                if removed is not None:
                    _add(out, "documents_deleted")
                    for key, value in removed.items():
                        _add(out, key, value)
        return out

    def collect_garbage(self) -> dict[str, float]:
        """One-off cleanup for graphs built before documents replaced their contribution:
        drop every non-current version, its evidence and candidate rows, and what only it supported."""
        out: dict[str, float] = {}
        with session_scope(self.engine) as s:
            todo = s.execute(text("""SELECT d.id, d.current_version_id FROM kg.documents d
                                     WHERE EXISTS (SELECT 1 FROM kg.document_versions v
                                                   WHERE v.document_id = d.id AND v.id <> d.current_version_id)
                                     ORDER BY d.id""")).all()
        for document_id, current in todo:
            with session_scope(self.engine) as s:
                lock_document(s, document_id)
                kept = {(r.edge_id, r.evidence_key) for r in s.execute(
                    select(EdgeEvidence.edge_id, EdgeEvidence.evidence_key)
                    .where(EdgeEvidence.document_version_id == current))}
                sweep = DocumentSweep(s, self.ontology)
                prior = sweep.begin(document_id, spare_version=current, scrub_examples=False)
                stats = sweep.finish(document_id, prior, keep_version=current, kept_evidence=kept)
                audit(s, "document_garbage_collected", "document", document_id, None, dict(stats))
                _add(out, "documents")
                for key, value in stats.items():
                    _add(out, key, value)
        return out

    # --- persistence helpers -------------------------------------------------------------

    def _register_ontology(self, s, run_id: uuid.UUID) -> int:
        """Record the ontology version on first use; refuse in-place edits.
        A newly registered version closes the review items it now covers."""
        existing = s.get(OntologyVersion, self.ontology.version)
        if existing is None:
            s.add(OntologyVersion(version=self.ontology.version, checksum=self.ontology.checksum,
                                  definition=self.ontology.model_dump(mode="json")))
            audit(s, "ontology_registered", "ontology", None, run_id,
                  {"version": self.ontology.version, "checksum": self.ontology.checksum})
            return reconcile_with_ontology(s, self.ontology, run_id)
        if existing.checksum != self.ontology.checksum:
            raise OntologyError([f"ontology {self.ontology.version} files changed since it was first used "
                                 f"(checksum {existing.checksum[:12]} -> {self.ontology.checksum[:12]}); "
                                 "bump the version instead of editing it in place"])
        return 0

    def _persist(self, s, doc: NormalizedDocument, run_id: uuid.UUID) -> uuid.UUID:
        values = dict(source_system=doc.source_system, source_type=doc.source_type,
                      source_external_id=doc.source_external_id, title=doc.title, uri=doc.uri,
                      source_created_at=doc.created_at, source_updated_at=doc.updated_at,
                      permissions=doc.permissions)
        s.execute(insert(Document).values(id=doc.document_id, **values)
                  .on_conflict_do_update(index_elements=["id"], set_=values))
        checksum = doc.checksum()
        version_id = s.execute(select(DocumentVersion.id).where(
            DocumentVersion.document_id == doc.document_id, DocumentVersion.checksum == checksum)).scalar_one_or_none()
        if version_id is None:
            version_id = uuid.uuid4()
            s.add(DocumentVersion(id=version_id, document_id=doc.document_id, checksum=checksum,
                                  normalizer_version=NORMALIZER_VERSION, normalized=doc.model_dump(mode="json"),
                                  ingestion_run_id=run_id))
            s.flush()
            for sec in doc.sections:
                s.add(DocumentSection(id=uuid.uuid4(), document_version_id=version_id, ordinal=sec.ordinal,
                                      text=sec.text, start_char=sec.start_char, end_char=sec.end_char,
                                      metadata_={"kind": sec.kind, **sec.metadata}))
            s.execute(update(Document).where(Document.id == doc.document_id).values(current_version_id=version_id))
        return version_id


def _add(stats: dict, key: str, value: float = 1) -> None:
    stats[key] = stats.get(key, 0) + value
