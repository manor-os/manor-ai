"""Index the messages that still carry an open decision card.

Counting a Workspace's open decisions filtered every message of every
Workspace conversation with a JSON path over its full metadata, so the
Workspace list and each chat page read the whole chat history. The partial
index holds only messages with an open card.

Revision ID: 20261111_01
Revises: 20261110_01
"""

from alembic import op
import sqlalchemy as sa

from packages.core.migrations.helpers import table_exists


revision = "20261111_01"
down_revision = "20261110_01"
branch_labels = None
depends_on = None


_INDEX = "ix_messages_open_cards"
# Frozen copy of packages.core.models.task.MESSAGE_OPEN_CARD_CONDITION.
_CONDITION = (
    "((messages.resolved_at IS NULL AND (messages.pending_action ->> 'kind') IS NOT NULL)"
    " OR jsonb_path_exists(messages.metadata,"
    " '$.hitl_requests[*] ? (exists(@.id) && (!exists(@.resolved) || @.resolved == false))'::jsonpath))"
)


def _invalid_index(name: str) -> bool:
    result = op.get_bind().execute(sa.text("""
        SELECT NOT index_state.indisvalid
        FROM pg_catalog.pg_index AS index_state
        JOIN pg_catalog.pg_class AS index_relation
          ON index_relation.oid = index_state.indexrelid
        JOIN pg_catalog.pg_namespace AS index_namespace
          ON index_namespace.oid = index_relation.relnamespace
        WHERE index_relation.relname = :index_name
          AND index_namespace.nspname = current_schema()
    """), {"index_name": name})
    return result.scalar_one_or_none() is True


def upgrade() -> None:
    if not table_exists("messages"):
        return
    with op.get_context().autocommit_block():
        if _invalid_index(_INDEX):
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
        op.execute(sa.text(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON messages (conversation_id) WHERE {_CONDITION}"
        ))


def downgrade() -> None:
    if not table_exists("messages"):
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
