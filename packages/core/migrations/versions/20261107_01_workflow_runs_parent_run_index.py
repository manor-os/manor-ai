"""Index the parent run of each workflow run, which the App's subflow lookups filter on.

Revision ID: 20261107_01
Revises: 20261104_20
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20261107_01"
down_revision: Union[str, None] = "20261104_20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workflow_runs_parent_run_id "
        "ON workflow_runs ((trigger_data->>'parent_run_id'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_workflow_runs_parent_run_id")
