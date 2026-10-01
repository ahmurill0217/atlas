"""The Atlas knowledge-graph schema (Postgres schema `kg`).

The local-development migrations 0001-0004 were squashed into this one (see git
history before commit "Squash migrations and ontology versions"). Databases created
before the squash were stamped to this revision after their schema was brought up to it.

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS kg;

        CREATE TABLE kg.ontology_versions (
            version     TEXT PRIMARY KEY,
            checksum    TEXT NOT NULL,
            definition  JSONB NOT NULL,
            loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE kg.ingestion_runs (
            id               UUID PRIMARY KEY,
            pipeline_version TEXT NOT NULL,
            ontology_version TEXT NOT NULL,
            component_versions JSONB NOT NULL DEFAULT '{}',
            stats            JSONB NOT NULL DEFAULT '{}',
            status           TEXT NOT NULL DEFAULT 'running',
            started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at      TIMESTAMPTZ
        );

        -- Source records ------------------------------------------------------
        CREATE TABLE kg.documents (
            id                 UUID PRIMARY KEY,
            source_system      TEXT NOT NULL,
            source_type        TEXT NOT NULL,
            source_external_id TEXT NOT NULL,
            title              TEXT,
            uri                TEXT,
            source_created_at  TIMESTAMPTZ,
            source_updated_at  TIMESTAMPTZ,
            permissions        JSONB NOT NULL DEFAULT '{}',
            current_version_id UUID,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (source_system, source_external_id)
        );

        CREATE TABLE kg.document_versions (
            id                 UUID PRIMARY KEY,
            document_id        UUID NOT NULL REFERENCES kg.documents(id) ON DELETE CASCADE,
            checksum           TEXT NOT NULL,
            normalizer_version TEXT NOT NULL,
            normalized         JSONB NOT NULL,           -- full NormalizedDocument, for replay
            ingestion_run_id   UUID REFERENCES kg.ingestion_runs(id),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (document_id, checksum)
        );

        CREATE TABLE kg.document_sections (
            id                  UUID PRIMARY KEY,
            document_version_id UUID NOT NULL REFERENCES kg.document_versions(id) ON DELETE CASCADE,
            ordinal             INTEGER NOT NULL,
            text                TEXT NOT NULL,
            start_char          INTEGER,
            end_char            INTEGER,
            metadata            JSONB NOT NULL DEFAULT '{}',
            UNIQUE (document_version_id, ordinal)
        );

        -- A version is processed at most once per (pipeline, ontology) => idempotent.
        CREATE TABLE kg.document_processing (
            document_version_id UUID NOT NULL REFERENCES kg.document_versions(id) ON DELETE CASCADE,
            pipeline_version    TEXT NOT NULL,
            ontology_checksum   TEXT NOT NULL,
            ingestion_run_id    UUID REFERENCES kg.ingestion_runs(id),
            processed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (document_version_id, pipeline_version, ontology_checksum)
        );

        -- Canonical graph -------------------------------------------------------
        CREATE TABLE kg.entities (
            id               UUID PRIMARY KEY,           -- immutable, never derived from a name
            entity_type      TEXT NOT NULL,
            canonical_name   TEXT NOT NULL,
            normalized_name  TEXT NOT NULL,
            ontology_version TEXT NOT NULL,
            properties       JSONB NOT NULL DEFAULT '{}',
            roles            TEXT[] NOT NULL DEFAULT '{}',
            identity_strength TEXT NOT NULL,             -- 'identifier' | 'name_only'
            status           TEXT NOT NULL DEFAULT 'active',  -- active | merged | retired
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            property_sources JSONB NOT NULL DEFAULT '{}'      -- property -> document id that set it
        );
        CREATE INDEX ix_kg_entities_type ON kg.entities (entity_type);
        CREATE INDEX ix_kg_entities_normalized_name ON kg.entities (entity_type, normalized_name);

        CREATE TABLE kg.entity_aliases (
            id                 UUID PRIMARY KEY,
            entity_id          UUID NOT NULL REFERENCES kg.entities(id) ON DELETE CASCADE,
            alias              TEXT NOT NULL,
            normalized_alias   TEXT NOT NULL,
            source_document_id UUID REFERENCES kg.documents(id) ON DELETE SET NULL,
            confidence         DOUBLE PRECISION,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (entity_id, normalized_alias)
        );
        CREATE INDEX ix_kg_entity_aliases_normalized ON kg.entity_aliases (normalized_alias);

        CREATE TABLE kg.entity_external_ids (
            id              UUID PRIMARY KEY,
            entity_id       UUID NOT NULL REFERENCES kg.entities(id) ON DELETE CASCADE,
            identifier_type TEXT NOT NULL,
            value           TEXT NOT NULL,               -- normalized
            source_system   TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (identifier_type, value)              -- one identifier, one entity
        );
        CREATE INDEX ix_kg_entity_external_ids_entity ON kg.entity_external_ids (entity_id);

        CREATE TABLE kg.edges (
            id               UUID PRIMARY KEY,
            source_entity_id UUID NOT NULL REFERENCES kg.entities(id),
            relation_type    TEXT NOT NULL,
            target_entity_id UUID NOT NULL REFERENCES kg.entities(id),
            ontology_version TEXT NOT NULL,
            provenance_class TEXT NOT NULL,              -- strongest class among its evidence
            confidence       DOUBLE PRECISION,
            properties       JSONB NOT NULL DEFAULT '{}',
            valid_from       TIMESTAMPTZ,
            valid_to         TIMESTAMPTZ,
            first_seen_at    TIMESTAMPTZ,                -- earliest source time of evidence
            last_seen_at     TIMESTAMPTZ,                -- latest source time of evidence
            status           TEXT NOT NULL DEFAULT 'active',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_kg_edge UNIQUE (source_entity_id, relation_type, target_entity_id)
        );
        CREATE INDEX ix_kg_edges_target ON kg.edges (target_entity_id, relation_type);
        CREATE INDEX ix_kg_edges_relation ON kg.edges (relation_type);

        CREATE TABLE kg.edge_evidence (
            id                  UUID PRIMARY KEY,
            edge_id             UUID NOT NULL REFERENCES kg.edges(id) ON DELETE CASCADE,
            evidence_key        TEXT NOT NULL,           -- hash of (version, field/section, text): dedupes re-ingestion
            document_id         UUID NOT NULL REFERENCES kg.documents(id),
            document_version_id UUID NOT NULL REFERENCES kg.document_versions(id),
            section_id          UUID REFERENCES kg.document_sections(id),
            provenance_class    TEXT NOT NULL,
            source_field        TEXT,
            evidence_text       TEXT,
            start_offset        INTEGER,
            end_offset          INTEGER,
            page_number         INTEGER,
            observed_at         TIMESTAMPTZ,
            extractor           TEXT NOT NULL,
            extractor_version   TEXT NOT NULL,
            confidence          DOUBLE PRECISION,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (edge_id, evidence_key)
        );
        CREATE INDEX ix_kg_edge_evidence_document ON kg.edge_evidence (document_id);
        CREATE INDEX ix_kg_edge_evidence_version ON kg.edge_evidence (document_version_id);

        CREATE TABLE kg.entity_merge_history (
            id                UUID PRIMARY KEY,
            source_entity_id  UUID NOT NULL,
            target_entity_id  UUID NOT NULL,
            reason            TEXT NOT NULL,
            score             DOUBLE PRECISION,
            reviewer          TEXT,
            algorithm_version TEXT NOT NULL,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Stage outputs (inspectable, replayable) ---------------------------------
        CREATE TABLE kg.candidate_entities (
            id                  UUID PRIMARY KEY,
            ingestion_run_id    UUID REFERENCES kg.ingestion_runs(id),
            document_version_id UUID NOT NULL REFERENCES kg.document_versions(id) ON DELETE CASCADE,
            local_id            TEXT NOT NULL,
            payload             JSONB NOT NULL,          -- the CandidateEntity as proposed
            mapped_type         TEXT,
            decision            TEXT NOT NULL,           -- MATCHED | CREATED | REVIEW | REJECTED
            entity_id           UUID,
            reason              TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_kg_candidate_entities_version ON kg.candidate_entities (document_version_id);
        CREATE INDEX ix_kg_candidate_entities_entity ON kg.candidate_entities (entity_id);

        CREATE TABLE kg.candidate_edges (
            id                  UUID PRIMARY KEY,
            ingestion_run_id    UUID REFERENCES kg.ingestion_runs(id),
            document_version_id UUID NOT NULL REFERENCES kg.document_versions(id) ON DELETE CASCADE,
            local_id            TEXT NOT NULL,
            payload             JSONB NOT NULL,          -- the CandidateEdge as proposed
            mapping             JSONB,                   -- ontology mapping result
            violations          JSONB NOT NULL DEFAULT '[]',
            decision            TEXT NOT NULL,           -- ACCEPTED | REVIEW | REJECTED
            edge_id             UUID,
            reason              TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_kg_candidate_edges_edge ON kg.candidate_edges (edge_id);
        CREATE INDEX ix_kg_candidate_edges_version ON kg.candidate_edges (document_version_id);

        -- Review queue ---------------------------------------------------------------
        CREATE TABLE kg.review_items (
            id                 UUID PRIMARY KEY,
            review_type        TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN | APPROVED | REJECTED | DEFERRED | AUTO_RESOLVED
            dedupe_key         TEXT NOT NULL UNIQUE,     -- same issue => same item (frequency grows)
            frequency          INTEGER NOT NULL DEFAULT 1,
            source_document_id UUID REFERENCES kg.documents(id) ON DELETE SET NULL,
            candidate_payload  JSONB NOT NULL,
            examples           JSONB NOT NULL DEFAULT '[]',
            related_entities   UUID[] NOT NULL DEFAULT '{}',
            confidence         DOUBLE PRECISION,
            reason             TEXT NOT NULL,
            ontology_version   TEXT NOT NULL,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at        TIMESTAMPTZ,
            reviewer           TEXT,
            resolution         JSONB
        );
        CREATE INDEX ix_kg_review_items_open ON kg.review_items (status, review_type);

        -- Search-index queue: the action each document still needs in brain, written with the
        -- graph change and drained by `atlas index` (no FK: a deleted document stays queued).
        CREATE TABLE kg.index_queue (
            document_id UUID PRIMARY KEY,
            action      TEXT NOT NULL,                   -- upsert | delete
            enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Immutable audit log ---------------------------------------------------------
        CREATE TABLE kg.audit_log (
            id               BIGSERIAL PRIMARY KEY,
            at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            actor            TEXT NOT NULL,              -- 'pipeline' or a reviewer
            action           TEXT NOT NULL,              -- entity_created, edge_created, ...
            object_type      TEXT NOT NULL,
            object_id        UUID,
            ingestion_run_id UUID,
            details          JSONB NOT NULL DEFAULT '{}'
        );
        CREATE INDEX ix_kg_audit_object ON kg.audit_log (object_type, object_id);

        CREATE FUNCTION kg.forbid_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'kg.audit_log is append-only';
        END $$;
        CREATE TRIGGER trg_audit_append_only BEFORE UPDATE OR DELETE ON kg.audit_log
            FOR EACH ROW EXECUTE FUNCTION kg.forbid_audit_mutation();
        """
    )



def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS kg CASCADE")
