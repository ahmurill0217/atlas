"""KnowledgeIngestionPipeline: explicit, inspectable stages.

    1 Acquire + 2 Normalize   source adapter -> NormalizedDocument
    3 Segment                 source-aware sections (validated offsets)
      Persist                 document / version (by checksum) / sections
    4 Extract                 candidates (Phase 1: structured fields only, no model)
    5-9 Resolve/Map/Validate/Score/Commit-or-Review   GraphCompiler
    10 Index                  (embeddings: later phase)

Idempotent: a document version is processed at most once per
(pipeline version, ontology checksum); re-running the same corpus is a no-op.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import Engine, func, select, update
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
    IngestionRun,
    OntologyVersion,
)
from atlas.db.session import get_engine, session_scope
from atlas.extraction.candidates import CandidateSet
from atlas.extraction.structured import StructuredExtractor
from atlas.ingestion.adapters import discover, normalize_file
from atlas.ingestion.normalized import NormalizedDocument
from atlas.ontology.loader import OntologyError, load_ontology
from atlas.ontology.models import Ontology
from atlas.provenance.audit import audit
from atlas.review.service import reconcile_with_ontology


class DocumentTrace(BaseModel):
    path: str
    status: str                                   # processed | unchanged | failed
    document_id: uuid.UUID | None = None
    document_version_id: uuid.UUID | None = None
    sections: int = 0
    candidates: CandidateSet | None = None
    report: CompileReport | None = None
    error: str | None = None
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

    def ingest(self, path: str | Path, keep_traces: bool = False) -> RunReport:
        run_id = uuid.uuid4()
        with session_scope(self.engine) as s:
            s.add(IngestionRun(id=run_id, pipeline_version=PIPELINE_VERSION,
                               ontology_version=self.ontology.version, component_versions=self.component_versions()))
            s.flush()
            auto_resolved = self._register_ontology(s, run_id)
        report = RunReport(run_id=run_id)
        stats: dict[str, float] = {"reviews_auto_resolved_by_ontology": auto_resolved} if auto_resolved else {}
        for file in discover(path):
            trace = self.process_file(file, run_id)
            _add(stats, f"documents_{trace.status}")
            _add(stats, "sections", trace.sections)
            if trace.candidates:
                _add(stats, "candidate_entities", len(trace.candidates.entities))
                _add(stats, "candidate_edges", len(trace.candidates.edges))
            if trace.report:
                for key, value in trace.report.stats.items():
                    _add(stats, key, value)
            for stage, ms in trace.timings_ms.items():
                _add(stats, f"latency_ms_{stage}", round(ms, 2))
            if keep_traces or trace.status == "failed":
                report.traces.append(trace)
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
            trace.document_id, trace.sections = doc.document_id, len(doc.sections)
            with session_scope(self.engine) as s:
                version_id = self._persist(s, doc, run_id)
                trace.document_version_id = version_id
                done = s.get(DocumentProcessing, (version_id, PIPELINE_VERSION, self.ontology.checksum))
                if done:
                    trace.status = "unchanged"
                    return trace
                lap("persist")
                trace.candidates = self.extractor.extract(doc, version_id)
                lap("extract")
                trace.report = GraphCompiler(s, self.ontology, run_id).compile(doc, trace.candidates)
                lap("compile")
                s.add(DocumentProcessing(document_version_id=version_id, pipeline_version=PIPELINE_VERSION,
                                         ontology_checksum=self.ontology.checksum, ingestion_run_id=run_id))
            trace.status = "processed"
        except Exception as exc:  # one bad document must not sink the run
            trace.status, trace.error = "failed", f"{type(exc).__name__}: {exc}"
        return trace

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
