"""Add finite, project-owned scheduler fields.

Revision ID: 20261012_02
Revises: 20261007_03
"""
from alembic import op
import sqlalchemy as sa

revision = "20261012_02"
down_revision = "20261007_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_jobs", sa.Column("owner_project_id", sa.String(26), nullable=True))
    op.add_column("scheduled_jobs", sa.Column("idempotency_key", sa.String(200), nullable=True))
    op.add_column("scheduled_jobs", sa.Column("stop_after", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_scheduled_jobs_owner_project_id", "scheduled_jobs", ["owner_project_id"])
    op.create_index("uq_scheduled_jobs_workspace_idempotency", "scheduled_jobs", ["entity_id", "workspace_id", "idempotency_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_scheduled_jobs_workspace_idempotency", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_owner_project_id", table_name="scheduled_jobs")
    op.drop_column("scheduled_jobs", "stop_after")
    op.drop_column("scheduled_jobs", "idempotency_key")
    op.drop_column("scheduled_jobs", "owner_project_id")
