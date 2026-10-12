"""Run the public Stat assessment schema migration against an isolated database."""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def _public_stat_revision():
    path = ROOT / "packages/core/migrations/versions/20260927_01_oss_runtime_schema.py"
    spec = importlib.util.spec_from_file_location("oss_stat_assessment_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.oss_regression
def test_public_stat_assessment_schema_upgrade_preserves_unknown_history():
    revision = _public_stat_revision()
    base_url = make_url(os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg2://manor:manor_secret@localhost:5434/manor_test",
    )).set(drivername="postgresql+psycopg2")
    database_name = f"manor_oss_stat_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine: sa.Engine | None = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        engine = sa.create_engine(base_url.set(database=database_name))
        with engine.begin() as connection:
            connection.execute(sa.text("""
                CREATE TABLE execution_plans (
                    id text PRIMARY KEY, status text, updated_at timestamptz, created_at timestamptz
                )
            """))
            connection.execute(sa.text("CREATE TABLE work_leases (plan_id text, status text)"))
            connection.execute(sa.text("""
                CREATE TABLE runtime_outbox_events (
                    id text PRIMARY KEY, available_at timestamptz, created_at timestamptz,
                    delivered_at timestamptz, event_type text, aggregate_id text
                )
            """))
            connection.execute(sa.text("CREATE TABLE tasks (id text PRIMARY KEY)"))
            connection.execute(sa.text("CREATE TABLE workspace_stats (id text PRIMARY KEY)"))
            connection.execute(sa.text("""
                CREATE TABLE workspace_stat_observations (value numeric(24, 6) NOT NULL)
            """))
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context), context.begin_transaction():
                revision.upgrade()

        with engine.begin() as connection:
            connection.execute(sa.text("""
                INSERT INTO workspace_stats (id, last_assessment)
                VALUES ('stat-1', '{"status":"insufficient_evidence","evidence":{"gaps":["no data"]}}')
            """))
            connection.execute(sa.text("INSERT INTO workspace_stat_observations (value) VALUES (NULL)"))
            assert connection.scalar(sa.text("""
                SELECT last_assessment->>'status' FROM workspace_stats WHERE id = 'stat-1'
            """)) == "insufficient_evidence"
            assert connection.scalar(sa.text("""
                SELECT (last_assessment->'evidence'->'gaps'->>0) FROM workspace_stats WHERE id = 'stat-1'
            """)) == "no data"
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            with pytest.raises(RuntimeError, match="Cannot discard unknown"):
                with Operations.context(context), context.begin_transaction():
                    revision.downgrade()
        with engine.connect() as connection:
            assert connection.scalar(sa.text("""
                SELECT last_assessment->>'status' FROM workspace_stats WHERE id = 'stat-1'
            """)) == "insufficient_evidence"
            assert connection.scalar(sa.text("""
                SELECT value IS NULL FROM workspace_stat_observations
            """)) is True
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
