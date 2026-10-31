"""deduplicate email inbound callback receipts

Revision ID: 20261026_10
Revises: 20261025_20
Create Date: 2026-10-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import (
    create_index_if_not_exists,
    index_exists,
    table_exists,
)


revision: str = "20261026_10"
down_revision: Union[str, None] = "20261025_20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("message_logs"):
        return
    op.execute(sa.text("""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY channel_config_id, external_id
                       ORDER BY created_at ASC, id ASC
                   ) AS duplicate_rank
              FROM message_logs
             WHERE channel_type = 'email'
               AND direction = 'inbound'
               AND external_id IS NOT NULL
        )
        DELETE FROM message_logs
         WHERE id IN (
             SELECT id FROM ranked WHERE duplicate_rank > 1
         )
    """))
    create_index_if_not_exists(
        "uq_message_logs_email_inbound_message",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "channel_type = 'email' AND direction = 'inbound' "
            "AND external_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    if index_exists("uq_message_logs_email_inbound_message"):
        op.drop_index(
            "uq_message_logs_email_inbound_message",
            table_name="message_logs",
        )
