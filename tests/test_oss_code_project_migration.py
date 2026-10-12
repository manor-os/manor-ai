from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[1]


def _public_code_project_revision():
    path = ROOT / "packages/core/migrations/versions/20260926_04_code_projects.py"
    spec = importlib.util.spec_from_file_location("oss_code_project_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_public_code_project_migration_repeats_without_losing_existing_data(db_session, monkeypatch):
    revision = _public_code_project_revision()
    schema = "oss_code_project_migration_test"

    def migrate(sync_session):
        connection = sync_session.connection()
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        connection.execute(text("CREATE TABLE sites (id VARCHAR(26) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO sites (id) VALUES ('site-1')"))
        monkeypatch.setattr(
            revision,
            "table_exists",
            lambda table: inspect(connection).has_table(table, schema=schema),
        )
        monkeypatch.setattr(
            revision,
            "column_exists",
            lambda table, column: column in {
                item["name"] for item in inspect(connection).get_columns(table, schema=schema)
            },
        )
        monkeypatch.setattr(
            revision,
            "index_exists",
            lambda name: connection.scalar(text("""
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes WHERE schemaname = :schema AND indexname = :name
                )
            """), {"schema": schema, "name": name}),
        )
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            connection.execute(text("""
                INSERT INTO code_projects (id, entity_id, workspace_id, name, root_path, profile)
                VALUES ('project-1', 'entity-1', 'workspace-1', 'Existing project', 'Projects/existing', 'generic')
            """))
            connection.execute(text("UPDATE sites SET project_id = 'project-1' WHERE id = 'site-1'"))
            revision.upgrade()

            assert connection.execute(text("""
                SELECT name, root_path FROM code_projects WHERE id = 'project-1'
            """)).one() == ("Existing project", "Projects/existing")
            assert connection.scalar(text("SELECT project_id FROM sites WHERE id = 'site-1'")) == "project-1"
            inspector = inspect(connection)
            assert {"uq_code_projects_entity_root", "ix_code_projects_entity_workspace"} <= {
                index["name"] for index in inspector.get_indexes(
                    "code_projects", schema=schema
                )
            }
            assert "ix_sites_project" in {
                index["name"] for index in inspector.get_indexes(
                    "sites", schema=schema
                )
            }
        connection.execute(text("SET LOCAL search_path TO public"))
        connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))

    await db_session.run_sync(migrate)
