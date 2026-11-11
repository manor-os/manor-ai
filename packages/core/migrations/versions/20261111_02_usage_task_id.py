"""Tie public token usage rows to the Task whose execution made the call.

Revision ID: 20261111_02
Revises: 20261111_01
"""
from alembic import op

revision = "20261111_02"
down_revision = "20261111_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE token_usage_logs ADD COLUMN IF NOT EXISTS task_id VARCHAR(26)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_usage_task ON token_usage_logs (task_id) "
        "WHERE task_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_token_usage_task")
    op.execute("ALTER TABLE token_usage_logs DROP COLUMN IF EXISTS task_id")
