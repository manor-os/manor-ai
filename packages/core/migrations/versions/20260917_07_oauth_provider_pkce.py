"""Bind Manor authorization codes to S256 PKCE, preserving legacy clients.

Revision ID: 20260917_07
Revises: 20260917_06
"""
from alembic import op
import sqlalchemy as sa
from packages.core.migrations.helpers import add_column_if_not_exists, drop_column_if_exists

revision = "20260917_07"
down_revision = "20260917_06"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_not_exists("oauth_client_apps", sa.Column("require_pkce", sa.Boolean(), nullable=True))
    add_column_if_not_exists("oauth_authorization_codes", sa.Column("code_challenge", sa.String(43), nullable=True))
    add_column_if_not_exists("oauth_authorization_codes", sa.Column("code_challenge_method", sa.String(8), nullable=True))


def downgrade():
    drop_column_if_exists("oauth_authorization_codes", "code_challenge_method")
    drop_column_if_exists("oauth_authorization_codes", "code_challenge")
    drop_column_if_exists("oauth_client_apps", "require_pkce")
