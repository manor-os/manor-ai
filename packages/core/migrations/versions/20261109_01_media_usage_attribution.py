"""Tie public token usage rows to their operation and media job.

Revision ID: 20261109_01
Revises: 20261108_01
"""
from alembic import op

revision = "20261109_01"
down_revision = "20261108_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_token_usage_operation ON token_usage_logs (operation_id)")
    op.execute("ALTER TABLE token_usage_logs ADD COLUMN IF NOT EXISTS media_job_id VARCHAR(26)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_token_usage_media_job ON token_usage_logs (media_job_id) "
        "WHERE media_job_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_token_usage_media_job")
    op.execute("ALTER TABLE token_usage_logs DROP COLUMN IF EXISTS media_job_id")
    op.execute("DROP INDEX IF EXISTS ix_token_usage_operation")
