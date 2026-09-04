from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def _sync_database_url() -> sa.engine.URL:
    raw = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test",
    )
    return make_url(raw).set(drivername="postgresql+psycopg2")


def _oss_migration_root(tmp_path: Path) -> Path:
    migration_root = tmp_path / "migrations"
    versions = migration_root / "versions"
    versions.mkdir(parents=True)
    shutil.copy2(ROOT / "packages/core/migrations/env.py", migration_root / "env.py")

    private_snapshots = ROOT / "scripts/oss_migrations/versions"
    source = private_snapshots if private_snapshots.is_dir() else ROOT / "packages/core/migrations/versions"
    for name in ("0001_oss_initial_schema.py", "0002_oss_schema_sync.py"):
        shutil.copy2(source / name, versions / name)
    return migration_root


def _seed_published_oss_data(connection: sa.Connection) -> None:
    connection.execute(
        sa.text("""
        INSERT INTO entities (id, name)
        VALUES ('entity_legacy', 'Legacy OSS')
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO users (
            id, entity_id, email, password_hash, role, timezone, locale, status
        ) VALUES (
            'user_legacy', 'entity_legacy', 'legacy@example.test', 'hash',
            'owner', 'UTC', 'en', 'active'
        )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO workspaces (
            id, entity_id, name, operating_model, status, heartbeat_enabled
        ) VALUES (
            'workspace_legacy', 'entity_legacy', 'Legacy Workspace',
            '{"goals":[{"goal_key":"contract_sales","metric_key":"sales"}]}'::jsonb,
            'active', false
        )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO conversations (id, entity_id, channel, status, scope)
        VALUES ('conversation_legacy', 'entity_legacy', 'workspace', 'active', 'workspace_main')
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO messages (
            id, conversation_id, role, content, refs, author_kind, message_kind
        ) VALUES (
            'message_legacy', 'conversation_legacy', 'assistant', 'Legacy response',
            '[]'::jsonb, 'agent', 'agent_update'
        )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO chat_message_feedback (
            id, entity_id, user_id, conversation_id, message_id, rating, metadata
        ) VALUES
            (
                'feedback_legacy', 'entity_legacy', 'user_legacy',
                'conversation_legacy', 'message_legacy', 'up', '{}'::jsonb
            ),
            (
                'feedback_orphan', 'entity_legacy', 'user_missing',
                'conversation_missing', 'message_missing', 'down', '{}'::jsonb
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO goals (
            id, entity_id, workspace_id, title, metric_key,
            target_value, status, priority, created_at
        ) VALUES
            (
                'goal_legacy_1', 'entity_legacy', 'workspace_legacy', 'Sales one',
                'sales', 10, 'active', 1, '2026-01-01T00:00:00Z'
            ),
            (
                'goal_legacy_2', 'entity_legacy', 'workspace_legacy', 'Sales two',
                'sales', 20, 'active', 1, '2026-01-02T00:00:00Z'
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO workspace_staff (
            id, workspace_id, user_id, role, status, created_at, updated_at
        ) VALUES
            (
                'workspace_staff_old', 'workspace_legacy', 'user_legacy',
                'owner', 'inactive', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            ),
            (
                'workspace_staff_new', 'workspace_legacy', 'user_legacy',
                'editor', 'active', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z'
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO documents (
            id, entity_id, name, fs_path, vector_status, source,
            is_trashed, created_at, updated_at
        ) VALUES
            (
                'document_legacy_old', 'entity_legacy', 'Old projection',
                'shared/report.md', 'pending', 'upload', false,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            ),
            (
                'document_legacy_new', 'entity_legacy', 'New projection',
                'shared/report.md', 'pending', 'upload', false,
                '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z'
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO message_logs (
            id, entity_id, channel_config_id, external_id,
            direction, channel_type, status, created_at
        ) VALUES
            (
                'message_log_old', 'entity_legacy', 'config_legacy', 'event_legacy',
                'inbound', 'whatsapp', 'received', '2026-01-01T00:00:00Z'
            ),
            (
                'message_log_new', 'entity_legacy', 'config_legacy', 'event_legacy',
                'inbound', 'whatsapp', 'received', '2026-01-02T00:00:00Z'
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO channels (id, entity_id, type, config, status)
        VALUES
            (
                'channel_legacy_1', 'entity_legacy', 'whatsapp',
                '{"channel_config_id":"config_legacy"}'::jsonb, 'active'
            ),
            (
                'channel_legacy_2', 'entity_legacy', 'whatsapp',
                '{"channel_config_id":"config_legacy"}'::jsonb, 'active'
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO skills (
            id, entity_id, slug, name, system_prompt, output_format,
            is_public, version, status, created_at
        ) VALUES
            (
                'skill_builtin_old', NULL, 'legacy-builtin', 'Legacy Builtin',
                'Do work', 'text', true, '1.0.0', 'active', '2026-01-01T00:00:00Z'
            ),
            (
                'skill_builtin_new', NULL, 'legacy-builtin', 'Legacy Builtin copy',
                'Do work', 'text', true, '1.0.1', 'active', '2026-01-02T00:00:00Z'
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO workflow_definitions (
            id, entity_id, name, trigger_type, is_active, version, status
        ) VALUES (
            'workflow_legacy', 'entity_legacy', 'Legacy Flow', 'manual', true, 3, 'active'
        )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO workflow_runs (
            id, workflow_id, entity_id, binding_id, status, trigger_source,
            trigger_data, created_at
        ) VALUES
            (
                'workflow_run_old', 'workflow_legacy', 'entity_legacy',
                'binding_legacy', 'completed', 'public_webchat',
                '{"webchat_session_id":"session_legacy","webchat_module_id":"module_legacy","webchat_submission_id":"submission_legacy"}'::jsonb,
                '2026-01-01T00:00:00Z'
            ),
            (
                'workflow_run_new', 'workflow_legacy', 'entity_legacy',
                'binding_legacy', 'completed', 'public_webchat',
                '{"webchat_session_id":"session_legacy","webchat_module_id":"module_legacy","webchat_submission_id":"submission_legacy"}'::jsonb,
                '2026-01-02T00:00:00Z'
            )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO notifications (
            id, entity_id, user_id, type, metadata, deliver_at, dispatch_status
        ) VALUES (
            'notification_legacy', 'entity_legacy', 'user_legacy', 'task',
            '{"workspace_id":"workspace_legacy","_scheduled":{"channel":"in_app"}}'::jsonb,
            CURRENT_TIMESTAMP, 'pending'
        )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO tasks (
            id, entity_id, workspace_id, title, status, priority, task_type,
            created_at, updated_at
        ) VALUES (
            'task_legacy', 'entity_legacy', 'workspace_legacy', 'Legacy Task',
            'completed', 1, 'work', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'
        )
    """)
    )
    connection.execute(
        sa.text("""
        INSERT INTO scheduled_jobs (
            id, job_id, entity_id, workspace_id, job_type, schedule_kind,
            every_seconds, timezone, enabled, consecutive_errors
        ) VALUES (
            'scheduled_job_legacy', 'legacy-job', 'entity_legacy', 'workspace_legacy',
            'agent', 'every', 3600, 'UTC', true, 0
        )
    """)
    )


@pytest.mark.oss_regression
def test_published_oss_database_with_legacy_data_upgrades_to_current_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url = _sync_database_url()
    database_name = f"manor_oss_upgrade_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    maintenance_url = base_url.set(database="postgres")
    database_url = base_url.set(database=database_name)
    admin = sa.create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    engine: sa.Engine | None = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))

        migration_root = _oss_migration_root(tmp_path)
        rendered_url = database_url.render_as_string(hide_password=False)
        monkeypatch.setenv("DATABASE_URL_SYNC", rendered_url)
        config = Config()
        config.set_main_option("script_location", str(migration_root))
        config.set_main_option("sqlalchemy.url", rendered_url)

        command.upgrade(config, "0001_oss_initial")
        engine = sa.create_engine(database_url)
        with engine.begin() as connection:
            _seed_published_oss_data(connection)

        command.upgrade(config, "head")

        with engine.connect() as connection:
            feedback = connection.execute(
                sa.text("""
                SELECT target_kind, target_id, mutation_sequence
                  FROM chat_message_feedback
                 WHERE id = 'feedback_legacy'
            """)
            ).one()
            assert feedback == ("response", "message_legacy", 1)
            assert (
                connection.scalar(sa.text("SELECT count(*) FROM chat_message_feedback WHERE id = 'feedback_orphan'"))
                == 0
            )

            goal_keys = (
                connection.execute(
                    sa.text("""
                SELECT goal_key FROM goals ORDER BY created_at, id
            """)
                )
                .scalars()
                .all()
            )
            assert goal_keys == ["contract_sales", "sales"]

            membership = connection.execute(
                sa.text("""
                SELECT id, role FROM workspace_staff
                 WHERE workspace_id = 'workspace_legacy' AND user_id = 'user_legacy'
            """)
            ).one()
            assert membership == ("workspace_staff_new", "editor")

            documents = connection.execute(
                sa.text("""
                SELECT id, fs_path, is_trashed
                  FROM documents
                 WHERE id IN ('document_legacy_old', 'document_legacy_new')
                 ORDER BY id
            """)
            ).all()
            assert documents == [
                ("document_legacy_new", "shared/report.md", False),
                ("document_legacy_old", None, True),
            ]
            assert (
                connection.scalar(
                    sa.text("""
                SELECT count(*) FROM message_logs
                 WHERE channel_type = 'whatsapp' AND external_id = 'event_legacy'
            """)
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.text("""
                SELECT count(*) FROM channels
                 WHERE type = 'whatsapp' AND status = 'active'
                   AND config ->> 'channel_config_id' = 'config_legacy'
            """)
                )
                == 0
            )

            builtin_skills = connection.execute(
                sa.text("""
                SELECT id, slug, status FROM skills
                 WHERE id IN ('skill_builtin_old', 'skill_builtin_new')
                 ORDER BY id
            """)
            ).all()
            assert builtin_skills[1] == (
                "skill_builtin_old",
                "legacy-builtin",
                "active",
            )
            assert builtin_skills[0][1].startswith("legacy-builtin--duplicate-")
            assert builtin_skills[0][2] == "inactive"

            webchat_owners = connection.execute(
                sa.text("""
                SELECT id, webchat_submission_id
                  FROM workflow_runs
                 WHERE id IN ('workflow_run_old', 'workflow_run_new')
                 ORDER BY id
            """)
            ).all()
            assert webchat_owners == [
                ("workflow_run_new", "submission_legacy"),
                ("workflow_run_old", None),
            ]
            assert (
                connection.scalar(
                    sa.text("""
                SELECT execution_snapshot <> '{}'::jsonb
                  FROM workflow_runs WHERE id = 'workflow_run_old'
            """)
                )
                is True
            )
            assert (
                connection.scalar(
                    sa.text("""
                SELECT count(*) FROM notification_outbox_events
                 WHERE notification_id = 'notification_legacy'
            """)
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.text("""
                SELECT status_changed_at IS NOT NULL FROM tasks WHERE id = 'task_legacy'
            """)
                )
                is True
            )
            assert (
                connection.scalar(
                    sa.text("""
                SELECT next_run_at IS NOT NULL
                  FROM scheduled_jobs WHERE id = 'scheduled_job_legacy'
            """)
                )
                is True
            )

            indexes = set(
                connection.execute(
                    sa.text("""
                SELECT indexname FROM pg_indexes WHERE schemaname = 'public'
            """)
                ).scalars()
            )
            assert {
                "ix_channel_configs_entity_owner_source",
                "ix_integrations_entity_owner_provider",
                "ix_sjr_prepared_recovery",
                "ix_sjr_published_recovery",
                "ix_sjr_settlement_recovery",
                "ix_sjr_projection_recovery",
            } <= indexes

            cloud_only_tables = set(
                connection.execute(
                    sa.text("""
                SELECT tablename
                  FROM pg_tables
                 WHERE schemaname = 'public'
                   AND tablename IN ('blueprint_purchases', 'merchant_accounts')
            """)
                ).scalars()
            )
            assert cloud_only_tables == set()
    finally:
        if engine is not None:
            engine.dispose()
        try:
            with admin.connect() as connection:
                connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        finally:
            admin.dispose()
