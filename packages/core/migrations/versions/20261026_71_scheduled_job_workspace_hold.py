"""Scheduled jobs remember when only the Workspace lifecycle holds them off.

A Job created while its Workspace is provisioning or paused is stored
disabled. ``workspace_hold`` separates that lifecycle hold from a person's or
the system's own decision to disable it, so the Job starts when the Workspace
becomes active.

Existing rows keep ``false``: their disabled state cannot be attributed after
the fact, and turning on a Job someone stopped would be worse than leaving a
held one off.

Revision ID: 20261026_71
Revises: 20261031_10
"""
from __future__ import annotations

import sqlalchemy as sa

from packages.core.migrations.helpers import add_column_if_not_exists, drop_column_if_exists

revision = "20261026_71"
down_revision = "20261031_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_not_exists(
        "scheduled_jobs",
        sa.Column(
            "workspace_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    drop_column_if_exists("scheduled_jobs", "workspace_hold")
