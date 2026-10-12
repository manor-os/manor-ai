"""Allow Blueprint share links to target verified accounts.

Revision ID: 20261003_01
Revises: 20261002_02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import column_exists, table_exists


revision = "20261003_01"
down_revision = "20261002_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("workspace_blueprints") and not column_exists(
        "workspace_blueprints", "share_allowed_emails",
    ):
        op.add_column(
            "workspace_blueprints",
            sa.Column(
                "share_allowed_emails",
                JSONB,
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )


def downgrade() -> None:
    if table_exists("workspace_blueprints") and column_exists(
        "workspace_blueprints", "share_allowed_emails",
    ):
        op.drop_column("workspace_blueprints", "share_allowed_emails")
