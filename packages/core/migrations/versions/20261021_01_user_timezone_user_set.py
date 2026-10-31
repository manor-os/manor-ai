"""Record whether a user chose their timezone.

Accounts used to start on a "UTC" placeholder that nothing replaced, so every
preference-aware surface and schedule ran in UTC. Clients now replace a zone
the user did not choose with the device's zone.

Existing rows: any non-default zone came from an explicit choice (or signup
geolocation, which the Cloud merge revision releases), so it stays fixed. A
"UTC" row cannot be told apart from the untouched default and follows the
device from the next sign-in; choosing UTC in Account settings fixes it again.

Revision ID: 20261021_01
Revises: 20261020_10
Create Date: 2026-10-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import column_exists, drop_column_if_exists

revision = "20261021_01"
down_revision = "20261020_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if column_exists("users", "timezone_user_set"):
        return
    op.add_column("users", sa.Column(
        "timezone_user_set", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.execute(
        "UPDATE users SET timezone_user_set = true "
        "WHERE timezone IS NOT NULL AND timezone NOT IN ('', 'UTC')"
    )


def downgrade() -> None:
    drop_column_if_exists("users", "timezone_user_set")
