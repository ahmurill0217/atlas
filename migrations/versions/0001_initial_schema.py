"""Root revision (formerly the brain_v0 prototype's public-schema tables).

The prototype was removed; its schema lives in git history (tag prototype-v0).
This revision is kept, empty, so the chain 0001 -> 0002 -> 0003 still resolves
for databases created before the removal. It creates nothing; the Atlas
schema starts in 0002 (schema `kg`). Databases that ran the old 0001 keep
their public-schema prototype tables until they are dropped by hand.

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
