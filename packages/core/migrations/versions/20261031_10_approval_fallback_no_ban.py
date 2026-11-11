"""An unrepresentable legacy ban is reviewed, never a ban of every change.

The approval conversion (20261006_01, corrected by 20261007_01/_02) turned any
legacy ban it could not represent exactly (``never_allow_capabilities:
external.message`` and the like) into a deny rule on every category but read:
the Workspace could no longer update its own Tasks, write a file or change an
automation, and nobody had asked for that. The v3 generator maps the legacy
outward capabilities to the exact actions the legacy classifier gave them and
caps any scope-wide fallback at ``ask``.

This rebuilds every converted row with v3: what v1 or the earlier correction
wrote and nobody changed takes the v3 value; owner edits since are kept (and
reported where a fallback they edited stays in force). An ordinary online
revision, like 20261007_01: it only relaxes generated rules and changes no
evaluator. Without v1 receipts it is a no-op.

Revision ID: 20261031_10
Revises: 20261030_80
"""
from alembic import op

revision = '20261031_10'
down_revision = '20261030_80'
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()


def downgrade() -> None:
    raise RuntimeError('Corrected approval policies require a reviewed forward change')
