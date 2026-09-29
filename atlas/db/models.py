"""ORM mapping of the `kg` schema. The Alembic migration (0002) is the DDL source of truth."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "kg"


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _fk(target: str, **kw) -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.{target}", **kw))


class OntologyVersion(Base):
    __tablename__, __table_args__ = "ontology_versions", {"schema": SCHEMA}
    version: Mapped[str] = mapped_column(Text, primary_key=True)
    checksum: Mapped[str] = mapped_column(Text)
    definition: Mapped[dict] = mapped_column(JSONB)
    loaded_at: Mapped[datetime] = _now()


class IngestionRun(Base):
    __tablename__, __table_args__ = "ingestion_runs", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    pipeline_version: Mapped[str] = mapped_column(Text)
    ontology_version: Mapped[str] = mapped_column(Text)
    component_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="running")
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(Base):
    __tablename__, __table_args__ = "documents", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    source_system: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    source_external_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str | None] = mapped_column(Text)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    permissions: Mapped[dict] = mapped_column(JSONB, default=dict)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _now()


class DocumentVersion(Base):
    __tablename__, __table_args__ = "document_versions", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = _fk("documents.id", ondelete="CASCADE")
    checksum: Mapped[str] = mapped_column(Text)
    normalizer_version: Mapped[str] = mapped_column(Text)
    normalized: Mapped[dict] = mapped_column(JSONB)
    ingestion_run_id: Mapped[uuid.UUID | None] = _fk("ingestion_runs.id")
    created_at: Mapped[datetime] = _now()


class DocumentSection(Base):
    __tablename__, __table_args__ = "document_sections", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    document_version_id: Mapped[uuid.UUID] = _fk("document_versions.id", ondelete="CASCADE")
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int | None] = mapped_column(Integer)
    end_char: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class DocumentProcessing(Base):
    __tablename__, __table_args__ = "document_processing", {"schema": SCHEMA}
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.document_versions.id", ondelete="CASCADE"), primary_key=True)
    pipeline_version: Mapped[str] = mapped_column(Text, primary_key=True)
    ontology_checksum: Mapped[str] = mapped_column(Text, primary_key=True)
    ingestion_run_id: Mapped[uuid.UUID | None] = _fk("ingestion_runs.id")
    processed_at: Mapped[datetime] = _now()


class Entity(Base):
    __tablename__, __table_args__ = "entities", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    entity_type: Mapped[str] = mapped_column(Text)
    canonical_name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(Text)
    ontology_version: Mapped[str] = mapped_column(Text)
    properties: Mapped[dict] = mapped_column(JSONB, default=dict)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    identity_strength: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class EntityAlias(Base):
    __tablename__, __table_args__ = "entity_aliases", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    entity_id: Mapped[uuid.UUID] = _fk("entities.id", ondelete="CASCADE")
    alias: Mapped[str] = mapped_column(Text)
    normalized_alias: Mapped[str] = mapped_column(Text)
    source_document_id: Mapped[uuid.UUID | None] = _fk("documents.id", ondelete="SET NULL")
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _now()


class EntityExternalId(Base):
    __tablename__, __table_args__ = "entity_external_ids", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    entity_id: Mapped[uuid.UUID] = _fk("entities.id", ondelete="CASCADE")
    identifier_type: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    source_system: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Edge(Base):
    __tablename__, __table_args__ = "edges", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    source_entity_id: Mapped[uuid.UUID] = _fk("entities.id")
    relation_type: Mapped[str] = mapped_column(Text)
    target_entity_id: Mapped[uuid.UUID] = _fk("entities.id")
    ontology_version: Mapped[str] = mapped_column(Text)
    provenance_class: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    properties: Mapped[dict] = mapped_column(JSONB, default=dict)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class EdgeEvidence(Base):
    __tablename__, __table_args__ = "edge_evidence", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    edge_id: Mapped[uuid.UUID] = _fk("edges.id", ondelete="CASCADE")
    evidence_key: Mapped[str] = mapped_column(Text)
    document_id: Mapped[uuid.UUID] = _fk("documents.id")
    document_version_id: Mapped[uuid.UUID] = _fk("document_versions.id")
    section_id: Mapped[uuid.UUID | None] = _fk("document_sections.id")
    provenance_class: Mapped[str] = mapped_column(Text)
    source_field: Mapped[str | None] = mapped_column(Text)
    evidence_text: Mapped[str | None] = mapped_column(Text)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extractor: Mapped[str] = mapped_column(Text)
    extractor_version: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = _now()


class CandidateEntityRow(Base):
    __tablename__, __table_args__ = "candidate_entities", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    ingestion_run_id: Mapped[uuid.UUID | None] = _fk("ingestion_runs.id")
    document_version_id: Mapped[uuid.UUID] = _fk("document_versions.id", ondelete="CASCADE")
    local_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    mapped_type: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class CandidateEdgeRow(Base):
    __tablename__, __table_args__ = "candidate_edges", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    ingestion_run_id: Mapped[uuid.UUID | None] = _fk("ingestion_runs.id")
    document_version_id: Mapped[uuid.UUID] = _fk("document_versions.id", ondelete="CASCADE")
    local_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    mapping: Mapped[dict | None] = mapped_column(JSONB)
    violations: Mapped[list] = mapped_column(JSONB, default=list)
    decision: Mapped[str] = mapped_column(Text)
    edge_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ReviewItem(Base):
    __tablename__, __table_args__ = "review_items", {"schema": SCHEMA}
    id: Mapped[uuid.UUID] = _pk()
    review_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="OPEN")
    dedupe_key: Mapped[str] = mapped_column(Text)
    frequency: Mapped[int] = mapped_column(Integer, default=1)
    source_document_id: Mapped[uuid.UUID | None] = _fk("documents.id", ondelete="SET NULL")
    candidate_payload: Mapped[dict] = mapped_column(JSONB)
    examples: Mapped[list] = mapped_column(JSONB, default=list)
    related_entities: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    confidence: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    ontology_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[dict | None] = mapped_column(JSONB)


class AuditLog(Base):
    __tablename__, __table_args__ = "audit_log", {"schema": SCHEMA}
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _now()
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    object_type: Mapped[str] = mapped_column(Text)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ingestion_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
