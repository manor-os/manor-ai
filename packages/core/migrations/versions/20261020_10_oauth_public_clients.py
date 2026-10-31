"""Public OAuth clients: dynamic registration, resource audiences, refresh tokens.

Lets native MCP clients (Claude Code, Codex) self-register (RFC 7591), redeem
codes with PKCE and no secret, receive audience-bound access tokens, and keep a
session alive with rotating refresh tokens.

Revision ID: 20261020_10
Revises: 20261020_01
Create Date: 2026-10-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import (
    add_column_if_not_exists,
    column_exists,
    drop_column_if_exists,
    table_exists,
)

revision = "20261020_10"
down_revision = "20261020_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if column_exists("oauth_client_apps", "client_secret_hash"):
        with op.batch_alter_table("oauth_client_apps") as batch:
            batch.alter_column("client_secret_hash", existing_type=sa.String(255), nullable=True)
    add_column_if_not_exists("oauth_client_apps", sa.Column(
        "registration_source", sa.String(20), nullable=False, server_default="configured",
    ))
    add_column_if_not_exists("oauth_client_apps", sa.Column("token_audience", sa.String(64), nullable=True))

    if not table_exists("oauth_refresh_tokens"):
        op.create_table(
            "oauth_refresh_tokens",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("family_id", sa.String(26), nullable=False),
            sa.Column("client_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(26), nullable=False),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("scope", JSONB(), nullable=False, server_default="[]"),
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        # Created with the table (same transaction), so the table guard covers them.
        op.create_index("ix_oauth_refresh_tokens_hash", "oauth_refresh_tokens", ["token_hash"], unique=True)
        op.create_index("ix_oauth_refresh_tokens_family", "oauth_refresh_tokens", ["family_id"])
        op.create_index("ix_oauth_refresh_tokens_user", "oauth_refresh_tokens", ["user_id"])


def downgrade() -> None:
    if table_exists("oauth_refresh_tokens"):
        op.drop_table("oauth_refresh_tokens")
    drop_column_if_exists("oauth_client_apps", "token_audience")
    drop_column_if_exists("oauth_client_apps", "registration_source")
    # Public clients cannot satisfy NOT NULL; remove them before restoring it.
    op.execute("DELETE FROM oauth_client_apps WHERE client_secret_hash IS NULL")
    with op.batch_alter_table("oauth_client_apps") as batch:
        batch.alter_column("client_secret_hash", existing_type=sa.String(255), nullable=False)
