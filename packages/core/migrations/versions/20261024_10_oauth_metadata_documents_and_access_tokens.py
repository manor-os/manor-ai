"""OAuth client metadata documents and personal access tokens for the MCP resource.

A Client ID Metadata Document client's id is the https URL of its document, so
client_id columns widen to 512 characters (a metadata-only change for varchar in
Postgres) and the cached client row records when it was fetched and until when
that copy is fresh. Personal access
tokens give callers that cannot run OAuth a revocable key for the MCP endpoint.

Revision ID: 20261024_10
Revises: 20261031_20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import (
    add_column_if_not_exists,
    drop_column_if_exists,
    table_exists,
)

revision = "20261024_10"
down_revision = "20261031_20"
branch_labels = None
depends_on = None

_CLIENT_ID_TABLES = ("oauth_client_apps", "oauth_authorization_codes", "oauth_refresh_tokens")


def upgrade() -> None:
    for table in _CLIENT_ID_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column("client_id", existing_type=sa.String(64), type_=sa.String(512),
                               existing_nullable=False)
    for column in ("metadata_fetched_at", "metadata_expires_at"):
        add_column_if_not_exists("oauth_client_apps", sa.Column(column, sa.DateTime(timezone=True), nullable=True))

    if not table_exists("personal_access_tokens"):
        op.create_table(
            "personal_access_tokens",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column("user_id", sa.String(26), nullable=False),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("audience", sa.String(64), nullable=False),
            sa.Column("scope", sa.String(64), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("token_prefix", sa.String(16), nullable=False),
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        # Created with the table (same transaction), so the table guard covers them.
        op.create_index("ix_personal_access_tokens_hash", "personal_access_tokens", ["token_hash"], unique=True)
        op.create_index("ix_personal_access_tokens_user", "personal_access_tokens", ["user_id"])


def downgrade() -> None:
    if table_exists("personal_access_tokens"):
        op.drop_table("personal_access_tokens")
    for column in ("metadata_expires_at", "metadata_fetched_at"):
        drop_column_if_exists("oauth_client_apps", column)
    # URL client ids cannot fit the narrower column; their rows go with them.
    for table in _CLIENT_ID_TABLES:
        op.execute(f"DELETE FROM {table} WHERE length(client_id) > 64")
        with op.batch_alter_table(table) as batch:
            batch.alter_column("client_id", existing_type=sa.String(512), type_=sa.String(64),
                               existing_nullable=False)
