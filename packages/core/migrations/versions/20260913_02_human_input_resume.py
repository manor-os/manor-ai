"""park a run or plan step on a human question and resume it with the answer

Revision ID: 20260913_02
Revises: 20260913_01
Create Date: 2026-09-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260913_02"
down_revision: Union[str, None] = "20260913_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A durable chat run parked by request_input points at the open
    # HitlRequest that holds the question; the answer resumes the run.
    op.add_column(
        "runtime_runs",
        sa.Column("human_input_request_id", sa.String(length=26), nullable=True),
    )
    # A plan subagent step parked by request_input keeps its agent transcript
    # here so the answer resumes the loop at the pending call instead of
    # re-running the whole step.
    op.add_column(
        "execution_steps",
        sa.Column("runtime_checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_steps", "runtime_checkpoint")
    op.drop_column("runtime_runs", "human_input_request_id")
