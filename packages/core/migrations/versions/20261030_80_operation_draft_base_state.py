"""Operation draft base state.

``workspace_operation_drafts.base_state`` keeps the live Workspace state a
draft started from, so a draft applied after someone else's change is merged
onto it field by field instead of being refused. Existing drafts keep NULL and
the old refusal.

Revision ID: 20261030_80
Revises: 20261030_75
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import add_column_if_not_exists, drop_column_if_exists


revision = "20261030_80"
down_revision = "20261030_75"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists("workspace_operation_drafts", sa.Column("base_state", JSONB(), nullable=True))


def downgrade() -> None:
    drop_column_if_exists("workspace_operation_drafts", "base_state")
