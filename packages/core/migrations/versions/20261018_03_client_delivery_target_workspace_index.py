"""Index client deliveries by their customer Workspace.

The billing payer lookup reads the accepted delivery of a Workspace on every
charge, so ``target_workspace_id`` needs its own index.

Revision ID: 20261018_03
Revises: 20261018_01
"""

from alembic import op


INDEX_NAME = "ix_client_delivery_target_workspace"

revision = "20261018_03"
down_revision = "20261018_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "client_workspace_deliveries",
        ["target_workspace_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="client_workspace_deliveries", if_exists=True)
