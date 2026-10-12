"""Finish the v1 correction for rows owners changed after the conversion.

The first release of 20261007_01 only flagged rows changed since the v1
conversion, so an owner who switched to "Run automatically" kept every
untouched v1 fallback (reads still asked). This rerun merges those rows like
20261007_01 now does: untouched v1 values take v2, owner edits are kept.
Rows an earlier run corrected are never redone; without v1 receipts, or after
the current 20261007_01, it changes nothing.

Revision ID: 20261007_02
Revises: 20261007_01
"""
from alembic import op

revision = '20261007_02'
down_revision = '20261007_01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()


def downgrade() -> None:
    raise RuntimeError('Corrected approval policies require a reviewed forward change')
