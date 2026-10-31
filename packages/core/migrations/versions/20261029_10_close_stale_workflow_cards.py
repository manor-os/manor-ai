"""Close the Workflow cards left open after their run ended or was replaced.

Readers no longer count these cards, and a run's end now closes them
(``close_stale_workflow_cards``). The cards already left open are closed here
once, in SQL, by the same rule:

* every open card of an ended run (completed, failed or cancelled) of a
  Workspace Flow that has run again since, from the same entry: closed as
  superseded, so a Flow that keeps failing keeps only its latest Retry;
* the questions and approvals of any ended run, and the Retry card of a
  cancelled run: closed as expired, or stopped when the run was cancelled.

The Personal Chat mirror of each card is marked resolved with it. Idempotent:
a second run finds no open card to close. Downgrade leaves the cards closed.

Revision ID: 20261029_10
Revises: 20261028_20 (public), 20261028_21 (private merge)
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import table_exists

revision = "20261029_10"
down_revision = "20261028_20"
branch_labels = None
depends_on = None

_ENDED = "('completed', 'failed', 'cancelled')"
_CLOSE = (
    "UPDATE messages m SET resolved_at = :now, "
    "resolution = jsonb_build_object('choice', {choice}), "
    "metadata = CASE WHEN jsonb_typeof(m.metadata->'hitl_requests') = 'array' "
    "  THEN jsonb_set(m.metadata, '{{hitl_requests}}', COALESCE(("
    "    SELECT jsonb_agg(CASE WHEN e->>'id' = m.id THEN e || '{{\"resolved\": true}}'::jsonb ELSE e END) "
    "    FROM jsonb_array_elements(m.metadata->'hitl_requests') e), '[]'::jsonb)) "
    "  ELSE m.metadata END "
    "FROM workflow_runs r "
    "WHERE m.resolved_at IS NULL AND m.pending_action->>'kind' IS NOT NULL "
    "AND r.id = m.pending_action->>'workflow_run_id' AND r.status IN " + _ENDED + " AND ({where})"
)


def upgrade() -> None:
    if not (table_exists("messages") and table_exists("workflow_runs")):
        return
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    bind.execute(sa.text(_CLOSE.format(choice="'superseded'", where=(
        "r.workspace_id IS NOT NULL AND EXISTS (SELECT 1 FROM workflow_runs n "
        "WHERE n.entity_id = r.entity_id AND n.workspace_id = r.workspace_id "
        "AND n.workflow_id = r.workflow_id AND n.binding_id IS NOT DISTINCT FROM r.binding_id "
        "AND (n.created_at > r.created_at OR (n.created_at = r.created_at AND n.id > r.id)))"
    ))), {"now": now})
    bind.execute(sa.text(_CLOSE.format(
        choice="CASE WHEN r.status = 'cancelled' THEN 'stopped' ELSE 'expired' END",
        where="m.pending_action->>'kind' <> 'workflow_retry' OR r.status = 'cancelled'",
    )), {"now": now})


def downgrade() -> None:
    pass
