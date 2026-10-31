"""Let connection owners share integrations; record shared connection use.

Revision ID: 20261011_01
Revises: 20261007_03
"""

from alembic import op
import sqlalchemy as sa

from packages.core.migrations.helpers import create_index_if_not_exists, table_exists


revision = "20261011_01"
down_revision = "20261007_03"
branch_labels = None
depends_on = None

_SHARE_PERMISSION_JSON = '["integrations.share"]'
# Roles seeded before 2026-08-24 never received the permission, so even owners
# and admins had no Share action. Members now share the accounts they own.
# Custom roles are left to their admins.
_SHARING_ROLES = ("owner", "admin", "member")


def upgrade() -> None:
    if table_exists("staff_roles"):
        for role_name in _SHARING_ROLES:
            op.execute(sa.text("""
                UPDATE staff_roles
                SET permissions = COALESCE(permissions, '[]'::jsonb) || CAST(:permission_json AS jsonb)
                WHERE lower(name) = :role_name
                  AND NOT COALESCE(permissions, '[]'::jsonb) @> CAST(:permission_json AS jsonb)
            """).bindparams(role_name=role_name, permission_json=_SHARE_PERMISSION_JSON))

    if not table_exists("integration_connection_uses"):
        op.create_table(
            "integration_connection_uses",
            sa.Column("id", sa.String(26), primary_key=True),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column("connection_kind", sa.String(20), nullable=False),
            sa.Column("connection_id", sa.String(26), nullable=False),
            sa.Column("owner_user_id", sa.String(26), nullable=False),
            sa.Column("actor_user_id", sa.String(26), nullable=False),
            sa.Column("agent_id", sa.String(26)),
            sa.Column("conversation_id", sa.String(26)),
            sa.Column("server_key", sa.String(80), nullable=False),
            sa.Column("tool_name", sa.String(120), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False,
            ),
        )
    create_index_if_not_exists(
        "ix_integration_connection_uses_connection",
        "integration_connection_uses",
        ["entity_id", "connection_kind", "connection_id", "created_at"],
    )


def downgrade() -> None:
    # The permission backfill is intentionally kept: removing it would hide
    # Share again from roles that legitimately hold it.
    op.drop_index("ix_integration_connection_uses_connection", table_name="integration_connection_uses")
    op.drop_table("integration_connection_uses")
