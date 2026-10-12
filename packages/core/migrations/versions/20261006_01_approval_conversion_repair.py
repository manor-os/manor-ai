"""Restore provable exact denials lost during reviewed legacy conversions."""
from alembic import op

from packages.core.migrations.approval_conversion_repair import apply

revision = '20261006_01'
down_revision = '20261005_01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    raise RuntimeError('Repaired approval restrictions require a reviewed forward change')
