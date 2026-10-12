"""Reconcile public model additions after the frozen OSS schema sync.

Revision ID: 0003_oss_public_schema_reconcile
Revises: 0002_oss_schema_sync
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.core.migrations.helpers import (
    add_column_if_not_exists,
    create_index_if_not_exists,
    table_exists,
)


revision: str = "0003_oss_public_schema_reconcile"
down_revision: str | None = "0002_oss_schema_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_marketplace_resource_links() -> None:
    if not table_exists("marketplace_resource_links"):
        op.create_table(
            "marketplace_resource_links",
            sa.Column("id", sa.String(26), nullable=False),
            sa.Column("entity_id", sa.String(26), nullable=False),
            sa.Column(
                "marketplace_source",
                sa.String(40),
                nullable=False,
                server_default="platform",
            ),
            sa.Column(
                "marketplace_resource_type",
                sa.String(40),
                nullable=False,
            ),
            sa.Column("marketplace_resource_id", sa.String(160), nullable=False),
            sa.Column("relationship", sa.String(30), nullable=False),
            sa.Column("scope_type", sa.String(30), nullable=False),
            sa.Column("scope_id", sa.String(160), nullable=False),
            sa.Column("local_resource_type", sa.String(40), nullable=False),
            sa.Column("local_resource_id", sa.String(160), nullable=False),
            sa.Column(
                "component_key",
                sa.String(160),
                nullable=False,
                server_default="root",
            ),
            sa.Column("marketplace_version", sa.String(40), nullable=True),
            sa.Column("linked_by", sa.String(26), nullable=True),
            sa.Column(
                "link_metadata",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "entity_id",
                "marketplace_source",
                "marketplace_resource_type",
                "marketplace_resource_id",
                "relationship",
                "scope_type",
                "scope_id",
                "local_resource_type",
                "component_key",
                name="uq_marketplace_resource_links_source_scope",
            ),
            sa.UniqueConstraint(
                "entity_id",
                "relationship",
                "scope_type",
                "scope_id",
                "local_resource_type",
                "local_resource_id",
                name="uq_marketplace_resource_links_local_scope",
            ),
        )
    create_index_if_not_exists(
        "ix_marketplace_resource_links_entity_id",
        "marketplace_resource_links",
        ["entity_id"],
    )
    create_index_if_not_exists(
        "ix_marketplace_resource_links_linked_by",
        "marketplace_resource_links",
        ["linked_by"],
    )
    create_index_if_not_exists(
        "ix_marketplace_resource_links_local",
        "marketplace_resource_links",
        ["entity_id", "local_resource_type", "local_resource_id", "relationship"],
    )
    create_index_if_not_exists(
        "ix_marketplace_resource_links_source",
        "marketplace_resource_links",
        [
            "marketplace_source",
            "marketplace_resource_type",
            "marketplace_resource_id",
        ],
    )


def _create_product_growth_events() -> None:
    if not table_exists("product_growth_events"):
        op.create_table(
            "product_growth_events",
            sa.Column("id", sa.String(length=26), nullable=False),
            sa.Column("entity_id", sa.String(length=26), nullable=False),
            sa.Column("workspace_id", sa.String(length=26), nullable=True),
            sa.Column("user_id", sa.String(length=26), nullable=False),
            sa.Column("milestone", sa.String(length=50), nullable=False),
            sa.Column("source_kind", sa.String(length=50), nullable=False),
            sa.Column("source_id", sa.String(length=100), nullable=False),
            sa.Column(
                "occurred_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    create_index_if_not_exists(
        "uq_product_growth_event_source",
        "product_growth_events",
        ["entity_id", "milestone", "source_kind", "source_id"],
        unique=True,
    )
    create_index_if_not_exists(
        "ix_product_growth_user_milestone_occurred",
        "product_growth_events",
        ["user_id", "milestone", "occurred_at"],
    )

    if table_exists("scheduled_jobs"):
        op.execute(sa.text("""
            INSERT INTO product_growth_events (
                id,
                entity_id,
                workspace_id,
                user_id,
                milestone,
                source_kind,
                source_id,
                occurred_at
            )
            SELECT
                substring(md5('automation_created:' || job.id), 1, 26),
                job.entity_id,
                job.workspace_id,
                job.user_id,
                'automation_created',
                'scheduled_job',
                job.id,
                job.created_at
            FROM scheduled_jobs AS job
            WHERE job.entity_id IS NOT NULL
              AND job.user_id IS NOT NULL
            ON CONFLICT (
                entity_id,
                milestone,
                source_kind,
                source_id
            ) DO NOTHING
        """))

    if table_exists("scheduled_jobs") and table_exists("scheduled_job_runs"):
        op.execute(sa.text("""
            INSERT INTO product_growth_events (
                id,
                entity_id,
                workspace_id,
                user_id,
                milestone,
                source_kind,
                source_id,
                occurred_at
            )
            SELECT
                substring(md5('automation_succeeded:' || run.id), 1, 26),
                job.entity_id,
                job.workspace_id,
                job.user_id,
                'automation_succeeded',
                'scheduled_job_run',
                run.id,
                coalesce(run.completed_at, run.started_at, run.created_at)
            FROM scheduled_job_runs AS run
            JOIN scheduled_jobs AS job ON job.job_id = run.job_id
            WHERE job.entity_id IS NOT NULL
              AND job.user_id IS NOT NULL
              AND run.status IN ('success', 'completed')
            ON CONFLICT (
                entity_id,
                milestone,
                source_kind,
                source_id
            ) DO NOTHING
        """))

    if table_exists("workspace_activities"):
        op.execute(sa.text("""
            INSERT INTO product_growth_events (
                id,
                entity_id,
                workspace_id,
                user_id,
                milestone,
                source_kind,
                source_id,
                occurred_at
            )
            SELECT
                substring(md5('legacy_growth_activity:' || activity.id), 1, 26),
                activity.entity_id,
                activity.workspace_id,
                activity.user_id,
                activity.event_type,
                'legacy_workspace_activity',
                activity.id,
                activity.created_at
            FROM workspace_activities AS activity
            WHERE activity.entity_id IS NOT NULL
              AND activity.user_id IS NOT NULL
              AND activity.event_type IN (
                  'automation_created',
                  'automation_succeeded'
              )
            ON CONFLICT (
                entity_id,
                milestone,
                source_kind,
                source_id
            ) DO NOTHING
        """))


def _add_document_upload_receipts() -> None:
    if not table_exists("documents"):
        return
    add_column_if_not_exists(
        "documents",
        sa.Column("upload_idempotency_key", sa.String(128), nullable=True),
    )
    add_column_if_not_exists(
        "documents",
        sa.Column("upload_request_fingerprint", sa.String(64), nullable=True),
    )
    create_index_if_not_exists(
        "uq_documents_upload_idempotency",
        "documents",
        ["entity_id", "owner_id", "upload_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("upload_idempotency_key IS NOT NULL"),
    )


def _ensure_agent_subscription_workspace_scope() -> None:
    if not table_exists("agent_subscriptions"):
        return
    bind = op.get_bind()
    expected_columns = ("entity_id", "agent_id", "workspace_id")
    if any(
        tuple(constraint.get("column_names") or ()) == expected_columns
        for constraint in sa.inspect(bind).get_unique_constraints(
            "agent_subscriptions"
        )
    ):
        return
    duplicate = bind.execute(sa.text("""
        SELECT 1
          FROM agent_subscriptions
         WHERE workspace_id IS NOT NULL
         GROUP BY entity_id, agent_id, workspace_id
        HAVING count(*) > 1
         LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot enforce agent subscription workspace uniqueness: "
            "duplicate (entity_id, agent_id, workspace_id) rows exist"
        )
    op.create_unique_constraint(
        "uq_agent_subscriptions_workspace_scope",
        "agent_subscriptions",
        list(expected_columns),
    )


def upgrade() -> None:
    _create_marketplace_resource_links()
    _create_product_growth_events()
    _add_document_upload_receipts()
    _ensure_agent_subscription_workspace_scope()


def downgrade() -> None:
    # Public upgrades are forward-only. These rows include durable provenance
    # and activation facts, so rollback is performed from the pre-upgrade DB
    # backup rather than by destructively deleting them.
    pass
