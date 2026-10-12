"""Share scoped compaction archives with durable main Agent runs.

Revision ID: 20260926_03
Revises: 20260926_02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260926_03"
down_revision = "20260926_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runtime_runs", sa.Column("context_state", JSONB, nullable=True))
    op.alter_column("agent_context_archives", "session_id", nullable=True)
    op.add_column("agent_context_archives", sa.Column("runtime_run_id", sa.String(26), nullable=True))
    op.create_foreign_key("fk_context_archive_runtime_run", "agent_context_archives", "runtime_runs", ["runtime_run_id"], ["id"], ondelete="CASCADE")
    op.create_unique_constraint("uq_runtime_context_archive_digest", "agent_context_archives", ["runtime_run_id", "digest"])
    op.create_check_constraint("ck_context_archive_owner", "agent_context_archives", "(session_id IS NULL) <> (runtime_run_id IS NULL)")


def downgrade() -> None:
    op.drop_column("runtime_runs", "context_state")
    op.execute("DELETE FROM agent_context_archives WHERE session_id IS NULL")
    op.drop_constraint("ck_context_archive_owner", "agent_context_archives", type_="check")
    op.drop_constraint("uq_runtime_context_archive_digest", "agent_context_archives", type_="unique")
    op.drop_constraint("fk_context_archive_runtime_run", "agent_context_archives", type_="foreignkey")
    op.drop_column("agent_context_archives", "runtime_run_id")
    op.alter_column("agent_context_archives", "session_id", nullable=False)
