"""Initial schema: documents, chunks (pgvector), knowledge graph and provenance.

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from alembic import op

from brain.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    dim = get_settings().embedding_dim
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")  # fuzzy candidate lookup

    op.execute(
        f"""
        CREATE TABLE documents (
            id           UUID PRIMARY KEY,
            source_uri   TEXT NOT NULL UNIQUE,
            title        TEXT,
            content_hash TEXT NOT NULL,
            metadata     JSONB NOT NULL DEFAULT '{{}}',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_documents_content_hash ON documents (content_hash);

        CREATE TABLE chunks (
            id          UUID PRIMARY KEY,
            document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content     TEXT NOT NULL,
            embedding   VECTOR({dim}),
            metadata    JSONB NOT NULL DEFAULT '{{}}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (document_id, chunk_index)
        );
        CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);

        CREATE TABLE extraction_runs (
            id             UUID PRIMARY KEY,
            label          TEXT,
            model          TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            config         JSONB NOT NULL DEFAULT '{{}}',
            stats          JSONB NOT NULL DEFAULT '{{}}',
            started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at    TIMESTAMPTZ
        );

        CREATE TABLE chunk_extractions (
            id                UUID PRIMARY KEY,
            chunk_id          UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
            extraction_run_id UUID REFERENCES extraction_runs(id) ON DELETE SET NULL,
            cache_key         TEXT NOT NULL,
            output            JSONB NOT NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (chunk_id, cache_key)
        );

        CREATE TABLE entities (
            id              UUID PRIMARY KEY,
            canonical_name  TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            entity_type     TEXT NOT NULL,
            description     TEXT,
            properties      JSONB NOT NULL DEFAULT '{{}}',
            embedding       VECTOR({dim}),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_entities_normalized_name ON entities (normalized_name);
        CREATE INDEX ix_entities_normalized_name_trgm ON entities USING gin (normalized_name gin_trgm_ops);
        CREATE INDEX ix_entities_entity_type ON entities (entity_type);
        CREATE INDEX ix_entities_embedding ON entities USING hnsw (embedding vector_cosine_ops);

        CREATE TABLE entity_aliases (
            id               UUID PRIMARY KEY,
            entity_id        UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            alias            TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            source           TEXT NOT NULL,
            UNIQUE (entity_id, normalized_alias)
        );
        CREATE INDEX ix_entity_aliases_normalized_alias ON entity_aliases (normalized_alias);

        CREATE TABLE relationships (
            id                UUID PRIMARY KEY,
            source_entity_id  UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            target_entity_id  UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL,
            description       TEXT,
            confidence        DOUBLE PRECISION,
            support_count     INTEGER NOT NULL DEFAULT 0,
            properties        JSONB NOT NULL DEFAULT '{{}}',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_relationship_edge UNIQUE (source_entity_id, target_entity_id, relationship_type)
        );
        -- The unique constraint's index already serves lookups by source.
        CREATE INDEX ix_relationships_target ON relationships (target_entity_id);
        CREATE INDEX ix_relationships_type ON relationships (relationship_type);

        CREATE TABLE entity_mentions (
            id                UUID PRIMARY KEY,
            entity_id         UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            chunk_id          UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
            mention_text      TEXT NOT NULL,
            start_offset      INTEGER,
            end_offset        INTEGER,
            confidence        DOUBLE PRECISION,
            extraction_run_id UUID REFERENCES extraction_runs(id) ON DELETE SET NULL,
            UNIQUE (entity_id, chunk_id, mention_text)
        );
        CREATE INDEX ix_entity_mentions_chunk ON entity_mentions (chunk_id);

        CREATE TABLE relationship_evidence (
            id                UUID PRIMARY KEY,
            relationship_id   UUID NOT NULL REFERENCES relationships(id) ON DELETE CASCADE,
            chunk_id          UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
            evidence_text     TEXT NOT NULL,
            confidence        DOUBLE PRECISION,
            extraction_run_id UUID REFERENCES extraction_runs(id) ON DELETE SET NULL,
            details           JSONB NOT NULL DEFAULT '{{}}',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (relationship_id, chunk_id)
        );
        CREATE INDEX ix_relationship_evidence_chunk ON relationship_evidence (chunk_id);

        CREATE TABLE resolution_log (
            id                UUID PRIMARY KEY,
            extraction_run_id UUID REFERENCES extraction_runs(id) ON DELETE CASCADE,
            chunk_id          UUID REFERENCES chunks(id) ON DELETE CASCADE,
            kind              TEXT NOT NULL,
            decision          TEXT NOT NULL,
            method            TEXT,
            score             DOUBLE PRECISION,
            subject           TEXT NOT NULL,
            entity_id         UUID,
            relationship_id   UUID,
            details           JSONB NOT NULL DEFAULT '{{}}',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_resolution_log_run ON resolution_log (extraction_run_id, kind, decision);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS resolution_log, relationship_evidence, entity_mentions,
            relationships, entity_aliases, entities, chunk_extractions,
            extraction_runs, chunks, documents CASCADE;
        """
    )
