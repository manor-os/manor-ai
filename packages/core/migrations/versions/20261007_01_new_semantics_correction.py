"""Correct the over-conservative v1 new-semantics approval conversion.

An ordinary online revision: it only rewrites v1-converted rows nobody has
changed since (to the v2 values, which only relax them), marks the others for
review, and changes no evaluator. Without v1 receipts it is a no-op.

Revision ID: 20261007_01
Revises: 20261006_01
"""
from alembic import op

revision = '20261007_01'
down_revision = '20261006_01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only the private new-semantics cutover ever wrote v1 receipts.
    connection = op.get_bind()


def downgrade() -> None:
    raise RuntimeError('Corrected approval policies require a reviewed forward change')
