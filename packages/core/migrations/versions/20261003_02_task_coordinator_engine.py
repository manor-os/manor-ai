"""Record which engine drives a Task coordinator run.

Existing runs keep the Plan/Step DAG engine. New runs choose their engine once,
from the task_agent_loop flag, when the row is created.

Revision ID: 20261003_02
Revises: 20261003_client_merge
"""

from alembic import op
import sqlalchemy as sa

from packages.core.migrations.helpers import column_exists, table_exists


revision = "20261003_02"
down_revision = "20261003_client_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists("task_coordinator_runs") and not column_exists(
        "task_coordinator_runs", "engine",
    ):
        op.add_column(
            "task_coordinator_runs",
            sa.Column(
                "engine",
                sa.String(length=20),
                nullable=False,
                server_default="plan_dag",
            ),
        )


def downgrade() -> None:
    if table_exists("task_coordinator_runs") and column_exists(
        "task_coordinator_runs", "engine",
    ):
        op.drop_column("task_coordinator_runs", "engine")
