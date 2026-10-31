"""Record which external Workspace collaborators steered a Task.

``tasks.collaborator_ids`` lists the collaborators (membership role
``collaborator``) who created, edited, started, answered, approved or
commented on a Task. The runtime authorization gate narrows every run of
that Task to their live-granted Workspaces, whoever the run executes as.
NULL and an empty array both mean no collaborator steered the Task.

Revision ID: 20261018_01
Revises: 20261015_01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from packages.core.migrations.helpers import add_column_if_not_exists, column_exists


revision = "20261018_01"
down_revision = "20261015_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists(
        "tasks", sa.Column("collaborator_ids", postgresql.ARRAY(sa.String(26)), nullable=True)
    )


def downgrade() -> None:
    if column_exists("tasks", "collaborator_ids"):
        op.drop_column("tasks", "collaborator_ids")
