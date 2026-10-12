"""allow sandbox reservations requested by task workers without a runtime run

Revision ID: 20260913_01
Revises: 20260911_04
Create Date: 2026-09-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260913_01"
down_revision: Union[str, None] = "20260917_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Task-worker subagents have no durable RuntimeRun, so a ticket they own
    # carries neither a run id nor a root run id.
    op.alter_column(
        "sandbox_reservations",
        "runtime_run_id",
        existing_type=sa.String(length=26),
        nullable=True,
    )
    op.alter_column(
        "sandbox_reservations",
        "root_run_id",
        existing_type=sa.String(length=26),
        nullable=True,
    )
    op.alter_column(
        "sandbox_instances",
        "runtime_run_id",
        existing_type=sa.String(length=26),
        nullable=True,
    )
    op.alter_column(
        "sandbox_instances",
        "root_run_id",
        existing_type=sa.String(length=26),
        nullable=True,
    )
    # The server default is kept, not dropped: it backfills existing rows and
    # keeps raw-SQL inserts and any not-yet-restarted writer correct during a
    # rolling deploy. The ORM always supplies the column explicitly anyway.
    op.add_column(
        "sandbox_reservations",
        sa.Column(
            "requester_kind",
            sa.String(length=16),
            nullable=False,
            server_default="run",
        ),
    )
    op.add_column(
        "sandbox_reservations",
        sa.Column("requester_id", sa.String(length=255), nullable=True),
    )
    # Run tickets keep deduplicating through uq_sandbox_reservations_run_tool;
    # task tickets have a NULL runtime_run_id (never equal under Postgres
    # semantics) and dedupe on the requester instead.
    op.create_index(
        "uq_sandbox_reservations_task_tool",
        "sandbox_reservations",
        ["requester_id", "tool_call_id"],
        unique=True,
        postgresql_where=sa.text("requester_kind = 'task'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sandbox_reservations_task_tool", table_name="sandbox_reservations")
    # Task-requester rows cannot be represented by the pre-migration schema
    # (NOT NULL run ids), so they are deleted before the columns are restored.
    # Their sandbox instances go first because they point back at the ticket.
    # The two IS NULL clauses only matter for instance rows written while this
    # migration was applied: before the upgrade the columns were NOT NULL, so
    # any nullable row is by definition a task-requester one.
    op.execute(
        sa.text(
            """
            DELETE FROM sandbox_instances
            WHERE runtime_run_id IS NULL
               OR root_run_id IS NULL
               OR reservation_id IN (
                   SELECT id FROM sandbox_reservations WHERE requester_kind = 'task'
               )
            """
        )
    )
    op.execute(
        sa.text(
            """
            DELETE FROM sandbox_reservations
            WHERE requester_kind = 'task'
               OR runtime_run_id IS NULL
               OR root_run_id IS NULL
            """
        )
    )
    op.drop_column("sandbox_reservations", "requester_id")
    op.drop_column("sandbox_reservations", "requester_kind")
    op.alter_column(
        "sandbox_instances",
        "root_run_id",
        existing_type=sa.String(length=26),
        nullable=False,
    )
    op.alter_column(
        "sandbox_instances",
        "runtime_run_id",
        existing_type=sa.String(length=26),
        nullable=False,
    )
    op.alter_column(
        "sandbox_reservations",
        "root_run_id",
        existing_type=sa.String(length=26),
        nullable=False,
    )
    op.alter_column(
        "sandbox_reservations",
        "runtime_run_id",
        existing_type=sa.String(length=26),
        nullable=False,
    )
