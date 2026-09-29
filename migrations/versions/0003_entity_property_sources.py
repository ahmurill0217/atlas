"""Record which document set each entity property.

A document re-describing itself (e.g. after a pipeline upgrade) supersedes its
own earlier value; a different document disagreeing is a CONFLICTING_FACT.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE kg.entities ADD COLUMN property_sources JSONB NOT NULL DEFAULT '{}'")


def downgrade() -> None:
    op.execute("ALTER TABLE kg.entities DROP COLUMN property_sources")
