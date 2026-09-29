"""SQLAlchemy ORM tables. PostgreSQL is the system of record for everything:
documents, chunks + embeddings (pgvector), and the knowledge graph itself
(entities = nodes, relationships = edges, plus provenance tables).

The Alembic migration in migrations/versions is the source of truth for DDL;
these models mirror it for use in application code.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from brain_v0.config import get_settings

EMBEDDING_DIM = get_settings().embedding_dim


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Base(DeclarativeBase):
    pass


# --- Source material -----------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_uri: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


# --- Extraction bookkeeping ------------------------------------------------------


class ExtractionRun(Base):
    """One `build` invocation. Lets every edge's evidence point at the run
    (model, prompt version, settings) that produced it."""

    __tablename__ = "extraction_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    label: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChunkExtraction(Base):
    """Raw structured LLM output per chunk. Doubles as a cache (keyed by
    model + prompt + ontology) so rebuilding does not re-bill the LLM, and keeps
    the unresolved proposal around for debugging nondeterminism."""

    __tablename__ = "chunk_extractions"
    __table_args__ = (UniqueConstraint("chunk_id", "cache_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL")
    )
    cache_key: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class ResolutionLog(Base):
    """Every resolver / validator decision with its method and score, so that
    merges and rejections are auditable rather than silent."""

    __tablename__ = "resolution_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE")
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chunks.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # "entity" | "relationship"
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    relationship_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


# --- Knowledge graph ---------------------------------------------------------------


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Used by the (optional) embedding step of entity resolution.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    __table_args__ = (UniqueConstraint("entity_id", "normalized_alias"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # "name" | "extracted_alias" | "manual"


class Relationship(Base):
    __tablename__ = "relationships"
    # One edge per (source, type, target): repeated extractions attach
    # evidence to the existing edge instead of creating parallel edges.
    __table_args__ = (UniqueConstraint("source_entity_id", "target_entity_id", "relationship_type"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    support_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EntityMention(Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (UniqueConstraint("entity_id", "chunk_id", "mention_text"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    mention_text: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int | None] = mapped_column(Integer)
    end_offset: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL")
    )


class RelationshipEvidence(Base):
    __tablename__ = "relationship_evidence"
    # At most one evidence row per (edge, chunk): the same chunk re-extracted
    # does not inflate support.
    __table_args__ = (UniqueConstraint("relationship_id", "chunk_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    relationship_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("relationships.id", ondelete="CASCADE"), nullable=False
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False
    )
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL")
    )
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()
