"""allow one Workspace Agent deployment to serve multiple services

Revision ID: 20260911_03
Revises: deployment-specific predecessors
Create Date: 2026-09-11
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from packages.core.migrations.helpers import column_exists, index_exists, table_exists

revision: str = "20260911_03"
down_revision: str | Sequence[str] | None = "20260910_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "agent_subscriptions"
_OLD_COLUMNS = ("entity_id", "agent_id", "workspace_id")
_NEW_COLUMNS = (*_OLD_COLUMNS, "service_key")
_NEW_CONSTRAINT = "uq_agent_subscriptions_workspace_service_agent"
_DEFAULT_INDEX = "uq_agent_subscriptions_workspace_default_agent"
_DEDUPE_MAP = "_agent_subscription_dedupe_map"
_SIGNED_PINS = "_agent_subscription_signed_pins"
_SUBSCRIPTION_REFERENCE_COLUMNS = (
    ("agent_initiatives", "agent_subscription_id"),
    ("channel_contacts", "agent_subscription_id"),
    ("channels", "agent_subscription_id"),
    ("conversations", "agent_subscription_id"),
    ("execution_plans", "agent_subscription_id"),
    ("execution_steps", "resolved_subscription_id"),
    ("messages", "author_subscription_id"),
    ("tasks", "owner_subscription_id"),
    ("work_leases", "subscription_id"),
)
_JSON_SCALAR_REFERENCES = (
    ("messages", "pending_action", "{agent_subscription_id}", None),
    (
        "messages",
        "metadata",
        "{workflow_current_subscription_id}",
        None,
    ),
    (
        "message_logs",
        "attachments",
        "{_manor_inbound_dispatch,agent_subscription_id}",
        None,
    ),
    (
        "twilio_voice_call_sessions",
        "metadata_json",
        "{agent_subscription_id}",
        None,
    ),
    ("notifications", "metadata", "{agent_subscription_id}", None),
    (
        "tasks",
        "details",
        "{agent_initiative,agent_subscription_id}",
        None,
    ),
    (
        "scheduled_jobs",
        "execution_target",
        "{subscription_id}",
        'target.execution_type = \'agent_initiative\'',
    ),
    (
        "runtime_runs",
        "execution_payload",
        "{runtime_metadata,agent_subscription_id}",
        None,
    ),
)
_JSON_ARRAY_REFERENCES = (
    ("messages", "metadata", "{workflow_steps}", "subscription_id", None),
    (
        "workspace_operation_drafts",
        "current_state",
        "{agent_mappings}",
        "agent_subscription_id",
        "target.status = 'open'",
    ),
    (
        "workspace_operation_drafts",
        "current_state",
        "{operating_model,agent_mappings}",
        "agent_subscription_id",
        "target.status = 'open'",
    ),
    (
        "workspaces",
        "operating_model",
        "{agent_mappings}",
        "agent_subscription_id",
        None,
    ),
    (
        "workspaces",
        "operating_model",
        "{capability_bindings}",
        "agent_subscription_id",
        None,
    ),
)


def _unique_constraint_name(columns: tuple[str, ...]) -> str | None:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints(_TABLE):
        if tuple(constraint.get("column_names") or ()) == columns:
            name = constraint.get("name")
            if name:
                return str(name)
    return None


def _has_multi_service_workspace_agents() -> bool:
    result = op.get_bind().execute(sa.text(
        "SELECT 1 FROM agent_subscriptions "
        "WHERE workspace_id IS NOT NULL "
        "GROUP BY entity_id, agent_id, workspace_id "
        "HAVING COUNT(*) > 1 LIMIT 1"
    ))
    return result.fetchone() is not None


def _create_signed_checkpoint_pins() -> None:
    """Protect live HMAC-authenticated Task continuations from ID rewrites."""

    if not table_exists("hitl_requests") or not column_exists(
        "hitl_requests", "context"
    ):
        op.execute(sa.text(f'''
            CREATE TEMP TABLE "{_SIGNED_PINS}" (
                subscription_id VARCHAR(26) PRIMARY KEY
            ) ON COMMIT DROP
        '''))
        return

    task_join = ""
    resume_clause = "FALSE"
    if table_exists("tasks") and column_exists("tasks", "details"):
        task_join = "LEFT JOIN tasks AS task ON task.id = request.origin_task_id"
        resume_clause = """
            (
                jsonb_typeof(
                    request.context -> 'task_runtime_resolution_v1'
                ) = 'object'
                AND task.details #>> '{_runtime_tool_hitl_v1,state}'
                    IN ('resume_queued', 'resuming')
            )
        """

    live_clause = f"(request.status = 'pending' OR {resume_clause})"
    op.execute(sa.text(f'''
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM hitl_requests AS request
                {task_join}
                WHERE {live_clause}
                  AND (
                    request.context ? 'task_runtime_checkpoint_v1'
                    OR request.context ? 'task_runtime_checkpoint_signature_v1'
                    OR request.context ? 'task_runtime_resolution_v1'
                  )
                  AND (
                    jsonb_typeof(
                        request.context -> 'task_runtime_checkpoint_v1'
                    ) IS DISTINCT FROM 'object'
                    OR COALESCE(
                        request.context #>> '{{task_runtime_checkpoint_v1,agent_subscription_id}}',
                        ''
                    ) = ''
                    OR COALESCE(
                        request.context ->> 'task_runtime_checkpoint_signature_v1',
                        ''
                    ) !~ '^[0-9a-f]{{64}}$'
                  )
            ) THEN
                RAISE EXCEPTION
                    'Cannot merge Agent subscriptions with a malformed live signed checkpoint';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM hitl_requests AS request
                {task_join}
                LEFT JOIN agent_subscriptions AS subscription
                  ON subscription.id = request.context #>>
                    '{{task_runtime_checkpoint_v1,agent_subscription_id}}'
                WHERE {live_clause}
                  AND jsonb_typeof(
                        request.context -> 'task_runtime_checkpoint_v1'
                      ) = 'object'
                  AND subscription.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot merge Agent subscriptions with a dangling live signed checkpoint';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM hitl_requests AS request
                {task_join}
                WHERE {live_clause}
                  AND jsonb_typeof(
                        request.context -> 'task_runtime_resolution_v1'
                      ) = 'object'
                  AND COALESCE(
                        request.context #>>
                          '{{task_runtime_resolution_v1,runtime_metadata,agent_subscription_id}}',
                        ''
                      ) <> COALESCE(
                        request.context #>>
                          '{{task_runtime_checkpoint_v1,agent_subscription_id}}',
                        ''
                      )
            ) THEN
                RAISE EXCEPTION
                    'Cannot merge Agent subscriptions with a mismatched signed resolution';
            END IF;
        END
        $$
    '''))
    op.execute(sa.text(f'''
        CREATE TEMP TABLE "{_SIGNED_PINS}" (
            subscription_id VARCHAR(26) PRIMARY KEY
        ) ON COMMIT DROP
    '''))
    op.execute(sa.text(f'''
        INSERT INTO "{_SIGNED_PINS}" (subscription_id)
        SELECT DISTINCT
            request.context #>>
              '{{task_runtime_checkpoint_v1,agent_subscription_id}}'
        FROM hitl_requests AS request
        {task_join}
        WHERE {live_clause}
          AND jsonb_typeof(
                request.context -> 'task_runtime_checkpoint_v1'
              ) = 'object'
          AND COALESCE(
                request.context ->> 'task_runtime_checkpoint_signature_v1',
                ''
              ) ~ '^[0-9a-f]{{64}}$'
    '''))


def _assert_merge_is_unambiguous() -> None:
    op.execute(sa.text(f'''
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM agent_subscriptions AS subscription
                JOIN "{_SIGNED_PINS}" AS pin
                  ON pin.subscription_id = subscription.id
                WHERE subscription.workspace_id IS NOT NULL
                GROUP BY
                    subscription.entity_id,
                    subscription.agent_id,
                    subscription.workspace_id,
                    subscription.service_key
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Cannot merge Agent subscriptions pinned by multiple live signed checkpoints';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM agent_subscriptions AS subscription
                WHERE subscription.workspace_id IS NOT NULL
                GROUP BY
                    subscription.entity_id,
                    subscription.agent_id,
                    subscription.workspace_id,
                    subscription.service_key
                HAVING COUNT(*) > 1
                   AND COUNT(*) FILTER (
                        WHERE subscription.status = 'active'
                   ) > 1
                   AND (
                        COUNT(DISTINCT subscription.owner_user_id) FILTER (
                            WHERE subscription.status = 'active'
                              AND subscription.owner_user_id IS NOT NULL
                        ) > 1
                        OR COUNT(DISTINCT subscription.name) FILTER (
                            WHERE subscription.status = 'active'
                              AND subscription.name IS NOT NULL
                        ) > 1
                        OR COUNT(DISTINCT subscription.custom_prompt) FILTER (
                            WHERE subscription.status = 'active'
                              AND subscription.custom_prompt IS NOT NULL
                        ) > 1
                        OR COUNT(DISTINCT subscription.config) FILTER (
                            WHERE subscription.status = 'active'
                              AND subscription.config <> '{{}}'::jsonb
                        ) > 1
                   )
            ) THEN
                RAISE EXCEPTION
                    'Cannot merge active Agent subscriptions with conflicting runtime configuration';
            END IF;
        END
        $$
    '''))


def _rewrite_scalar_json_reference(
    table_name: str,
    column_name: str,
    path: str,
    predicate: str | None,
) -> None:
    if not table_exists(table_name) or not column_exists(table_name, column_name):
        return
    predicate_sql = f" AND ({predicate})" if predicate else ""
    op.execute(sa.text(f'''
        UPDATE "{table_name}" AS target
        SET "{column_name}" = jsonb_set(
            target."{column_name}",
            '{path}',
            to_jsonb(dedupe.canonical_id),
            false
        )
        FROM "{_DEDUPE_MAP}" AS dedupe
        WHERE target."{column_name}" #>> '{path}' = dedupe.duplicate_id
        {predicate_sql}
    '''))


def _rewrite_array_json_reference(
    table_name: str,
    column_name: str,
    array_path: str,
    item_key: str,
    predicate: str | None,
) -> None:
    if not table_exists(table_name) or not column_exists(table_name, column_name):
        return
    predicate_sql = f" AND ({predicate})" if predicate else ""
    op.execute(sa.text(f'''
        UPDATE "{table_name}" AS target
        SET "{column_name}" = jsonb_set(
            target."{column_name}",
            '{array_path}',
            (
                SELECT jsonb_agg(
                    CASE
                        WHEN dedupe.canonical_id IS NOT NULL
                        THEN jsonb_set(
                            item.value,
                            '{{{item_key}}}',
                            to_jsonb(dedupe.canonical_id),
                            false
                        )
                        ELSE item.value
                    END
                    ORDER BY item.ordinality
                )
                FROM jsonb_array_elements(
                    target."{column_name}" #> '{array_path}'
                ) WITH ORDINALITY AS item(value, ordinality)
                LEFT JOIN "{_DEDUPE_MAP}" AS dedupe
                  ON item.value ->> '{item_key}' = dedupe.duplicate_id
            ),
            false
        )
        WHERE jsonb_typeof(
                target."{column_name}" #> '{array_path}'
              ) = 'array'
          AND EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    target."{column_name}" #> '{array_path}'
                ) AS candidate(value)
                JOIN "{_DEDUPE_MAP}" AS dedupe
                  ON candidate.value ->> '{item_key}' = dedupe.duplicate_id
              )
          {predicate_sql}
    '''))


def _repair_json_references() -> None:
    for table_name, column_name, path, predicate in _JSON_SCALAR_REFERENCES:
        _rewrite_scalar_json_reference(
            table_name,
            column_name,
            path,
            predicate,
        )
    for (
        table_name,
        column_name,
        array_path,
        item_key,
        predicate,
    ) in _JSON_ARRAY_REFERENCES:
        _rewrite_array_json_reference(
            table_name,
            column_name,
            array_path,
            item_key,
            predicate,
        )


def _repair_duplicate_workspace_subscriptions() -> None:
    # Historical installs may predate the Workspace subscription uniqueness
    # constraint. Preserve a live signed continuation when present; otherwise
    # keep the active deployment. Redirect runtime references before deleting
    # retired aliases, without rewriting HMAC-authenticated checkpoint JSON.
    op.execute(sa.text(f'LOCK TABLE "{_TABLE}" IN SHARE ROW EXCLUSIVE MODE'))
    _create_signed_checkpoint_pins()
    _assert_merge_is_unambiguous()
    op.execute(sa.text(f'''
        CREATE TEMP TABLE "{_DEDUPE_MAP}" (
            duplicate_id VARCHAR(26) PRIMARY KEY,
            canonical_id VARCHAR(26) NOT NULL
        ) ON COMMIT DROP
    '''))
    op.execute(sa.text(f'''
        INSERT INTO "{_DEDUPE_MAP}" (duplicate_id, canonical_id)
        SELECT duplicate_id, canonical_id
        FROM (
            SELECT
                id AS duplicate_id,
                FIRST_VALUE(id) OVER (
                    PARTITION BY entity_id, agent_id, workspace_id, service_key
                    ORDER BY
                        CASE WHEN pin.subscription_id IS NOT NULL THEN 0 ELSE 1 END,
                        CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                        created_at ASC NULLS LAST,
                        id ASC
                ) AS canonical_id,
                ROW_NUMBER() OVER (
                    PARTITION BY entity_id, agent_id, workspace_id, service_key
                    ORDER BY
                        CASE WHEN pin.subscription_id IS NOT NULL THEN 0 ELSE 1 END,
                        CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                        created_at ASC NULLS LAST,
                        id ASC
                ) AS duplicate_rank
            FROM "{_TABLE}" AS subscription
            LEFT JOIN "{_SIGNED_PINS}" AS pin
              ON pin.subscription_id = subscription.id
            WHERE workspace_id IS NOT NULL
        ) AS ranked
        WHERE duplicate_rank > 1
    '''))

    if (
        table_exists("subscription_workers")
        and column_exists("subscription_workers", "subscription_id")
    ):
        op.execute(sa.text(f'''
            INSERT INTO subscription_workers AS binding (
                subscription_id,
                worker_id,
                priority,
                is_preferred,
                created_at
            )
            SELECT
                dedupe.canonical_id,
                binding.worker_id,
                MIN(binding.priority),
                BOOL_OR(binding.is_preferred),
                MIN(binding.created_at)
            FROM subscription_workers AS binding
            JOIN "{_DEDUPE_MAP}" AS dedupe
              ON dedupe.duplicate_id = binding.subscription_id
            GROUP BY dedupe.canonical_id, binding.worker_id
            ON CONFLICT (subscription_id, worker_id) DO UPDATE
            SET
                priority = LEAST(binding.priority, EXCLUDED.priority),
                is_preferred = binding.is_preferred OR EXCLUDED.is_preferred,
                created_at = LEAST(binding.created_at, EXCLUDED.created_at)
        '''))
        op.execute(sa.text(f'''
            DELETE FROM subscription_workers AS binding
            USING "{_DEDUPE_MAP}" AS dedupe
            WHERE binding.subscription_id = dedupe.duplicate_id
        '''))

    for table_name, column_name in _SUBSCRIPTION_REFERENCE_COLUMNS:
        if not table_exists(table_name) or not column_exists(table_name, column_name):
            continue
        op.execute(sa.text(f'''
            UPDATE "{table_name}" AS reference
            SET "{column_name}" = dedupe.canonical_id
            FROM "{_DEDUPE_MAP}" AS dedupe
            WHERE reference."{column_name}" = dedupe.duplicate_id
        '''))

    _repair_json_references()

    op.execute(sa.text(f'''
        DELETE FROM "{_TABLE}" AS subscription
        USING "{_DEDUPE_MAP}" AS dedupe
        WHERE subscription.id = dedupe.duplicate_id
    '''))


def upgrade() -> None:
    if not table_exists(_TABLE):
        return

    old_constraint = _unique_constraint_name(_OLD_COLUMNS)
    if old_constraint:
        op.drop_constraint(old_constraint, _TABLE, type_="unique")
    _repair_duplicate_workspace_subscriptions()
    if _unique_constraint_name(_NEW_COLUMNS) is None:
        op.create_unique_constraint(_NEW_CONSTRAINT, _TABLE, list(_NEW_COLUMNS))
    if not index_exists(_DEFAULT_INDEX):
        op.create_index(
            _DEFAULT_INDEX,
            _TABLE,
            list(_OLD_COLUMNS),
            unique=True,
            postgresql_where=sa.text(
                "workspace_id IS NOT NULL AND service_key IS NULL"
            ),
        )


def downgrade() -> None:
    if not table_exists(_TABLE):
        return
    if _has_multi_service_workspace_agents():
        raise RuntimeError(
            "Cannot downgrade while a Workspace Agent is bound to multiple "
            "services; remove the additional service bindings first."
        )

    if index_exists(_DEFAULT_INDEX):
        op.drop_index(_DEFAULT_INDEX, table_name=_TABLE)
    new_constraint = _unique_constraint_name(_NEW_COLUMNS)
    if new_constraint:
        op.drop_constraint(new_constraint, _TABLE, type_="unique")
    if _unique_constraint_name(_OLD_COLUMNS) is None:
        op.create_unique_constraint(None, _TABLE, list(_OLD_COLUMNS))
