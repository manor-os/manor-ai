"""Move legacy Blueprint upgrade restore points out of Workspace settings.

A restore point is a full rollback snapshot. Kept in
``workspaces.settings._blueprint.restore_point`` it rode along with every
Workspace read; it now lives in the Workspace's upgrade state row, beside the
v3 receipt. The row keeps one undo at a time: when it already holds a v3
receipt, the settings copy was unreachable (undo takes the v3 receipt and
then drops the settings copy) and is removed.

Revision ID: 20261110_01
Revises: 20261109_01
"""
from __future__ import annotations

from alembic import op

revision = "20261110_01"
down_revision = "20261109_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO workspace_blueprint_upgrade_states
            (workspace_id, entity_id, revision, baseline, restore_point, created_at)
        SELECT w.id, w.entity_id, 1, '{}'::jsonb,
               w.settings -> '_blueprint' -> 'restore_point', now()
        FROM workspaces w
        WHERE jsonb_typeof(w.settings -> '_blueprint' -> 'restore_point') = 'object'
          AND NOT EXISTS (
              SELECT 1 FROM workspace_blueprint_upgrade_states s
              WHERE s.workspace_id = w.id
          )
        """
    )
    op.execute(
        """
        UPDATE workspace_blueprint_upgrade_states s
        SET restore_point = w.settings -> '_blueprint' -> 'restore_point',
            updated_at = now()
        FROM workspaces w
        WHERE s.workspace_id = w.id
          AND s.restore_point IS NULL
          AND jsonb_typeof(w.settings -> '_blueprint' -> 'restore_point') = 'object'
        """
    )
    op.execute(
        """
        UPDATE workspaces
        SET settings = jsonb_set(
            settings, '{_blueprint}', (settings -> '_blueprint') - 'restore_point'
        )
        WHERE jsonb_typeof(settings -> '_blueprint') = 'object'
          AND (settings -> '_blueprint') ? 'restore_point'
        """
    )


def downgrade() -> None:
    # Only legacy points go back; a v3 receipt names its protocol_version.
    op.execute(
        """
        UPDATE workspaces w
        SET settings = jsonb_set(w.settings, '{_blueprint,restore_point}', s.restore_point)
        FROM workspace_blueprint_upgrade_states s
        WHERE s.workspace_id = w.id
          AND jsonb_typeof(s.restore_point) = 'object'
          AND NOT (s.restore_point ? 'protocol_version')
          AND jsonb_typeof(w.settings -> '_blueprint') = 'object'
        """
    )
    op.execute(
        """
        UPDATE workspace_blueprint_upgrade_states
        SET restore_point = NULL
        WHERE jsonb_typeof(restore_point) = 'object'
          AND NOT (restore_point ? 'protocol_version')
        """
    )
