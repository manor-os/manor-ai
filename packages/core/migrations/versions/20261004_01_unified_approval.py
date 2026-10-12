"""One approval policy with persistent Room root binding and fenced conversion.

Revision ID: 20261004_01
Revises: 20261003_01
"""
import json
import os
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.migrations.helpers import column_exists, table_exists
from packages.core.migrations.unified_approval import apply, preflight

revision = '20261004_01'
down_revision = '20261003_01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if table_exists('chat_rooms') and not column_exists('chat_rooms', 'approval_policy'):
        op.add_column('chat_rooms', sa.Column('approval_policy', JSONB, nullable=True))
    if table_exists('chat_room_tool_approvals') and not column_exists('chat_room_tool_approvals', 'approval_requirements'):
        op.add_column('chat_room_tool_approvals', sa.Column('approval_requirements', JSONB, nullable=True))
    if table_exists('execution_steps') and not column_exists('execution_steps', 'approval_operation_revision'):
        op.add_column('execution_steps', sa.Column('approval_operation_revision', sa.Integer, nullable=False, server_default='0'))
    connection = op.get_bind()
    if table_exists('hitl_requests') and 'ix_hitl_requests_entity_operation' not in {index['name'] for index in sa.inspect(connection).get_indexes('hitl_requests')}:
        op.create_index('ix_hitl_requests_entity_operation', 'hitl_requests', ['entity_id', 'dedup_key', 'created_at'])
    plan_path = os.environ.get('MANOR_APPROVAL_MIGRATION_PLAN')
    reviewed = json.loads(Path(plan_path).read_text()) if plan_path else preflight(connection)
    # A blocker raises inside Alembic's transaction, including the new column.
    # The operator resolves the explicit preflight list while execution stays
    # offline; there is no legacy online evaluator after this release.
    apply(connection, reviewed)


def downgrade() -> None:
    raise RuntimeError('Approval decisions cannot be rolled back to the legacy interpreter; apply a forward repair')
