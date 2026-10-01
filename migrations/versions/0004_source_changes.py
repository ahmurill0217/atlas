"""Source changes replace a document's contribution; an index queue keeps brain in step.

kg.index_queue holds, per document, the latest search-index action ('upsert' or 'delete'),
written in the same transaction as the graph change and drained by `atlas index`.
The new indexes serve the per-document cleanup: entity references are counted through
candidate_entities, and deleting a version must find its evidence and candidate edges.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kg.index_queue (
            document_id UUID PRIMARY KEY,        -- no FK: a deleted document stays queued for removal
            action      TEXT NOT NULL,           -- upsert | delete
            enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_kg_candidate_entities_entity ON kg.candidate_entities (entity_id);
        CREATE INDEX ix_kg_candidate_edges_version ON kg.candidate_edges (document_version_id);
        CREATE INDEX ix_kg_edge_evidence_version ON kg.edge_evidence (document_version_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX kg.ix_kg_edge_evidence_version;
        DROP INDEX kg.ix_kg_candidate_edges_version;
        DROP INDEX kg.ix_kg_candidate_entities_entity;
        DROP TABLE kg.index_queue;
        """
    )
