"""Add exact-address verification evidence without backfilling existing users.

Revision ID: 20260917_08
Revises: 20260917_07
"""

import sqlalchemy as sa

from packages.core.migrations.helpers import add_column_if_not_exists, drop_column_if_exists

revision = "20260917_08"
down_revision = "20260917_07"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_not_exists("users", sa.Column("verified_email", sa.String(255), nullable=True))
    add_column_if_not_exists("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    add_column_if_not_exists(
        "oauth_client_apps",
        sa.Column(
            "require_verified_email",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    drop_column_if_exists("oauth_client_apps", "require_verified_email")
    drop_column_if_exists("users", "email_verified_at")
    drop_column_if_exists("users", "verified_email")
