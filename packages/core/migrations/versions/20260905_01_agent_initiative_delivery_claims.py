"""add bounded Agent Initiative delivery recovery claims

Revision ID: 20260905_01
Revises: 20260904_01
Create Date: 2026-09-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import (
    add_column_if_not_exists,
    create_index_if_not_exists,
    drop_column_if_exists,
    index_exists,
    table_exists,
)


revision: str = "20260905_01"
down_revision: Union[str, None] = "20260904_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not table_exists("agent_initiative_deliveries"):
        return

    add_column_if_not_exists(
        "agent_initiative_deliveries",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    add_column_if_not_exists(
        "agent_initiative_deliveries",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    add_column_if_not_exists(
        "agent_initiative_deliveries",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    add_column_if_not_exists(
        "agent_initiative_deliveries",
        sa.Column("claim_token", sa.String(length=26), nullable=True),
    )
    create_index_if_not_exists(
        "ix_agent_initiative_deliveries_due",
        "agent_initiative_deliveries",
        ["status", "next_attempt_at"],
    )
    create_index_if_not_exists(
        "ix_agent_initiative_deliveries_lease",
        "agent_initiative_deliveries",
        ["status", "locked_until"],
    )


def downgrade() -> None:
    if not table_exists("agent_initiative_deliveries"):
        return
    for index_name in (
        "ix_agent_initiative_deliveries_lease",
        "ix_agent_initiative_deliveries_due",
    ):
        if index_exists(index_name):
            op.drop_index(index_name, table_name="agent_initiative_deliveries")
    for column_name in (
        "claim_token",
        "locked_until",
        "next_attempt_at",
        "attempt_count",
    ):
        drop_column_if_exists("agent_initiative_deliveries", column_name)
