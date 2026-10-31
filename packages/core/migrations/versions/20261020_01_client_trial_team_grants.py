"""One live access grant per person and delivery.

Every manager of a delivery's source Workspace may open the customer trial
from their own company, each through their own grant, so the uniqueness rule
moves from one ``provider_trial`` grant per delivery to one active
``provider_trial``/``maintainer`` grant per (delivery, user). A revoked grant
no longer blocks a new one.

Upgrade also revokes duplicate active grants per (delivery, user), keeping a
live one (the oldest when several are), and moves a persisted company pointer
(``users.entity_id``/``role``) off a derived collaborator membership to the
person's own company: the provider works from home and never switches into
the customer company again. Both are idempotent.

Downgrade restores one ``provider_trial`` grant per delivery, keeping the
oldest active one (else the oldest), and clears the extra grants' entries, derived seats and, when nothing
else is live, their collaborator memberships.

Revision ID: 20261020_01
Revises: 20261018_03
"""

from alembic import op
import sqlalchemy as sa

from packages.core.migrations.helpers import column_exists, table_exists


OLD_INDEX = "ux_workspace_collaboration_provider_trial_delivery"
INDEX_NAME = "ux_workspace_collaboration_delivery_user"
ACCESS_GRANT = (
    "delivery_id IS NOT NULL AND user_id IS NOT NULL AND status = 'active'"
    " AND role IN ('provider_trial', 'maintainer')"
)
# auth_service.normalize_membership_role; the collaborator role is never a home.
MEMBERSHIP_ROLES = "'owner', 'admin', 'member', 'viewer', 'external', 'client'"

revision = "20261020_01"
down_revision = "20261019_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    duplicates = f"""
        SELECT id, first_value(id) OVER (
            PARTITION BY delivery_id, user_id
            ORDER BY (expires_at IS NOT NULL AND expires_at <= now()), created_at NULLS LAST, id
        ) AS keeper
        FROM workspace_collaborations
        WHERE {ACCESS_GRANT}
    """
    if column_exists("workspace_staff", "collaboration_id"):
        # The person's one derived seat passes to the grant that stays.
        op.execute(sa.text(f"""
            UPDATE workspace_staff s SET collaboration_id = d.keeper, expires_at = k.expires_at
            FROM ({duplicates}) d JOIN workspace_collaborations k ON k.id = d.keeper
            WHERE s.collaboration_id = d.id AND d.id <> d.keeper
        """))
    op.execute(sa.text(f"""
        UPDATE workspace_collaborations c SET status = 'revoked', snapshot = '{{}}'::jsonb
        FROM ({duplicates}) d
        WHERE c.id = d.id AND d.id <> d.keeper
    """))
    op.drop_index(OLD_INDEX, table_name="workspace_collaborations", if_exists=True)
    op.create_index(
        INDEX_NAME,
        "workspace_collaborations",
        ["delivery_id", "user_id"],
        unique=True,
        postgresql_where=sa.text(ACCESS_GRANT),
        if_not_exists=True,
    )
    if not all(table_exists(t) for t in ("users", "user_memberships")):
        return
    # external_collaboration_access.home_membership: the person's own active
    # company, primary first.
    home = """
        FROM user_memberships h
        WHERE h.user_id = u.id AND h.entity_id <> u.entity_id AND h.role <> 'collaborator'
          AND h.status = 'active' AND h.deleted_at IS NULL
    """
    op.execute(sa.text(f"""
        UPDATE users u SET (entity_id, role) = (
            SELECT h.entity_id,
                   CASE WHEN lower(btrim(h.role)) IN ({MEMBERSHIP_ROLES}) THEN lower(btrim(h.role)) ELSE 'member' END
            {home}
            ORDER BY h.is_primary DESC, h.created_at
            LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 FROM user_memberships c
            WHERE c.user_id = u.id AND c.entity_id = u.entity_id AND c.role = 'collaborator'
        )
        AND EXISTS (SELECT 1 {home})
    """))


def downgrade() -> None:
    extra = """
        SELECT id FROM (
            SELECT id, row_number() OVER (
                PARTITION BY delivery_id ORDER BY (status <> 'active'), created_at NULLS LAST, id
            ) AS n
            FROM workspace_collaborations
            WHERE role = 'provider_trial' AND delivery_id IS NOT NULL
        ) ranked WHERE n > 1
    """
    if table_exists("workspace_collaboration_entries"):
        op.execute(sa.text(f"DELETE FROM workspace_collaboration_entries WHERE collaboration_id IN ({extra})"))
    if column_exists("workspace_staff", "collaboration_id"):
        op.execute(sa.text(f"DELETE FROM workspace_staff WHERE collaboration_id IN ({extra})"))
    if table_exists("user_memberships"):
        # A collaborator membership stays only while another access grant is live.
        op.execute(sa.text(f"""
            UPDATE user_memberships m SET status = 'inactive'
            WHERE m.role = 'collaborator' AND m.status = 'active'
              AND EXISTS (
                  SELECT 1 FROM workspace_collaborations c
                  WHERE c.id IN ({extra}) AND c.user_id = m.user_id AND c.entity_id = m.entity_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM workspace_collaborations c
                  WHERE c.user_id = m.user_id AND c.entity_id = m.entity_id AND c.status = 'active'
                    AND c.role IN ('provider_trial', 'maintainer') AND c.id NOT IN ({extra})
                    AND (c.expires_at IS NULL OR c.expires_at > now())
              )
        """))
    op.execute(sa.text(f"DELETE FROM workspace_collaborations WHERE id IN ({extra})"))
    op.drop_index(INDEX_NAME, table_name="workspace_collaborations", if_exists=True)
    op.create_index(
        OLD_INDEX,
        "workspace_collaborations",
        ["delivery_id"],
        unique=True,
        postgresql_where=sa.text("role = 'provider_trial'"),
        if_not_exists=True,
    )
