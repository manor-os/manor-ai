"""Declare services that installed Workspaces already subscribe Agents to."""
from alembic import op

from packages.core.migrations.workspace_service_declarations import apply

revision = '20261007_03'
down_revision = '20261007_02'
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply(op.get_bind())


def downgrade() -> None:
    # Additive declarations of services that already had live deployments;
    # removing them would only restore the invalid state.
    pass
