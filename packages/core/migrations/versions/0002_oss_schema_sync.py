"""Synchronize the published OSS schema with current public models.

Revision ID: 0002_oss_schema_sync
Revises: 0001_oss_initial
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from packages.core.schedule_clock import scheduled_job_next_run_at

revision: str = "0002_oss_schema_sync"
down_revision: str | None = "0001_oss_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_scheduled_job_clocks() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("""
        SELECT id, enabled, schedule_kind, cron_expr, every_seconds, run_at,
               timezone, last_run_at, delete_after_run
          FROM scheduled_jobs
         WHERE next_run_at IS NULL
    """)
    ).mappings()
    now = datetime.now(UTC)
    while batch := rows.fetchmany(1000):
        updates = []
        for values in batch:
            next_run_at = scheduled_job_next_run_at(
                SimpleNamespace(**values),
                now=now,
                inclusive=True,
            )
            if next_run_at is not None:
                updates.append({"job_id": values["id"], "next_run_at": next_run_at})
        if updates:
            bind.execute(
                sa.text("""
                    UPDATE scheduled_jobs
                       SET next_run_at = :next_run_at
                     WHERE id = :job_id
                """),
                updates,
            )


def upgrade() -> None:
    # These Cloud marketplace tables were present in the published 0001
    # snapshot. Remove them additively here because 0001 is immutable.
    op.drop_table("blueprint_purchases")
    op.drop_table("merchant_accounts")

    op.create_table(
        "runtime_execution_claims",
        sa.Column("claim_key", sa.String(length=255), nullable=False),
        sa.Column("claim_token", sa.String(length=26), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("claim_key"),
    )
    op.create_index("ix_runtime_execution_claims_expires", "runtime_execution_claims", ["expires_at"], unique=False)
    op.create_table(
        "runtime_outbox_events",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("aggregate_id", sa.String(length=255), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_runtime_outbox_dedupe_key"),
    )
    op.create_index(
        "ix_runtime_outbox_pending",
        "runtime_outbox_events",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.create_table(
        "runtime_runs",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("root_run_id", sa.String(length=26), nullable=False),
        sa.Column("parent_run_id", sa.String(length=26), nullable=True),
        sa.Column("conversation_id", sa.String(length=26), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=26), nullable=True),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("agent_id", sa.String(length=26), nullable=True),
        sa.Column("workspace_id", sa.String(length=26), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_reason", sa.String(length=80), nullable=True),
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("execution_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reservation_id", sa.String(length=26), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_sandbox_id", sa.String(length=255), nullable=True),
        sa.Column("active_execution_id", sa.String(length=128), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runtime_runs_conversation_created", "runtime_runs", ["conversation_id", "created_at"], unique=False
    )
    op.create_index("ix_runtime_runs_entity_status", "runtime_runs", ["entity_id", "status"], unique=False)
    op.create_index("ix_runtime_runs_root_status", "runtime_runs", ["root_run_id", "status"], unique=False)
    op.create_index(
        "uq_runtime_runs_active_conversation",
        "runtime_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(
            "parent_run_id IS NULL AND status IN ('queued','running','waiting_resource','resuming','cancel_requested','cancelling')"
        ),
    )
    op.create_table(
        "sandbox_instances",
        sa.Column("sandbox_id", sa.String(length=255), nullable=False),
        sa.Column("reservation_id", sa.String(length=26), nullable=False),
        sa.Column("runtime_run_id", sa.String(length=26), nullable=False),
        sa.Column("root_run_id", sa.String(length=26), nullable=False),
        sa.Column("runner_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("sandbox_id"),
        sa.UniqueConstraint("reservation_id"),
    )
    op.create_index("ix_sandbox_instances_root_status", "sandbox_instances", ["root_run_id", "status"], unique=False)
    op.create_index("ix_sandbox_instances_runner_status", "sandbox_instances", ["runner_id", "status"], unique=False)
    op.create_table(
        "sandbox_reservations",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("runtime_run_id", sa.String(length=26), nullable=False),
        sa.Column("root_run_id", sa.String(length=26), nullable=False),
        sa.Column("tool_call_id", sa.String(length=255), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runner_id", sa.String(length=80), nullable=True),
        sa.Column("sandbox_id", sa.String(length=255), nullable=True),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("runtime_run_id", "tool_call_id", name="uq_sandbox_reservations_run_tool"),
    )
    op.create_index(
        "ix_sandbox_reservations_queue",
        "sandbox_reservations",
        ["priority", "enqueued_at", "id"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued','requeued')"),
    )
    op.create_index(
        "ix_sandbox_reservations_root_status", "sandbox_reservations", ["root_run_id", "status"], unique=False
    )
    op.create_index(
        "uq_sandbox_reservations_sandbox",
        "sandbox_reservations",
        ["sandbox_id"],
        unique=True,
        postgresql_where=sa.text("sandbox_id IS NOT NULL"),
    )
    op.create_table(
        "sandbox_runners",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("active_limit", sa.Integer(), nullable=False),
        sa.Column("executing_limit", sa.Integer(), nullable=False),
        sa.Column("active_count", sa.Integer(), nullable=False),
        sa.Column("executing_count", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sandbox_runners_status", "sandbox_runners", ["status", "last_health_at"], unique=False)
    op.create_index("uq_sandbox_runners_base_url", "sandbox_runners", ["base_url"], unique=True)
    op.create_table(
        "twilio_voice_call_sessions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("owner_user_id", sa.String(length=26), nullable=True),
        sa.Column("workspace_id", sa.String(length=26), nullable=True),
        sa.Column("channel_config_id", sa.String(length=26), nullable=False),
        sa.Column("agent_id", sa.String(length=26), nullable=True),
        sa.Column("conversation_id", sa.String(length=26), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("call_sid", sa.String(length=64), nullable=True),
        sa.Column("stream_sid", sa.String(length=64), nullable=True),
        sa.Column("session_token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_number", sa.String(length=40), nullable=True),
        sa.Column("to_number", sa.String(length=40), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_token_hash"),
    )
    op.create_index(
        op.f("ix_twilio_voice_call_sessions_channel_config_id"),
        "twilio_voice_call_sessions",
        ["channel_config_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_twilio_voice_call_sessions_entity_id"), "twilio_voice_call_sessions", ["entity_id"], unique=False
    )
    op.create_index(
        op.f("ix_twilio_voice_call_sessions_owner_user_id"),
        "twilio_voice_call_sessions",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_twilio_voice_call_sessions_workspace_id"), "twilio_voice_call_sessions", ["workspace_id"], unique=False
    )
    op.create_index(
        "ix_twilio_voice_sessions_config_status",
        "twilio_voice_call_sessions",
        ["channel_config_id", "status"],
        unique=False,
    )
    op.create_index("ix_twilio_voice_sessions_expires", "twilio_voice_call_sessions", ["expires_at"], unique=False)
    op.create_index(
        "uq_twilio_voice_sessions_call_sid",
        "twilio_voice_call_sessions",
        ["call_sid"],
        unique=True,
        postgresql_where=sa.text("call_sid IS NOT NULL"),
    )
    op.create_table(
        "wechat_personal_sessions",
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("owner_user_id", sa.String(length=26), nullable=False),
        sa.Column("integration_id", sa.String(length=26), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("integration_id"),
    )
    op.create_table(
        "workspace_artifact_purge_jobs",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("storage_base", sa.String(length=1100), nullable=False),
        sa.Column("target_kind", sa.String(length=16), server_default="tree", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspace_artifact_purge_due",
        "workspace_artifact_purge_jobs",
        ["next_attempt_at", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_workspace_artifact_purge_scope", "workspace_artifact_purge_jobs", ["entity_id", "storage_base"], unique=True
    )
    op.create_table(
        "mcp_account_tool_catalogs",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("oauth_account_id", sa.String(length=26), nullable=True),
        sa.Column("integration_id", sa.String(length=26), nullable=True),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("tools_cached", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("tools_cached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(oauth_account_id IS NOT NULL AND integration_id IS NULL) OR (oauth_account_id IS NULL AND integration_id IS NOT NULL)",
            name="ck_mcp_account_tool_catalogs_one_source",
        ),
        sa.ForeignKeyConstraint(["integration_id"], ["integrations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["oauth_account_id"], ["oauth_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "integration_id", name="uq_mcp_account_tool_catalogs_integration"),
        sa.UniqueConstraint("provider", "oauth_account_id", name="uq_mcp_account_tool_catalogs_oauth"),
    )
    op.create_index("ix_mcp_account_tool_catalogs_provider", "mcp_account_tool_catalogs", ["provider"], unique=False)
    op.create_table(
        "notification_outbox_events",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("notification_id", sa.String(length=26), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=26), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_outbox_due", "notification_outbox_events", ["status", "available_at"], unique=False
    )
    op.create_index(
        "ix_notification_outbox_lease", "notification_outbox_events", ["status", "locked_until"], unique=False
    )
    op.create_index(
        "uq_notification_outbox_notification", "notification_outbox_events", ["notification_id"], unique=True
    )
    op.create_table(
        "user_session_leases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("session_id", sa.String(length=26), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["user_session_logs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_session_lease_expires", "user_session_leases", ["expires_at"], unique=False)
    op.create_index(
        "ix_user_session_lease_scope_expiry",
        "user_session_leases",
        ["entity_id", "user_id", "expires_at"],
        unique=False,
    )
    op.create_index("ix_user_session_lease_session", "user_session_leases", ["session_id"], unique=False)
    op.alter_column(
        "audit_log",
        "resource_id",
        existing_type=sa.VARCHAR(length=26),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.add_column("integrations", sa.Column("owner_user_id", sa.String(length=26), nullable=True))
    op.create_index(
        "ix_integrations_entity_owner_provider",
        "integrations",
        ["entity_id", "owner_user_id", "provider"],
        unique=False,
    )
    op.execute(
        sa.text("""
        UPDATE integrations
           SET owner_user_id = created_by_user_id
         WHERE owner_user_id IS NULL
           AND created_by_user_id IS NOT NULL
    """)
    )
    op.add_column("channel_configs", sa.Column("owner_user_id", sa.String(length=26), nullable=True))
    op.add_column("channel_configs", sa.Column("credential_source_kind", sa.String(length=32), nullable=True))
    op.add_column("channel_configs", sa.Column("credential_source_id", sa.String(length=26), nullable=True))
    op.add_column("channel_configs", sa.Column("telegram_bot_id", sa.String(length=32), nullable=True))
    op.add_column("channel_configs", sa.Column("discord_application_id", sa.String(length=32), nullable=True))
    op.add_column("channel_configs", sa.Column("discord_guild_id", sa.String(length=32), nullable=True))
    op.add_column("channel_configs", sa.Column("whatsapp_phone_number_id", sa.String(length=32), nullable=True))
    op.create_index(
        "ix_channel_configs_entity_owner_source",
        "channel_configs",
        ["entity_id", "owner_user_id", "credential_source_kind", "credential_source_id"],
        unique=False,
    )
    op.execute(
        sa.text("""
        UPDATE channel_configs AS channel_config
           SET owner_user_id = integration.owner_user_id,
               credential_source_kind = 'integration',
               credential_source_id = integration.id
          FROM integrations AS integration
         WHERE channel_config.owner_user_id IS NULL
           AND integration.owner_user_id IS NOT NULL
           AND integration.entity_id = channel_config.entity_id
           AND integration.id = channel_config.config->>'integration_id'
    """)
    )
    op.execute(
        sa.text("""
        WITH candidates AS (
            SELECT credentials->>'phone_number_id' AS phone_number_id
              FROM channel_configs
             WHERE channel_type = 'whatsapp'
               AND whatsapp_phone_number_id IS NULL
               AND NULLIF(TRIM(credentials->>'phone_number_id'), '') IS NOT NULL
             GROUP BY credentials->>'phone_number_id'
            HAVING COUNT(*) = 1
        )
        UPDATE channel_configs AS channel_config
           SET whatsapp_phone_number_id = NULLIF(
               TRIM(channel_config.credentials->>'phone_number_id'), ''
           )
         WHERE channel_config.channel_type = 'whatsapp'
           AND channel_config.whatsapp_phone_number_id IS NULL
           AND NULLIF(TRIM(channel_config.credentials->>'phone_number_id'), '') IN (
               SELECT phone_number_id FROM candidates
           )
    """)
    )
    op.create_index(
        "ux_channel_configs_discord_installation",
        "channel_configs",
        ["discord_application_id", "discord_guild_id"],
        unique=True,
    )
    op.create_index("ux_channel_configs_telegram_bot_id", "channel_configs", ["telegram_bot_id"], unique=True)
    op.create_index(
        "ux_channel_configs_whatsapp_phone_number_id", "channel_configs", ["whatsapp_phone_number_id"], unique=True
    )
    op.execute(
        sa.text("""
        WITH ambiguous AS (
            SELECT config->>'channel_config_id' AS channel_config_id
              FROM channels
             WHERE type = 'whatsapp'
               AND status = 'active'
               AND config->>'channel_config_id' IS NOT NULL
             GROUP BY config->>'channel_config_id'
            HAVING COUNT(*) > 1
        )
        UPDATE channels AS binding
           SET status = 'inactive'
          FROM ambiguous
         WHERE binding.type = 'whatsapp'
           AND binding.status = 'active'
           AND binding.config->>'channel_config_id' = ambiguous.channel_config_id
    """)
    )
    op.execute(
        sa.text("""
        UPDATE channel_contacts
           SET agent_subscription_id = NULL
         WHERE channel_type = 'whatsapp'
           AND agent_subscription_id IS NOT NULL
    """)
    )
    op.create_index(
        "ux_channels_active_whatsapp_config",
        "channels",
        [sa.literal_column("(config ->> 'channel_config_id')")],
        unique=True,
        postgresql_where=sa.text(
            "type = 'whatsapp' AND status = 'active' AND config ->> 'channel_config_id' IS NOT NULL"
        ),
        sqlite_where=sa.text("type = 'whatsapp' AND status = 'active' AND config ->> 'channel_config_id' IS NOT NULL"),
    )
    op.execute(
        sa.text("""
        UPDATE channels AS binding
           SET user_id = config.owner_user_id
          FROM channel_configs AS config
         WHERE binding.user_id IS NULL
           AND config.owner_user_id IS NOT NULL
           AND binding.entity_id = config.entity_id
           AND binding.type = config.channel_type
           AND binding.config->>'channel_config_id' = config.id
    """)
    )
    op.add_column("chat_message_feedback", sa.Column("target_kind", sa.String(length=32), nullable=True))
    op.add_column("chat_message_feedback", sa.Column("target_id", sa.String(length=64), nullable=True))
    op.add_column("chat_message_feedback", sa.Column("task_id", sa.String(length=26), nullable=True))
    op.add_column("chat_message_feedback", sa.Column("plan_id", sa.String(length=26), nullable=True))
    op.add_column("chat_message_feedback", sa.Column("mutation_sequence", sa.BigInteger(), nullable=True))
    op.execute(
        sa.text("""
        UPDATE chat_message_feedback AS feedback
           SET target_kind = CASE
                   WHEN feedback.metadata->>'target_kind' IN (
                       'task_completion', 'plan_completion'
                   ) THEN feedback.metadata->>'target_kind'
                   ELSE 'response'
               END,
               plan_id = refs.plan_id,
               task_id = refs.task_id,
               target_id = CASE
                   WHEN feedback.metadata->>'target_kind' IN (
                       'task_completion', 'plan_completion'
                   ) THEN COALESCE(refs.plan_id, refs.task_id, feedback.message_id)
                   ELSE feedback.message_id
               END,
               mutation_sequence = COALESCE(feedback.mutation_sequence, 1)
          FROM messages AS message
          LEFT JOIN LATERAL (
              SELECT
                  max(ref->>'id') FILTER (WHERE ref->>'type' = 'plan') AS plan_id,
                  max(ref->>'id') FILTER (WHERE ref->>'type' = 'task') AS task_id
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(message.refs) = 'array' THEN message.refs
                        ELSE '[]'::jsonb
                    END
                ) AS ref
          ) AS refs ON TRUE
         WHERE message.id = feedback.message_id
    """)
    )
    op.execute(
        sa.text("""
        UPDATE chat_message_feedback AS feedback
           SET target_kind = 'task_completion',
               task_id = plan.task_id
          FROM execution_plans AS plan
         WHERE feedback.plan_id = plan.id
           AND feedback.target_kind IN ('task_completion', 'plan_completion')
           AND plan.task_id IS NOT NULL
    """)
    )
    op.execute(
        sa.text("""
        UPDATE chat_message_feedback
           SET target_kind = COALESCE(target_kind, 'response'),
               target_id = COALESCE(target_id, message_id),
               mutation_sequence = COALESCE(mutation_sequence, 1)
         WHERE target_kind IS NULL
            OR target_id IS NULL
            OR mutation_sequence IS NULL
    """)
    )
    op.execute(
        sa.text("""
        DELETE FROM chat_message_feedback AS feedback
         WHERE NOT EXISTS (SELECT 1 FROM users WHERE users.id = feedback.user_id)
            OR NOT EXISTS (
                SELECT 1 FROM conversations
                 WHERE conversations.id = feedback.conversation_id
            )
            OR NOT EXISTS (
                SELECT 1 FROM messages WHERE messages.id = feedback.message_id
            )
    """)
    )
    op.execute(
        sa.text("""
        DELETE FROM chat_message_feedback AS feedback
         USING (
             SELECT id
               FROM (
                   SELECT id,
                          row_number() OVER (
                              PARTITION BY target_kind, target_id, user_id
                              ORDER BY updated_at DESC NULLS LAST,
                                       created_at DESC NULLS LAST,
                                       id DESC
                          ) AS duplicate_rank
                     FROM chat_message_feedback
               ) AS ranked
              WHERE duplicate_rank > 1
         ) AS duplicate
         WHERE feedback.id = duplicate.id
    """)
    )
    op.alter_column("chat_message_feedback", "target_kind", existing_type=sa.String(length=32), nullable=False)
    op.alter_column("chat_message_feedback", "target_id", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("chat_message_feedback", "mutation_sequence", existing_type=sa.BigInteger(), nullable=False)
    op.create_index("ix_chat_feedback_user", "chat_message_feedback", ["user_id"], unique=False)
    op.create_index(
        "ux_chat_feedback_target_user", "chat_message_feedback", ["target_kind", "target_id", "user_id"], unique=True
    )
    op.create_foreign_key(
        "fk_chat_feedback_user", "chat_message_feedback", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_chat_feedback_message", "chat_message_feedback", "messages", ["message_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_chat_feedback_conversation",
        "chat_message_feedback",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_comments_resource_normalized",
        "comments",
        [sa.literal_column("lower(trim(resource_type))"), "resource_id"],
        unique=False,
    )
    op.execute(
        sa.text("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY entity_id, fs_path
                       ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST,
                                created_at DESC NULLS LAST,
                                id DESC
                   ) AS duplicate_rank
              FROM documents
             WHERE fs_path IS NOT NULL AND is_trashed = false
        )
        UPDATE documents AS document
           SET is_trashed = true,
               fs_path = NULL,
               trashed_at = COALESCE(document.trashed_at, CURRENT_TIMESTAMP),
               trashed_by = 'system:deduplicate-fs-path',
               metadata = COALESCE(document.metadata, '{}'::jsonb) ||
                   jsonb_build_object(
                       'deduplicated_fs_path', document.fs_path,
                       'restore_blocked_reason', 'duplicate_filesystem_projection'
                   )
          FROM ranked
         WHERE document.id = ranked.id AND ranked.duplicate_rank > 1
    """)
    )
    op.create_index(
        "uq_documents_entity_fs_path_active",
        "documents",
        ["entity_id", "fs_path"],
        unique=True,
        postgresql_where=sa.text("fs_path IS NOT NULL AND is_trashed = false"),
        sqlite_where=sa.text("fs_path IS NOT NULL AND is_trashed = 0"),
    )
    op.add_column("event_logs", sa.Column("workspace_id", sa.String(length=26), nullable=True))
    op.add_column("event_logs", sa.Column("external_delivery_status", sa.String(length=20), nullable=True))
    op.add_column(
        "event_logs", sa.Column("external_delivery_attempt_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("event_logs", sa.Column("external_delivery_available_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("event_logs", sa.Column("external_delivery_locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("event_logs", sa.Column("external_delivery_claim_token", sa.String(length=26), nullable=True))
    op.add_column("event_logs", sa.Column("external_delivery_delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "event_logs",
        sa.Column(
            "external_delivery_completed_sinks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column("event_logs", sa.Column("external_delivery_last_error", sa.Text(), nullable=True))
    op.create_index(
        "ix_event_external_delivery_due",
        "event_logs",
        ["external_delivery_status", "external_delivery_available_at"],
        unique=False,
        postgresql_where=sa.text("external_delivery_status IN ('pending', 'processing')"),
    )
    op.create_index(
        "ix_event_external_delivery_lease",
        "event_logs",
        ["external_delivery_status", "external_delivery_locked_until"],
        unique=False,
        postgresql_where=sa.text("external_delivery_status = 'processing'"),
    )
    op.add_column("goals", sa.Column("goal_key", sa.String(length=100), nullable=True))
    op.execute(
        sa.text("""
        WITH runtime_ranked AS (
            SELECT id, workspace_id, metric_key,
                   row_number() OVER (
                       PARTITION BY workspace_id, metric_key
                       ORDER BY created_at, id
                   ) AS metric_rank
              FROM goals
             WHERE workspace_id IS NOT NULL
        ), contract_ranked AS (
            SELECT workspace.id AS workspace_id,
                   contract.goal->>'goal_key' AS goal_key,
                   COALESCE(
                       NULLIF(BTRIM(contract.goal->>'metric_key'), ''),
                       NULLIF(BTRIM(contract.goal->>'goal_key'), ''),
                       NULLIF(BTRIM(contract.goal->>'key'), '')
                   ) AS metric_key,
                   row_number() OVER (
                       PARTITION BY workspace.id, COALESCE(
                           NULLIF(BTRIM(contract.goal->>'metric_key'), ''),
                           NULLIF(BTRIM(contract.goal->>'goal_key'), ''),
                           NULLIF(BTRIM(contract.goal->>'key'), '')
                       )
                       ORDER BY contract.ordinality
                   ) AS metric_rank
              FROM workspaces AS workspace
              CROSS JOIN LATERAL jsonb_array_elements(
                  CASE
                      WHEN jsonb_typeof(workspace.operating_model->'goals') = 'array'
                      THEN workspace.operating_model->'goals'
                      ELSE '[]'::jsonb
                  END
              ) WITH ORDINALITY AS contract(goal, ordinality)
        )
        UPDATE goals AS goal
           SET goal_key = LEFT(BTRIM(contract.goal_key), 100)
          FROM runtime_ranked AS runtime
          JOIN contract_ranked AS contract
            ON contract.workspace_id = runtime.workspace_id
           AND contract.metric_key = runtime.metric_key
           AND contract.metric_rank = runtime.metric_rank
         WHERE runtime.id = goal.id
           AND NULLIF(BTRIM(contract.goal_key), '') IS NOT NULL
    """)
    )
    op.execute(
        sa.text("""
        UPDATE goals
           SET goal_key = LEFT(
               COALESCE(
                   NULLIF(BTRIM(goal_key), ''),
                   NULLIF(BTRIM(metric_key), ''),
                   'goal'
               ),
               100
           )
    """)
    )
    op.execute(
        sa.text("""
        DO $$
        DECLARE
            goal_row RECORD;
            base_key text;
            candidate_key text;
            suffix_number integer;
            suffix_text text;
        BEGIN
            CREATE TEMP TABLE oss_goal_key_repair
            ON COMMIT DROP AS
            SELECT id, entity_id, workspace_id, goal_key AS assigned_key
              FROM goals WITH NO DATA;

            CREATE UNIQUE INDEX oss_goal_key_repair_workspace
                ON oss_goal_key_repair (workspace_id, assigned_key)
             WHERE workspace_id IS NOT NULL;
            CREATE UNIQUE INDEX oss_goal_key_repair_entity
                ON oss_goal_key_repair (entity_id, assigned_key)
             WHERE workspace_id IS NULL;

            FOR goal_row IN
                SELECT id, entity_id, workspace_id, goal_key
                  FROM goals
                 ORDER BY created_at, id
            LOOP
                base_key := LEFT(BTRIM(goal_row.goal_key), 100);
                candidate_key := base_key;
                suffix_number := 2;
                WHILE EXISTS (
                    SELECT 1
                      FROM oss_goal_key_repair AS assigned
                     WHERE (
                         (goal_row.workspace_id IS NOT NULL
                          AND assigned.workspace_id = goal_row.workspace_id)
                         OR
                         (goal_row.workspace_id IS NULL
                          AND assigned.workspace_id IS NULL
                          AND assigned.entity_id = goal_row.entity_id)
                     )
                       AND assigned.assigned_key = candidate_key
                ) LOOP
                    suffix_text := '_' || suffix_number::text;
                    candidate_key := LEFT(
                        base_key,
                        100 - length(suffix_text)
                    ) || suffix_text;
                    suffix_number := suffix_number + 1;
                END LOOP;
                INSERT INTO oss_goal_key_repair (
                    id, entity_id, workspace_id, assigned_key
                ) VALUES (
                    goal_row.id, goal_row.entity_id,
                    goal_row.workspace_id, candidate_key
                );
            END LOOP;

            UPDATE goals AS goal
               SET goal_key = repair.assigned_key
              FROM oss_goal_key_repair AS repair
             WHERE goal.id = repair.id;
        END $$
    """)
    )
    op.alter_column("goals", "goal_key", existing_type=sa.String(length=100), nullable=False)
    op.create_index(
        "uq_goals_entity_goal_key",
        "goals",
        ["entity_id", "goal_key"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NULL"),
        sqlite_where=sa.text("workspace_id IS NULL"),
    )
    op.create_index(
        "uq_goals_workspace_goal_key",
        "goals",
        ["workspace_id", "goal_key"],
        unique=True,
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
        sqlite_where=sa.text("workspace_id IS NOT NULL"),
    )
    op.execute(
        sa.text("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY channel_type, channel_config_id, external_id
                       ORDER BY created_at ASC, id ASC
                   ) AS duplicate_rank
              FROM message_logs
             WHERE channel_type IN (
                       'discord', 'ms_teams', 'outlook', 'slack',
                       'twilio_sms', 'wechat', 'wechat_personal', 'whatsapp'
                   )
               AND direction = 'inbound'
               AND external_id IS NOT NULL
        )
        DELETE FROM message_logs
         WHERE id IN (
             SELECT id FROM ranked WHERE duplicate_rank > 1
         )
    """)
    )
    op.create_index(
        "uq_message_logs_discord_inbound_interaction",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("channel_type = 'discord' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_message_logs_ms_teams_inbound_event",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("channel_type = 'ms_teams' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_message_logs_outlook_inbound_message",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("channel_type = 'outlook' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_message_logs_slack_inbound_event",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("channel_type = 'slack' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_message_logs_twilio_inbound_sid",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("channel_type = 'twilio_sms' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_message_logs_wechat_inbound_message",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("channel_type = 'wechat' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_message_logs_wechat_personal_inbound_message",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "channel_type = 'wechat_personal' AND direction = 'inbound' AND external_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_message_logs_whatsapp_inbound_event",
        "message_logs",
        ["channel_config_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("channel_type = 'whatsapp' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.add_column("messages", sa.Column("response_surface_event_id", sa.String(length=96), nullable=True))
    op.create_index(
        "uq_messages_conversation_response_surface_event",
        "messages",
        ["conversation_id", "response_surface_event_id"],
        unique=True,
        postgresql_where=sa.text("response_surface_event_id IS NOT NULL"),
    )
    op.add_column("notifications", sa.Column("workspace_id", sa.String(length=26), nullable=True))
    op.add_column("notifications", sa.Column("idempotency_key", sa.String(length=255), nullable=True))
    op.execute(
        sa.text("""
        UPDATE notifications
           SET workspace_id = NULLIF(metadata->>'workspace_id', '')
         WHERE workspace_id IS NULL
           AND metadata ? 'workspace_id'
    """)
    )
    op.execute(
        sa.text("""
        INSERT INTO notification_outbox_events (
            id, notification_id, payload, status, attempt_count,
            available_at, created_at, updated_at
        )
        SELECT notification.id,
               notification.id,
               COALESCE(notification.metadata->'_scheduled', '{}'::jsonb),
               'pending',
               0,
               COALESCE(notification.deliver_at, CURRENT_TIMESTAMP),
               notification.created_at,
               CURRENT_TIMESTAMP
          FROM notifications AS notification
         WHERE notification.dispatch_status IN ('pending', 'dispatching')
        ON CONFLICT (notification_id) DO NOTHING
    """)
    )
    op.execute(
        sa.text("""
        UPDATE notifications
           SET dispatch_status = 'pending'
         WHERE dispatch_status = 'dispatching'
    """)
    )
    op.create_index("ix_notifications_workspace", "notifications", ["workspace_id", "created_at"], unique=False)
    op.create_index(
        "uq_notifications_recipient_idempotency",
        "notifications",
        ["entity_id", "user_id", "idempotency_key"],
        unique=True,
    )
    op.add_column("review_runs", sa.Column("delivery_id", sa.String(length=120), nullable=True))
    op.add_column("review_runs", sa.Column("lease_owner", sa.String(length=120), nullable=True))
    op.add_column("review_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_review_runs_workspace_delivery_live_or_terminal",
        "review_runs",
        ["workspace_id", "delivery_id"],
        unique=True,
        postgresql_where=sa.text("delivery_id IS NOT NULL AND status IN ('running', 'succeeded', 'skipped')"),
    )
    op.add_column("scheduled_jobs", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scheduled_jobs", sa.Column("skill_generation_revision", sa.Integer(), nullable=True))
    op.add_column(
        "scheduled_jobs", sa.Column("skill_generation_next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "scheduled_jobs", sa.Column("skill_generation_attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("scheduled_jobs", sa.Column("skill_generation_last_error", sa.Text(), nullable=True))
    _backfill_scheduled_job_clocks()
    op.create_index("ix_scheduled_jobs_due", "scheduled_jobs", ["enabled", "next_run_at", "id"], unique=False)
    op.create_index(
        "ix_scheduled_jobs_skill_generation_due",
        "scheduled_jobs",
        ["enabled", "skill_generation_next_attempt_at", "id"],
        unique=False,
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_sjr_prepared_recovery
            ON scheduled_job_runs (created_at, id)
         WHERE result->>'dispatch_status' = 'prepared'
    """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_sjr_published_recovery
            ON scheduled_job_runs (created_at, id)
         WHERE status = 'running'
           AND result->>'dispatch_status' = 'published'
    """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_sjr_settlement_recovery
            ON scheduled_job_runs (created_at, id)
         WHERE result->>'scheduled_execution_state' = 'settlement_pending'
    """)
    )
    op.execute(
        sa.text("""
        CREATE INDEX ix_sjr_projection_recovery
            ON scheduled_job_runs (created_at, id)
         WHERE result ? 'scheduled_result_projection'
    """)
    )
    op.add_column("sites", sa.Column("created_by_user_id", sa.String(length=26), nullable=True))
    op.create_index("ix_sites_created_by_user", "sites", ["created_by_user_id"], unique=False)
    op.execute(
        sa.text("""
        CREATE TEMP TABLE oss_builtin_skill_id_map ON COMMIT DROP AS
        WITH ranked AS (
            SELECT skill.id AS skill_id,
                   first_value(skill.id) OVER (
                       PARTITION BY skill.slug
                       ORDER BY EXISTS (
                           SELECT 1
                             FROM agent_skill_bindings AS binding
                            WHERE binding.skill_id = skill.id
                       ) DESC,
                       skill.created_at ASC NULLS LAST,
                       skill.id ASC
                   ) AS canonical_id,
                   count(*) OVER (PARTITION BY skill.slug) AS row_count
              FROM skills AS skill
             WHERE skill.entity_id IS NULL AND skill.slug IS NOT NULL
        )
        SELECT skill_id, canonical_id
          FROM ranked
         WHERE row_count > 1 AND skill_id <> canonical_id
    """)
    )
    op.execute(
        sa.text("""
        WITH ranked AS (
            SELECT binding.id,
                   row_number() OVER (
                       PARTITION BY binding.agent_id,
                                    COALESCE(id_map.canonical_id, binding.skill_id)
                       ORDER BY (binding.status = 'active') DESC,
                                (id_map.skill_id IS NULL) DESC,
                                binding.created_at ASC NULLS LAST,
                                binding.id ASC
                   ) AS duplicate_rank
              FROM agent_skill_bindings AS binding
              LEFT JOIN oss_builtin_skill_id_map AS id_map
                ON id_map.skill_id = binding.skill_id
             WHERE id_map.skill_id IS NOT NULL
                OR binding.skill_id IN (
                    SELECT DISTINCT canonical_id FROM oss_builtin_skill_id_map
                )
        )
        DELETE FROM agent_skill_bindings
         WHERE id IN (
             SELECT id FROM ranked WHERE duplicate_rank > 1
         )
    """)
    )
    op.execute(
        sa.text("""
        UPDATE agent_skill_bindings AS binding
           SET skill_id = id_map.canonical_id
          FROM oss_builtin_skill_id_map AS id_map
         WHERE binding.skill_id = id_map.skill_id
    """)
    )
    op.execute(
        sa.text("""
        UPDATE skills AS duplicate
           SET slug = LEFT(duplicate.slug, 60)
                      || '--duplicate-' || RIGHT(duplicate.id, 26),
               status = 'inactive',
               is_public = false
          FROM oss_builtin_skill_id_map AS id_map
         WHERE duplicate.id = id_map.skill_id
    """)
    )
    op.create_index(
        "uq_skills_builtin_slug",
        "skills",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("entity_id IS NULL AND slug IS NOT NULL"),
    )
    op.alter_column(
        "staff", "avatar_url", existing_type=sa.VARCHAR(length=500), type_=sa.Text(), existing_nullable=True
    )
    op.add_column("tasks", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        sa.text("""
        UPDATE tasks
           SET status_changed_at = COALESCE(updated_at, created_at)
         WHERE status_changed_at IS NULL
    """)
    )
    op.alter_column(
        "users", "avatar_url", existing_type=sa.VARCHAR(length=500), type_=sa.Text(), existing_nullable=True
    )
    op.add_column("workflow_action_grants", sa.Column("proposal_item_id", sa.String(length=26), nullable=True))
    op.add_column(
        "workflow_action_grants", sa.Column("workflow_lineage_root_run_id", sa.String(length=26), nullable=True)
    )
    op.add_column("workflow_action_grants", sa.Column("action_key", sa.String(length=120), nullable=True))
    op.add_column("workflow_action_grants", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_action_grants", sa.Column("consumed_by_run_id", sa.String(length=26), nullable=True))
    op.alter_column("workflow_action_grants", "project_id", existing_type=sa.VARCHAR(length=26), nullable=True)
    op.create_index(
        "ix_workflow_action_grants_lineage",
        "workflow_action_grants",
        ["workflow_lineage_root_run_id", "grant_type"],
        unique=False,
    )
    op.create_index(
        "uq_workflow_action_grants_proposal_item",
        "workflow_action_grants",
        ["proposal_item_id"],
        unique=True,
        postgresql_where=sa.text("proposal_item_id IS NOT NULL"),
        sqlite_where=sa.text("proposal_item_id IS NOT NULL"),
    )
    op.add_column("workflow_runs", sa.Column("webchat_session_id", sa.String(length=128), nullable=True))
    op.add_column("workflow_runs", sa.Column("webchat_module_id", sa.String(length=80), nullable=True))
    op.add_column("workflow_runs", sa.Column("webchat_submission_id", sa.String(length=80), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column("execution_snapshot", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
    )
    op.add_column("workflow_runs", sa.Column("continuation_token", sa.String(length=26), nullable=True))
    op.add_column("workflow_runs", sa.Column("continuation_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_runs", sa.Column("continuation_next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "workflow_runs", sa.Column("terminal_effects_completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workflow_runs", sa.Column("terminal_effects_next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        sa.text("""
        UPDATE workflow_runs AS run
           SET execution_snapshot = jsonb_build_object(
               'workflow_id', definition.id,
               'entity_id', definition.entity_id,
               'name', COALESCE(definition.name, definition.id),
               'description', COALESCE(definition.description, ''),
               'version', COALESCE(definition.version, 1),
               'steps', COALESCE(definition.steps, '[]'::jsonb),
               'variables', COALESCE(definition.variables, '{}'::jsonb)
           )
          FROM workflow_definitions AS definition
         WHERE run.workflow_id = definition.id
           AND run.execution_snapshot = '{}'::jsonb
    """)
    )
    op.execute(
        sa.text("""
        UPDATE workflow_runs
           SET webchat_session_id = trigger_data->>'webchat_session_id',
               webchat_module_id = trigger_data->>'webchat_module_id',
               webchat_submission_id = trigger_data->>'webchat_submission_id'
         WHERE trigger_source = 'public_webchat'
           AND webchat_session_id IS NULL
           AND webchat_module_id IS NULL
           AND webchat_submission_id IS NULL
    """)
    )
    op.execute(
        sa.text("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY binding_id, webchat_session_id,
                                    webchat_module_id, webchat_submission_id
                       ORDER BY created_at DESC, id DESC
                   ) AS duplicate_rank
              FROM workflow_runs
             WHERE trigger_source = 'public_webchat'
               AND webchat_session_id IS NOT NULL
               AND webchat_module_id IS NOT NULL
               AND webchat_submission_id IS NOT NULL
        )
        UPDATE workflow_runs AS run
           SET webchat_session_id = NULL,
               webchat_module_id = NULL,
               webchat_submission_id = NULL
          FROM ranked
         WHERE run.id = ranked.id AND ranked.duplicate_rank > 1
    """)
    )
    op.execute(
        sa.text("""
        UPDATE workflow_runs
           SET terminal_effects_completed_at = COALESCE(
                   updated_at, completed_at, created_at, CURRENT_TIMESTAMP
               ),
               terminal_effects_next_attempt_at = NULL
         WHERE status IN ('completed', 'failed', 'cancelled')
           AND terminal_effects_completed_at IS NULL
           AND NOT (
               (
                   status = 'failed'
                   AND COALESCE(
                       trigger_data->'_workflow_terminal_effects'
                                   ->>'error_handlers_enqueued',
                       ''
                   ) = 'false'
               )
               OR (
                   trigger_data ? 'parent_run_id'
                   AND COALESCE(trigger_data->>'parent_step_id', '') <> ''
               )
           )
    """)
    )
    op.create_index(
        "ix_workflow_runs_continuation_due", "workflow_runs", ["continuation_next_attempt_at"], unique=False
    )
    op.create_index(
        "ix_workflow_runs_terminal_effects_due",
        "workflow_runs",
        ["terminal_effects_completed_at", "terminal_effects_next_attempt_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_workflow_runs_webchat_submission",
        "workflow_runs",
        ["binding_id", "webchat_session_id", "webchat_module_id", "webchat_submission_id"],
        unique=True,
        postgresql_where=sa.text(
            "trigger_source = 'public_webchat' AND webchat_session_id IS NOT NULL AND webchat_module_id IS NOT NULL AND webchat_submission_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "trigger_source = 'public_webchat' AND webchat_session_id IS NOT NULL AND webchat_module_id IS NOT NULL AND webchat_submission_id IS NOT NULL"
        ),
    )
    op.execute(
        sa.text("""
        UPDATE workspace_staff
           SET role = 'viewer'
         WHERE user_id IS NOT NULL
           AND (role IS NULL OR role NOT IN ('owner','editor','contributor','viewer'))
    """)
    )
    op.execute(
        sa.text("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY workspace_id, user_id
                       ORDER BY CASE
                           WHEN status = 'active'
                            AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                           THEN 0
                           WHEN status = 'active' THEN 1
                           ELSE 2
                       END,
                       updated_at DESC NULLS LAST,
                       created_at DESC NULLS LAST,
                       id DESC
                   ) AS duplicate_rank
              FROM workspace_staff
             WHERE user_id IS NOT NULL
        )
        DELETE FROM workspace_staff
         WHERE id IN (
             SELECT id FROM ranked WHERE duplicate_rank > 1
         )
    """)
    )
    op.drop_index(op.f("ix_workspace_staff_workspace_user"), table_name="workspace_staff")
    op.create_index("uq_workspace_staff_workspace_user", "workspace_staff", ["workspace_id", "user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_workspace_staff_workspace_user", table_name="workspace_staff")
    op.create_index(
        op.f("ix_workspace_staff_workspace_user"), "workspace_staff", ["workspace_id", "user_id"], unique=False
    )
    op.drop_index(
        "uq_workflow_runs_webchat_submission",
        table_name="workflow_runs",
        postgresql_where=sa.text(
            "trigger_source = 'public_webchat' AND webchat_session_id IS NOT NULL AND webchat_module_id IS NOT NULL AND webchat_submission_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "trigger_source = 'public_webchat' AND webchat_session_id IS NOT NULL AND webchat_module_id IS NOT NULL AND webchat_submission_id IS NOT NULL"
        ),
    )
    op.drop_index("ix_workflow_runs_terminal_effects_due", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_continuation_due", table_name="workflow_runs")
    op.drop_column("workflow_runs", "terminal_effects_next_attempt_at")
    op.drop_column("workflow_runs", "terminal_effects_completed_at")
    op.drop_column("workflow_runs", "continuation_next_attempt_at")
    op.drop_column("workflow_runs", "continuation_due_at")
    op.drop_column("workflow_runs", "continuation_token")
    op.drop_column("workflow_runs", "execution_snapshot")
    op.drop_column("workflow_runs", "webchat_submission_id")
    op.drop_column("workflow_runs", "webchat_module_id")
    op.drop_column("workflow_runs", "webchat_session_id")
    op.drop_index(
        "uq_workflow_action_grants_proposal_item",
        table_name="workflow_action_grants",
        postgresql_where=sa.text("proposal_item_id IS NOT NULL"),
        sqlite_where=sa.text("proposal_item_id IS NOT NULL"),
    )
    op.drop_index("ix_workflow_action_grants_lineage", table_name="workflow_action_grants")
    op.alter_column("workflow_action_grants", "project_id", existing_type=sa.VARCHAR(length=26), nullable=False)
    op.drop_column("workflow_action_grants", "consumed_by_run_id")
    op.drop_column("workflow_action_grants", "consumed_at")
    op.drop_column("workflow_action_grants", "action_key")
    op.drop_column("workflow_action_grants", "workflow_lineage_root_run_id")
    op.drop_column("workflow_action_grants", "proposal_item_id")
    op.alter_column(
        "users", "avatar_url", existing_type=sa.Text(), type_=sa.VARCHAR(length=500), existing_nullable=True
    )
    op.drop_column("tasks", "status_changed_at")
    op.alter_column(
        "staff", "avatar_url", existing_type=sa.Text(), type_=sa.VARCHAR(length=500), existing_nullable=True
    )
    op.drop_index(
        "uq_skills_builtin_slug",
        table_name="skills",
        postgresql_where=sa.text("entity_id IS NULL AND slug IS NOT NULL"),
    )
    op.drop_index("ix_sites_created_by_user", table_name="sites")
    op.drop_column("sites", "created_by_user_id")
    op.drop_index("ix_sjr_projection_recovery", table_name="scheduled_job_runs")
    op.drop_index("ix_sjr_settlement_recovery", table_name="scheduled_job_runs")
    op.drop_index("ix_sjr_published_recovery", table_name="scheduled_job_runs")
    op.drop_index("ix_sjr_prepared_recovery", table_name="scheduled_job_runs")
    op.drop_index("ix_scheduled_jobs_skill_generation_due", table_name="scheduled_jobs")
    op.drop_index("ix_scheduled_jobs_due", table_name="scheduled_jobs")
    op.drop_column("scheduled_jobs", "skill_generation_last_error")
    op.drop_column("scheduled_jobs", "skill_generation_attempts")
    op.drop_column("scheduled_jobs", "skill_generation_next_attempt_at")
    op.drop_column("scheduled_jobs", "skill_generation_revision")
    op.drop_column("scheduled_jobs", "next_run_at")
    op.drop_index(
        "uq_review_runs_workspace_delivery_live_or_terminal",
        table_name="review_runs",
        postgresql_where=sa.text("delivery_id IS NOT NULL AND status IN ('running', 'succeeded', 'skipped')"),
    )
    op.drop_column("review_runs", "lease_expires_at")
    op.drop_column("review_runs", "lease_owner")
    op.drop_column("review_runs", "delivery_id")
    op.drop_index("uq_notifications_recipient_idempotency", table_name="notifications")
    op.drop_index("ix_notifications_workspace", table_name="notifications")
    op.drop_column("notifications", "idempotency_key")
    op.drop_column("notifications", "workspace_id")
    op.drop_index(
        "uq_messages_conversation_response_surface_event",
        table_name="messages",
        postgresql_where=sa.text("response_surface_event_id IS NOT NULL"),
    )
    op.drop_column("messages", "response_surface_event_id")
    op.drop_index(
        "uq_message_logs_whatsapp_inbound_event",
        table_name="message_logs",
        postgresql_where=sa.text("channel_type = 'whatsapp' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_message_logs_wechat_personal_inbound_message",
        table_name="message_logs",
        postgresql_where=sa.text(
            "channel_type = 'wechat_personal' AND direction = 'inbound' AND external_id IS NOT NULL"
        ),
    )
    op.drop_index(
        "uq_message_logs_wechat_inbound_message",
        table_name="message_logs",
        postgresql_where=sa.text("channel_type = 'wechat' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_message_logs_twilio_inbound_sid",
        table_name="message_logs",
        postgresql_where=sa.text("channel_type = 'twilio_sms' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_message_logs_slack_inbound_event",
        table_name="message_logs",
        postgresql_where=sa.text("channel_type = 'slack' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_message_logs_outlook_inbound_message",
        table_name="message_logs",
        postgresql_where=sa.text("channel_type = 'outlook' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_message_logs_ms_teams_inbound_event",
        table_name="message_logs",
        postgresql_where=sa.text("channel_type = 'ms_teams' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_message_logs_discord_inbound_interaction",
        table_name="message_logs",
        postgresql_where=sa.text("channel_type = 'discord' AND direction = 'inbound' AND external_id IS NOT NULL"),
    )
    op.drop_index("ix_integrations_entity_owner_provider", table_name="integrations")
    op.drop_column("integrations", "owner_user_id")
    op.drop_index(
        "uq_goals_workspace_goal_key",
        table_name="goals",
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
        sqlite_where=sa.text("workspace_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_goals_entity_goal_key",
        table_name="goals",
        postgresql_where=sa.text("workspace_id IS NULL"),
        sqlite_where=sa.text("workspace_id IS NULL"),
    )
    op.drop_column("goals", "goal_key")
    op.drop_index(
        "ix_event_external_delivery_lease",
        table_name="event_logs",
        postgresql_where=sa.text("external_delivery_status = 'processing'"),
    )
    op.drop_index(
        "ix_event_external_delivery_due",
        table_name="event_logs",
        postgresql_where=sa.text("external_delivery_status IN ('pending', 'processing')"),
    )
    op.drop_column("event_logs", "external_delivery_last_error")
    op.drop_column("event_logs", "external_delivery_completed_sinks")
    op.drop_column("event_logs", "external_delivery_delivered_at")
    op.drop_column("event_logs", "external_delivery_claim_token")
    op.drop_column("event_logs", "external_delivery_locked_until")
    op.drop_column("event_logs", "external_delivery_available_at")
    op.drop_column("event_logs", "external_delivery_attempt_count")
    op.drop_column("event_logs", "external_delivery_status")
    op.drop_column("event_logs", "workspace_id")
    op.drop_index(
        "uq_documents_entity_fs_path_active",
        table_name="documents",
        postgresql_where=sa.text("fs_path IS NOT NULL AND is_trashed = false"),
        sqlite_where=sa.text("fs_path IS NOT NULL AND is_trashed = 0"),
    )
    op.drop_index("ix_comments_resource_normalized", table_name="comments")
    op.drop_constraint("fk_chat_feedback_conversation", "chat_message_feedback", type_="foreignkey")
    op.drop_constraint("fk_chat_feedback_message", "chat_message_feedback", type_="foreignkey")
    op.drop_constraint("fk_chat_feedback_user", "chat_message_feedback", type_="foreignkey")
    op.drop_index("ux_chat_feedback_target_user", table_name="chat_message_feedback")
    op.drop_index("ix_chat_feedback_user", table_name="chat_message_feedback")
    op.drop_column("chat_message_feedback", "mutation_sequence")
    op.drop_column("chat_message_feedback", "plan_id")
    op.drop_column("chat_message_feedback", "task_id")
    op.drop_column("chat_message_feedback", "target_id")
    op.drop_column("chat_message_feedback", "target_kind")
    op.drop_index(
        "ux_channels_active_whatsapp_config",
        table_name="channels",
        postgresql_where=sa.text(
            "type = 'whatsapp' AND status = 'active' AND config ->> 'channel_config_id' IS NOT NULL"
        ),
        sqlite_where=sa.text("type = 'whatsapp' AND status = 'active' AND config ->> 'channel_config_id' IS NOT NULL"),
    )
    op.drop_index("ux_channel_configs_whatsapp_phone_number_id", table_name="channel_configs")
    op.drop_index("ux_channel_configs_telegram_bot_id", table_name="channel_configs")
    op.drop_index("ux_channel_configs_discord_installation", table_name="channel_configs")
    op.drop_index("ix_channel_configs_entity_owner_source", table_name="channel_configs")
    op.drop_column("channel_configs", "whatsapp_phone_number_id")
    op.drop_column("channel_configs", "discord_guild_id")
    op.drop_column("channel_configs", "discord_application_id")
    op.drop_column("channel_configs", "telegram_bot_id")
    op.drop_column("channel_configs", "credential_source_id")
    op.drop_column("channel_configs", "credential_source_kind")
    op.drop_column("channel_configs", "owner_user_id")
    op.alter_column(
        "audit_log",
        "resource_id",
        existing_type=sa.String(length=255),
        type_=sa.VARCHAR(length=26),
        existing_nullable=True,
    )
    op.drop_index("ix_user_session_lease_session", table_name="user_session_leases")
    op.drop_index("ix_user_session_lease_scope_expiry", table_name="user_session_leases")
    op.drop_index("ix_user_session_lease_expires", table_name="user_session_leases")
    op.drop_table("user_session_leases")
    op.drop_index("uq_notification_outbox_notification", table_name="notification_outbox_events")
    op.drop_index("ix_notification_outbox_lease", table_name="notification_outbox_events")
    op.drop_index("ix_notification_outbox_due", table_name="notification_outbox_events")
    op.drop_table("notification_outbox_events")
    op.drop_index("ix_mcp_account_tool_catalogs_provider", table_name="mcp_account_tool_catalogs")
    op.drop_table("mcp_account_tool_catalogs")
    op.drop_index("uq_workspace_artifact_purge_scope", table_name="workspace_artifact_purge_jobs")
    op.drop_index("ix_workspace_artifact_purge_due", table_name="workspace_artifact_purge_jobs")
    op.drop_table("workspace_artifact_purge_jobs")
    op.drop_table("wechat_personal_sessions")
    op.drop_index(
        "uq_twilio_voice_sessions_call_sid",
        table_name="twilio_voice_call_sessions",
        postgresql_where=sa.text("call_sid IS NOT NULL"),
    )
    op.drop_index("ix_twilio_voice_sessions_expires", table_name="twilio_voice_call_sessions")
    op.drop_index("ix_twilio_voice_sessions_config_status", table_name="twilio_voice_call_sessions")
    op.drop_index(op.f("ix_twilio_voice_call_sessions_workspace_id"), table_name="twilio_voice_call_sessions")
    op.drop_index(op.f("ix_twilio_voice_call_sessions_owner_user_id"), table_name="twilio_voice_call_sessions")
    op.drop_index(op.f("ix_twilio_voice_call_sessions_entity_id"), table_name="twilio_voice_call_sessions")
    op.drop_index(op.f("ix_twilio_voice_call_sessions_channel_config_id"), table_name="twilio_voice_call_sessions")
    op.drop_table("twilio_voice_call_sessions")
    op.drop_index("uq_sandbox_runners_base_url", table_name="sandbox_runners")
    op.drop_index("ix_sandbox_runners_status", table_name="sandbox_runners")
    op.drop_table("sandbox_runners")
    op.drop_index(
        "uq_sandbox_reservations_sandbox",
        table_name="sandbox_reservations",
        postgresql_where=sa.text("sandbox_id IS NOT NULL"),
    )
    op.drop_index("ix_sandbox_reservations_root_status", table_name="sandbox_reservations")
    op.drop_index(
        "ix_sandbox_reservations_queue",
        table_name="sandbox_reservations",
        postgresql_where=sa.text("status IN ('queued','requeued')"),
    )
    op.drop_table("sandbox_reservations")
    op.drop_index("ix_sandbox_instances_runner_status", table_name="sandbox_instances")
    op.drop_index("ix_sandbox_instances_root_status", table_name="sandbox_instances")
    op.drop_table("sandbox_instances")
    op.drop_index(
        "uq_runtime_runs_active_conversation",
        table_name="runtime_runs",
        postgresql_where=sa.text(
            "parent_run_id IS NULL AND status IN ('queued','running','waiting_resource','resuming','cancel_requested','cancelling')"
        ),
    )
    op.drop_index("ix_runtime_runs_root_status", table_name="runtime_runs")
    op.drop_index("ix_runtime_runs_entity_status", table_name="runtime_runs")
    op.drop_index("ix_runtime_runs_conversation_created", table_name="runtime_runs")
    op.drop_table("runtime_runs")
    op.drop_index(
        "ix_runtime_outbox_pending",
        table_name="runtime_outbox_events",
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.drop_table("runtime_outbox_events")
    op.drop_index("ix_runtime_execution_claims_expires", table_name="runtime_execution_claims")
    op.drop_table("runtime_execution_claims")

    op.create_table(
        "merchant_accounts",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("stripe_account_id", sa.String(length=255), nullable=False),
        sa.Column("onboarding_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("charges_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("payouts_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ux_merchant_accounts_entity", "merchant_accounts", ["entity_id"], unique=True)
    op.create_index(
        "ux_merchant_accounts_stripe",
        "merchant_accounts",
        ["stripe_account_id"],
        unique=True,
    )
    op.create_table(
        "blueprint_purchases",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("blueprint_id", sa.String(length=26), nullable=False),
        sa.Column("buyer_entity_id", sa.String(length=26), nullable=False),
        sa.Column("buyer_user_id", sa.String(length=26), nullable=False),
        sa.Column("order_id", sa.String(length=26), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=10), server_default="usd", nullable=False),
        sa.Column("platform_fee_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("seller_amount_cents", sa.Integer(), nullable=False),
        sa.Column("stripe_checkout_session_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=255), nullable=True),
        sa.Column("payload_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blueprint_title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_blueprint_purchases_blueprint",
        "blueprint_purchases",
        ["blueprint_id"],
        unique=False,
    )
    op.create_index(
        "ix_blueprint_purchases_buyer",
        "blueprint_purchases",
        ["buyer_entity_id"],
        unique=False,
    )
    op.create_index(
        "ux_blueprint_purchases_checkout_session",
        "blueprint_purchases",
        ["stripe_checkout_session_id"],
        unique=True,
        postgresql_where=sa.text("stripe_checkout_session_id IS NOT NULL"),
    )
    op.create_index(
        "ux_blueprint_purchases_live_entitlement",
        "blueprint_purchases",
        ["blueprint_id", "buyer_entity_id"],
        unique=True,
        postgresql_where=sa.text("status != 'refunded'"),
    )
