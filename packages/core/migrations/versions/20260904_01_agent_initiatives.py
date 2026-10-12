"""add proactive Agent Initiative persistence

Revision ID: 20260904_01
Revises: deployment-specific predecessor
Create Date: 2026-09-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.core.migrations.helpers import (
    add_column_if_not_exists,
    column_exists,
    create_index_if_not_exists,
    drop_column_if_exists,
    foreign_key_exists,
    index_exists,
    table_exists,
)


revision: str = "20260904_01"
down_revision: Union[str, None] = "0003_oss_public_schema_reconcile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if table_exists("agent_subscriptions"):
        add_column_if_not_exists(
            "agent_subscriptions",
            sa.Column("owner_user_id", sa.String(length=26), nullable=True),
        )
        create_index_if_not_exists(
            "uq_agent_subscriptions_personal_owner",
            "agent_subscriptions",
            ["entity_id", "agent_id", "owner_user_id"],
            unique=True,
            postgresql_where=sa.text(
                "workspace_id IS NULL AND owner_user_id IS NOT NULL"
            ),
        )

    if not table_exists("agent_initiatives"):
        op.create_table(
            "agent_initiatives",
            sa.Column("id", sa.String(length=26), primary_key=True),
            sa.Column("entity_id", sa.String(length=26), nullable=False),
            sa.Column("workspace_id", sa.String(length=26), nullable=True),
            sa.Column(
                "agent_subscription_id",
                sa.String(length=26),
                nullable=False,
            ),
            sa.Column("owner_user_id", sa.String(length=26), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("trigger_kind", sa.String(length=20), nullable=False),
            sa.Column("trigger_source", sa.String(length=64), nullable=True),
            sa.Column(
                "trigger_config",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "condition_config",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "decision_policy",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "delivery_policy",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "interruption_policy",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="draft",
            ),
            sa.Column(
                "revision",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["agent_subscription_id"],
                ["agent_subscriptions.id"],
                name="fk_agent_initiatives_subscription",
                ondelete="CASCADE",
            ),
        )

    create_index_if_not_exists(
        "ix_agent_initiatives_owner_status",
        "agent_initiatives",
        ["entity_id", "owner_user_id", "status"],
    )
    create_index_if_not_exists(
        "ix_agent_initiatives_subscription_status",
        "agent_initiatives",
        ["entity_id", "agent_subscription_id", "status"],
    )
    create_index_if_not_exists(
        "ix_agent_initiatives_workspace_status",
        "agent_initiatives",
        ["workspace_id", "status"],
    )
    create_index_if_not_exists(
        "ix_agent_initiatives_trigger_status",
        "agent_initiatives",
        ["entity_id", "trigger_kind", "trigger_source", "status"],
    )

    if not table_exists("agent_initiative_states"):
        op.create_table(
            "agent_initiative_states",
            sa.Column("initiative_id", sa.String(length=26), primary_key=True),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_fingerprint", sa.String(length=64), nullable=True),
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "notification_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("last_silent_reason", sa.Text(), nullable=True),
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
            sa.ForeignKeyConstraint(
                ["initiative_id"],
                ["agent_initiatives.id"],
                name="fk_agent_initiative_states_initiative",
                ondelete="CASCADE",
            ),
        )

    if not table_exists("agent_initiative_runs"):
        op.create_table(
            "agent_initiative_runs",
            sa.Column("id", sa.String(length=26), primary_key=True),
            sa.Column("initiative_id", sa.String(length=26), nullable=False),
            sa.Column("scheduled_run_id", sa.String(length=26), nullable=True),
            sa.Column("task_id", sa.String(length=26), nullable=True),
            sa.Column("occurrence_key", sa.String(length=200), nullable=False),
            sa.Column("trigger_type", sa.String(length=20), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="admitted",
            ),
            sa.Column(
                "candidate_decision",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column("final_decision", sa.String(length=20), nullable=True),
            sa.Column("reason_code", sa.String(length=64), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["initiative_id"],
                ["agent_initiatives.id"],
                name="fk_agent_initiative_runs_initiative",
                ondelete="CASCADE",
            ),
        )

    create_index_if_not_exists(
        "uq_agent_initiative_runs_occurrence",
        "agent_initiative_runs",
        ["initiative_id", "occurrence_key"],
        unique=True,
    )
    create_index_if_not_exists(
        "ix_agent_initiative_runs_initiative_created",
        "agent_initiative_runs",
        ["initiative_id", "created_at"],
    )

    if not table_exists("agent_initiative_deliveries"):
        op.create_table(
            "agent_initiative_deliveries",
            sa.Column("id", sa.String(length=26), primary_key=True),
            sa.Column("initiative_run_id", sa.String(length=26), nullable=False),
            sa.Column("kind", sa.String(length=20), nullable=False),
            sa.Column("recipient_user_id", sa.String(length=26), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("idempotency_key", sa.String(length=255), nullable=False),
            sa.Column("message_id", sa.String(length=26), nullable=True),
            sa.Column("notification_id", sa.String(length=26), nullable=True),
            sa.Column("suppressed_reason", sa.Text(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["initiative_run_id"],
                ["agent_initiative_runs.id"],
                name="fk_agent_initiative_deliveries_run",
                ondelete="CASCADE",
            ),
        )

    create_index_if_not_exists(
        "uq_agent_initiative_deliveries_run_kind",
        "agent_initiative_deliveries",
        ["initiative_run_id", "kind"],
        unique=True,
    )
    create_index_if_not_exists(
        "uq_agent_initiative_deliveries_idempotency",
        "agent_initiative_deliveries",
        ["idempotency_key"],
        unique=True,
    )
    if table_exists("scheduled_jobs"):
        add_column_if_not_exists(
            "scheduled_jobs",
            sa.Column("agent_initiative_id", sa.String(length=26), nullable=True),
        )
        if (
            column_exists("scheduled_jobs", "agent_initiative_id")
            and not foreign_key_exists(
                "scheduled_jobs", "fk_scheduled_jobs_agent_initiative"
            )
        ):
            op.create_foreign_key(
                "fk_scheduled_jobs_agent_initiative",
                "scheduled_jobs",
                "agent_initiatives",
                ["agent_initiative_id"],
                ["id"],
                ondelete="CASCADE",
            )
        create_index_if_not_exists(
            "uq_scheduled_jobs_agent_initiative",
            "scheduled_jobs",
            ["agent_initiative_id"],
            unique=True,
            postgresql_where=sa.text("agent_initiative_id IS NOT NULL"),
        )


def downgrade() -> None:
    if table_exists("scheduled_jobs"):
        if index_exists("uq_scheduled_jobs_agent_initiative"):
            op.drop_index(
                "uq_scheduled_jobs_agent_initiative",
                table_name="scheduled_jobs",
            )
        if foreign_key_exists(
            "scheduled_jobs", "fk_scheduled_jobs_agent_initiative"
        ):
            op.drop_constraint(
                "fk_scheduled_jobs_agent_initiative",
                "scheduled_jobs",
                type_="foreignkey",
            )
        drop_column_if_exists("scheduled_jobs", "agent_initiative_id")

    for table_name in (
        "agent_initiative_deliveries",
        "agent_initiative_runs",
        "agent_initiative_states",
        "agent_initiatives",
    ):
        if table_exists(table_name):
            op.drop_table(table_name)

    if table_exists("agent_subscriptions"):
        if index_exists("uq_agent_subscriptions_personal_owner"):
            op.drop_index(
                "uq_agent_subscriptions_personal_owner",
                table_name="agent_subscriptions",
            )
        drop_column_if_exists("agent_subscriptions", "owner_user_id")
