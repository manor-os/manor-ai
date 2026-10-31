"""Client deliveries record the release their payload holds.

``content_release`` keeps the payload's content version and the Knowledge
documents the provider selected, so a provider can publish a newer export of
the source Workspace to an already-delivered customer Workspace.

Revision ID: 20261028_10
Revises: 20261026_10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import add_column_if_not_exists, column_exists, table_exists


revision = "20261028_10"
down_revision = "20261026_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not table_exists("client_workspace_deliveries"):
        return
    add_column_if_not_exists(
        "client_workspace_deliveries",
        sa.Column("content_release", JSONB, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    if table_exists("client_workspace_deliveries") and column_exists("client_workspace_deliveries", "content_release"):
        op.drop_column("client_workspace_deliveries", "content_release")
