"""Never-expiring collaboration grants and provider trial backfill.

``workspace_collaborations.expires_at`` becomes nullable: a NULL expiry never
lapses (revocation stays a separate status). Accepted deliveries that are
still in trial get one ``provider_trial`` grant for the delivery sender, so
the provider keeps working inside the customer Workspace until acceptance.
Membership and WorkspaceStaff rows are derived from grants at runtime and are
not written here; ``workspace_staff.collaboration_id`` marks a seat derived
from a grant so customer-managed seats are never rewritten by it.

authority_fingerprint is filled after deploy by
scripts/reconcile_provider_trial_access.py, which runs
external_collaboration_access.reconcile_provider_trial_access before any
derived access is created; until then live_boundary rejects these rows, which
fails closed. Deliveries without an accepting customer, whose sender is
removed, or whose customer Workspace is deleted are not backfilled.

Revision ID: 20261015_01
Revises: 20261016_01
"""

from alembic import op
import sqlalchemy as sa

from packages.core.migrations.helpers import add_column_if_not_exists, column_exists, table_exists


INDEX_NAME = "ux_workspace_collaboration_provider_trial_delivery"

revision = "20261015_01"
down_revision = "20261016_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "workspace_collaborations",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )
    op.create_index(
        INDEX_NAME,
        "workspace_collaborations",
        ["delivery_id"],
        unique=True,
        postgresql_where=sa.text("role = 'provider_trial'"),
        if_not_exists=True,
    )
    add_column_if_not_exists("workspace_staff", sa.Column("collaboration_id", sa.String(26), nullable=True))
    if not all(table_exists(t) for t in ("client_workspace_deliveries", "users", "workspaces")):
        return
    op.execute(sa.text("""
        INSERT INTO workspace_collaborations (
            id, entity_id, workspace_id, delivery_id, email, role, status,
            authorized_by, user_id, expires_at, accepted_at,
            share_configuration, snapshot, created_at, updated_at
        )
        SELECT
            -- ULID-shaped: 26 Crockford base32 characters, the first 0-7.
            '0' || upper(substr(md5(random()::text || clock_timestamp()::text || d.id), 1, 25)),
            d.target_entity_id, d.target_workspace_id, d.id, lower(u.email),
            'provider_trial', 'active',
            d.accepted_by, d.created_by, NULL, coalesce(d.accepted_at, now()),
            false, '{}'::jsonb, now(), now()
        FROM client_workspace_deliveries d
        JOIN users u ON u.id = d.created_by
        WHERE u.deleted_at IS NULL AND u.status = 'active'
          AND EXISTS (
              SELECT 1 FROM workspaces w
              WHERE w.id = d.target_workspace_id AND w.deleted_at IS NULL
          )
          AND d.target_workspace_id IS NOT NULL
          AND d.target_entity_id IS NOT NULL
          AND d.accepted_by IS NOT NULL
          AND d.phase IN ('trial', 'awaiting_acceptance')
          AND NOT EXISTS (
              SELECT 1 FROM workspace_collaborations c
              WHERE c.delivery_id = d.id AND c.role = 'provider_trial'
          )
    """))


def downgrade() -> None:
    if table_exists("workspace_collaboration_entries"):
        op.execute(sa.text("""
            DELETE FROM workspace_collaboration_entries
            WHERE collaboration_id IN (
                SELECT id FROM workspace_collaborations WHERE role = 'provider_trial'
            )
        """))
    # Older revisions have no derived access: without the marker a derived owner
    # seat would read as customer-managed, so remove it and park the membership.
    if table_exists("user_memberships"):
        op.execute(sa.text("""
            UPDATE user_memberships m SET status = 'inactive'
            WHERE m.role = 'collaborator' AND EXISTS (
                SELECT 1 FROM workspace_collaborations c
                WHERE c.user_id = m.user_id AND c.entity_id = m.entity_id
                  AND c.role IN ('provider_trial', 'maintainer')
            )
        """))
    if column_exists("workspace_staff", "collaboration_id"):
        op.execute(sa.text("DELETE FROM workspace_staff WHERE collaboration_id IS NOT NULL"))
        op.drop_column("workspace_staff", "collaboration_id")
    op.execute(sa.text("DELETE FROM workspace_collaborations WHERE role = 'provider_trial'"))
    op.drop_index(INDEX_NAME, table_name="workspace_collaborations", if_exists=True)
    op.execute(sa.text("UPDATE workspace_collaborations SET expires_at = now() WHERE expires_at IS NULL"))
    op.alter_column(
        "workspace_collaborations",
        "expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
