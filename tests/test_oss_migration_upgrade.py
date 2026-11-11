from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
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

    source_versions = ROOT / "packages/core/migrations/versions"
    for source in sorted(source_versions.glob("*.py")):
        shutil.copy2(source, versions / source.name)
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
@pytest.mark.parametrize("upgrade_start", ["initial", "published_head"])
def test_published_oss_database_with_legacy_data_upgrades_to_current_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upgrade_start: str,
) -> None:
    baseline_root = None
    if upgrade_start == "published_head":
        raw_baseline = os.getenv("OSS_BASELINE_DIR")
        if not raw_baseline:
            pytest.skip("OSS_BASELINE_DIR is required for the locked public HEAD upgrade")
        baseline_root = Path(raw_baseline).resolve()
        assert (baseline_root / "packages/core/migrations/versions").is_dir()
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

        baseline_config = None
        if baseline_root is not None:
            baseline_migrations = tmp_path / "published-migrations"
            shutil.copytree(baseline_root / "packages/core/migrations", baseline_migrations)
            baseline_config = Config()
            baseline_config.set_main_option("script_location", str(baseline_migrations))
            baseline_config.set_main_option("sqlalchemy.url", rendered_url)
        command.upgrade(baseline_config or config, "0001_oss_initial")
        engine = sa.create_engine(database_url)
        with engine.begin() as connection:
            _seed_published_oss_data(connection)

        # Historical capability grants require an operator-reviewed replacement.
        # A published-baseline database must complete that same reviewed
        # cutover before it can represent an installed public release.
        upgrade_config = baseline_config or config
        command.upgrade(upgrade_config, "20261004_03")
        from packages.core.migrations import unified_approval
        from packages.core.migrations.unified_approval import preflight
        with engine.begin() as connection:
            metadata = sa.MetaData()
            plans = sa.Table('execution_plans', metadata, autoload_with=connection)
            steps = sa.Table('execution_steps', metadata, autoload_with=connection)
            requests = sa.Table('hitl_requests', metadata, autoload_with=connection)
            workspaces = sa.Table('workspaces', metadata, autoload_with=connection)
            operating = connection.execute(sa.select(workspaces.c.operating_model).where(
                workspaces.c.id == 'workspace_legacy')).scalar_one()
            operating['rules'] = [{'rule_key': 'ask_send', 'rule_type': 'approval_required',
                                   'actions': ['communication.send']}]
            connection.execute(workspaces.update().where(workspaces.c.id == 'workspace_legacy').values(operating_model=operating))
            connection.execute(plans.insert().values(id='legacy_plan', entity_id='entity_legacy',
                workspace_id='workspace_legacy', status='paused'))
            connection.execute(steps.insert().values(id='legacy_step', plan_id='legacy_plan',
                entity_id='entity_legacy', workspace_id='workspace_legacy', step_key='send',
                kind='action', action_key='communication.send', params={'to': 'alice'},
                step_status='waiting_human', attempt_count=0, max_attempts=3))
            connection.execute(requests.insert().values(id='legacy_approval', entity_id='entity_legacy',
                workspace_id='workspace_legacy', origin_kind='step', origin_step_id='legacy_step',
                origin_plan_id='legacy_plan', action_key='communication.send', risk_level='medium',
                status='pending', dedup_key='legacy', hitl_type='authorize', context={}))
        with engine.connect() as connection:
            initial_plan = preflight(connection)
            assert {item["scope"] for item in initial_plan["blockers"]} == {
                "workspace:workspace_legacy", "request:legacy_approval"}
            operating_model = connection.execute(sa.text(
                "SELECT operating_model FROM workspaces WHERE id='workspace_legacy'"
            )).scalar_one()
            reviewed_plan = preflight(connection, resolutions={"workspace:workspace_legacy": {
                "policy": {"approval": {"root_entity_id": "entity_legacy", "policy": {
                    "mode": "confirm_changes", "rules": []}}},
                "operating_model": operating_model,
            }, "request:legacy_approval": {
                "status": "expired", "resolved_reason": "approval_migration_unverifiable",
            }})
            assert not reviewed_plan["blockers"]
        plan_path = tmp_path / "reviewed-approval-migration.json"
        plan_path.write_text(json.dumps(reviewed_plan, default=str))
        from scripts import preflight_approval_upgrade as preflight_cli

        def read_only_preflight(connection, *, resolutions=None):
            assert connection.scalar(sa.text('SHOW transaction_read_only')) == 'on'
            return preflight(connection, resolutions=resolutions)

        with monkeypatch.context() as patch:
            patch.setattr(preflight_cli, 'preflight', read_only_preflight)
            patch.setattr('sys.argv', ['preflight', '--check'])
            with pytest.raises(SystemExit) as exc:
                preflight_cli.main()
            assert exc.value.code == 2
        monkeypatch.setenv("MANOR_APPROVAL_MIGRATION_PLAN", str(plan_path))
        original_apply = unified_approval.apply

        def fail_after_writes(connection, plan):
            original_apply(connection, plan)
            raise RuntimeError('injected cutover failure')

        with monkeypatch.context() as patch:
            patch.setattr(unified_approval, 'apply', fail_after_writes)
            with pytest.raises(RuntimeError, match='injected cutover failure'):
                command.upgrade(upgrade_config, 'head')
        with engine.connect() as connection:
            assert connection.scalar(sa.text('SELECT version_num FROM alembic_version')) == '20261004_03'
            assert 'approval_policy' not in {col['name'] for col in sa.inspect(connection).get_columns('chat_rooms')}
            assert connection.scalar(sa.text("SELECT status FROM hitl_requests WHERE id='legacy_approval'")) == 'pending'
            assert connection.scalar(sa.text("SELECT count(*) FROM audit_log WHERE action='approval_policy.migrated'")) == 0
        command.upgrade(upgrade_config, "head")

        with engine.connect() as connection:
            audit_count = connection.scalar(sa.text("SELECT count(*) FROM audit_log WHERE action='approval_policy.migrated'"))
            assert connection.scalar(sa.text("SELECT status FROM hitl_requests WHERE id='legacy_approval'")) == 'expired'
            assert connection.scalar(sa.text("SELECT step_status FROM execution_steps WHERE id='legacy_step'")) == 'waiting_human'
            receipt = connection.scalar(sa.text("SELECT details FROM audit_log WHERE resource_id='legacy_approval'"))
            assert receipt['source']['status'] == 'pending'
            assert receipt['after']['context'] == receipt['source']['context'] == {}
            migrated_operating = connection.scalar(sa.text("SELECT operating_model FROM workspaces WHERE id='workspace_legacy'"))
            assert migrated_operating['rules'][0]['approval_selectors'] == [
                {'kind': 'action', 'action_key': 'communication.send'}]
            assert not preflight(connection)['operations']
        command.upgrade(upgrade_config, 'head')
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT count(*) FROM audit_log WHERE action='approval_policy.migrated'")) == audit_count

        if baseline_config is not None:
            with engine.begin() as connection:
                connection.execute(sa.text("""
                    INSERT INTO workflow_runs (id, workflow_id, entity_id, status)
                    VALUES ('workflow_run_inflight', 'workflow_legacy', 'entity_legacy', 'running')
                """))
                connection.execute(sa.text("""
                    INSERT INTO channel_configs (
                        id, entity_id, channel_type, provider, status,
                        credential_ref, credential_scheme
                    ) VALUES (
                        'channel_config_baseline', 'entity_legacy', 'telegram', 'telegram',
                        'active', 'test-reference-only', 'vault_transit'
                    )
                """))
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    ScriptDirectory.from_config(baseline_config).get_current_head()
                )
                assert connection.scalar(sa.text(
                    "SELECT content FROM messages WHERE id = 'message_legacy'"
                )) == "Legacy response"
                assert connection.scalar(sa.text(
                    "SELECT status FROM workflow_runs WHERE id = 'workflow_run_inflight'"
                )) == "running"
            command.upgrade(config, "head")

        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            if baseline_root is not None:
                assert connection.scalar(sa.text(
                    "SELECT status FROM workflow_runs WHERE id = 'workflow_run_inflight'"
                )) == "running"
                assert connection.scalar(sa.text(
                    "SELECT credential_ref FROM channel_configs WHERE id = 'channel_config_baseline'"
                )) == "test-reference-only"
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                ScriptDirectory.from_config(config).get_current_head()
            )
            assert "approval_policy" in {column["name"] for column in inspector.get_columns("chat_rooms")}
            assert "approval_requirements" in {column["name"] for column in inspector.get_columns("chat_room_tool_approvals")}
            assert "approval_operation_revision" in {column["name"] for column in inspector.get_columns("execution_steps")}
            assert "ix_hitl_requests_entity_operation" in {index["name"] for index in inspector.get_indexes("hitl_requests")}
            migrated_policy = connection.execute(sa.text(
                "SELECT policy FROM governance_policies WHERE workspace_id='workspace_legacy'"
            )).scalar_one()
            assert migrated_policy["approval"]["root_entity_id"] == "entity_legacy"
            assert migrated_policy["approval"]["policy"]["mode"] == "confirm_changes"
            assert connection.execute(sa.text(
                "SELECT count(*) FROM audit_log WHERE action='approval_policy.migrated'"
            )).scalar_one() > 0
            assert "assistant_blocks" in {
                column["name"] for column in inspector.get_columns("chat_room_agent_runs")
            }
            assert {
                "task_coordinator_runs", "runtime_file_commits",
                "conversation_transcript_items", "task_run_journal",
                "task_agent_child_runs",
            } <= set(inspector.get_table_names())
            # 20261025_10 drops the retired planner's manual Plan submissions.
            assert "task_plan_submissions" not in inspector.get_table_names()
            assert "engine" in {
                column["name"] for column in inspector.get_columns("task_coordinator_runs")
            }
            assert "effect_id" in {
                column["name"] for column in inspector.get_columns("runtime_file_commits")
            }
            assert "last_assessment" in {
                column["name"] for column in inspector.get_columns("workspace_stats")
            }
            assert {"operation_id", "media_job_id", "task_id"} <= {
                column["name"] for column in inspector.get_columns("token_usage_logs")
            }
            assert {"ix_token_usage_operation", "ix_token_usage_media_job", "ix_token_usage_task"} <= {
                index["name"] for index in inspector.get_indexes("token_usage_logs")
            }
            assert "ix_workflow_runs_parent_run_id" in {
                index["name"] for index in inspector.get_indexes("workflow_runs")
            }
            # 20261104_20: content-free failure codes on the daily health rows.
            assert "error_codes" in {
                column["name"] for column in inspector.get_columns("metrics_workspace_daily")
            }
            assert next(
                column for column in inspector.get_columns("workspace_stat_observations")
                if column["name"] == "value"
            )["nullable"] is True
            assert "ix_plans_wake_runnable" in {
                index["name"] for index in inspector.get_indexes("execution_plans")
            }
            conversations = {
                column["name"]: column
                for column in inspector.get_columns("conversations")
            }
            assert {
                "summary_covered_at",
                "summary_covered_message_id",
                "summary_version",
            } <= set(conversations)
            assert conversations["summary_version"]["nullable"] is False
            assert connection.execute(
                sa.text("""
                    SELECT id, summary, summary_covered_at,
                           summary_covered_message_id, summary_version
                    FROM conversations
                    WHERE id = 'conversation_legacy'
                """)
            ).one() == ("conversation_legacy", None, None, None, 0)
            assert "code_projects" in inspector.get_table_names()
            assert "project_id" in {column["name"] for column in inspector.get_columns("sites")}
            assert {
                "snapshot_key", "content_sha256", "workspace_id", "provenance",
            } <= {column["name"] for column in inspector.get_columns("document_versions")}
            assert {
                "ix_documents_entity_owner",
                "ix_documents_browse_date",
                "ix_documents_browse_folder_date",
            } <= {index["name"] for index in inspector.get_indexes("documents")}
            assert {
                "ix_document_folders_entity_owner",
                "uq_document_folders_entity_parent_owner_name",
            } <= {index["name"] for index in inspector.get_indexes("document_folders")}
            assert "ix_messages_open_cards" in {index["name"] for index in inspector.get_indexes("messages")}
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

            browser_session_columns = {
                column["name"]: column
                for column in inspector.get_columns("browser_sessions")
            }
            assert {
                "id",
                "user_id",
                "entity_id",
                "token_version",
                "mfa_authenticated",
                "token_hash",
                "previous_hash",
                "rotation",
                "rotated_at",
                "expires_at",
                "revoked_at",
                "created_at",
                "updated_at",
            } <= set(browser_session_columns)
            assert browser_session_columns["user_id"]["nullable"] is False
            assert browser_session_columns["expires_at"]["nullable"] is False

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
                "ix_browser_sessions_user_id",
                "ix_browser_sessions_expires_at",
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


@pytest.mark.oss_regression
@pytest.mark.parametrize("install", ["fresh", "read_then_create"])
def test_oss_resident_migrations_support_real_orm_insert_and_upgrade(tmp_path, monkeypatch, install):
    from sqlalchemy.orm import Session
    from packages.core.models.webchat_resident import WebchatResidentBinding
    from packages.core.models.webchat_resident_create import ResidentCreateGrant, ResidentMaintenanceDraft

    base_url = _sync_database_url()
    database_name = f"manor_oss_resident_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    database_url = base_url.set(database=database_name)
    admin = sa.create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        created = True
        rendered_url = database_url.render_as_string(hide_password=False)
        monkeypatch.setenv("DATABASE_URL_SYNC", rendered_url)
        config = Config()
        config.set_main_option("script_location", str(_oss_migration_root(tmp_path)))
        config.set_main_option("sqlalchemy.url", rendered_url)
        engine = sa.create_engine(database_url)
        command.upgrade(config, "20260911_03" if install == "read_then_create" else "head")
        with engine.begin() as connection:
            connection.execute(sa.text("INSERT INTO entities (id, name) VALUES ('resident_legacy', 'Synthetic legacy')"))
        if install == "read_then_create":
            command.upgrade(config, "20260917_05")
            assert not sa.inspect(engine).has_table("webchat_resident_create_grants")

        # Exercise the real model against released DDL, never metadata.create_all.
        with Session(engine) as db:
            binding = WebchatResidentBinding(
                entity_id="resident_legacy", workspace_id="workspace", channel_binding_id="channel",
                channel_config_id="config", channel_contact_id="contact", conversation_id="conversation",
                session_id="session", target_account_id="account", target_version="version",
                issuer="https://pms.example.test", pms_entity_id=-1, browser_proof_hash="a" * 64,
                credential_scheme="dev_fernet", status="pending", generation=0,
            )
            db.add(binding)
            db.commit()
            binding_id, before_created, before_updated = binding.id, binding.created_at, binding.updated_at
            assert before_created is not None and before_updated is not None
        if install == "read_then_create":
            command.upgrade(config, "head")

        with Session(engine) as db:
            binding = db.get(WebchatResidentBinding, binding_id)
            assert (binding.created_at, binding.updated_at, binding.browser_proof_hash) == (
                before_created, before_updated, "a" * 64,
            )
            grant = ResidentCreateGrant(
                binding_id=binding_id, issuer=binding.issuer, credential_scheme="dev_fernet",
                read_generation=binding.generation,
            )
            draft = ResidentMaintenanceDraft(
                binding_id=binding_id, fields={"title": "Synthetic repair"}, payload_hash="b" * 64,
                submission_key="c" * 48,
            )
            db.add_all([grant, draft])
            db.commit()
            assert grant.created_at and grant.updated_at and draft.created_at and draft.updated_at
            assert db.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                ScriptDirectory.from_config(config).get_current_head()
            )
            assert db.scalar(sa.text("SELECT name FROM entities WHERE id='resident_legacy'")) == "Synthetic legacy"
            db.delete(binding)
            db.commit()
            assert db.scalar(sa.select(sa.func.count()).select_from(ResidentCreateGrant)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(ResidentMaintenanceDraft)) == 0
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if created:
                with admin.connect() as connection:
                    connection.execute(sa.text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        finally:
            admin.dispose()
