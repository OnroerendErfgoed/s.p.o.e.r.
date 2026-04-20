"""add domain_relations table (no-op, folded into initial_schema)

Revision ID: a3c1e7d4f890
Revises: 6226f68ae484
Create Date: 2026-04-16

This migration is a no-op. The ``domain_relations`` table was added
to the initial_schema migration when the models were regenerated,
which caused this migration to fail on fresh databases ("table
already exists"). The file is kept as the tip of the migration chain
so Alembic's revision graph stays intact.
"""
from alembic import op

revision = "a3c1e7d4f890"
down_revision = "6226f68ae484"
branch_labels = None
depends_on = None


def upgrade():
    # Folded into initial_schema. No-op.
    pass


def downgrade():
    pass
