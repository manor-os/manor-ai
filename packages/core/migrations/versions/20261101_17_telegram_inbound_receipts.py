"""deduplicate Telegram inbound updates by their update_id receipt

Revision ID: 20261101_17
Revises: 20261031_30
Create Date: 2026-11-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import index_exists, table_exists


revision: str = "20261101_17"
down_revision: Union[str, None] = "20261031_30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("message_logs"):
        return
    # Only receipts keyed "update:<id>" are unique; earlier Telegram rows hold
    # per-chat message ids and are left as they are.
    if not index_exists("uq_message_logs_telegram_inbound_update"):
        op.create_index(
            "uq_message_logs_telegram_inbound_update",
            "message_logs",
            ["channel_config_id", "external_id"],
            unique=True,
            postgresql_where=sa.text(
                "channel_type = 'telegram' AND direction = 'inbound' "
                "AND external_id LIKE 'update:%'"
            ),
        )


def downgrade() -> None:
    if table_exists("message_logs") and index_exists(
        "uq_message_logs_telegram_inbound_update"
    ):
        op.drop_index(
            "uq_message_logs_telegram_inbound_update",
            table_name="message_logs",
        )
