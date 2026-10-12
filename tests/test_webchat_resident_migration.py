"""ORM insertion must also work against the released Alembic schema."""
import importlib
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session

from packages.core.models.webchat_resident import WebchatResidentBinding


def test_pending_binding_inserts_against_formal_migration():
    migration = importlib.import_module(
        "packages.core.migrations.versions.20260917_05_webchat_resident_binding"
    )
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        # Do not use Base.metadata.create_all: that hid a real migration/ORM
        # mismatch where the ORM explicitly inserted a NULL updated_at.
        with Session(bind=connection) as session:
            binding = WebchatResidentBinding(
                entity_id="test_entity", workspace_id="test_workspace",
                channel_binding_id="test_channel_binding", channel_config_id="test_channel",
                channel_contact_id="test_contact", conversation_id="test_conversation",
                session_id="test_session", target_account_id="test_account",
                target_version="test_version", issuer="http://127.0.0.1:18711",
                pms_entity_id=-1, browser_proof_hash="a" * 64,
                credential_scheme="dev_fernet", status="pending", generation=1,
            )
            session.add(binding)
            session.flush()
            session.expire_all()
            saved = session.scalar(select(WebchatResidentBinding))
            assert saved is not None
            assert saved.status == "pending"
            assert saved.created_at is not None
            assert saved.updated_at is not None
    engine.dispose()


@pytest.mark.parametrize("edition", ["cloud", "oss"])
def test_rebased_resident_chain_preserves_published_apify_and_existing_users(monkeypatch, edition):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "packages/core/migrations"))
    script = ScriptDirectory.from_config(config)
    # The resident chain ends at 20260917_08; later revisions (sandbox task
    # tickets, human-input resume) are PostgreSQL-only and out of scope here.
    revisions = list(reversed(list(script.iterate_revisions("20260917_08", "20260917_01"))))
    assert [item.revision for item in revisions] == [
        "20260917_05", "20260917_06", "20260917_07", "20260917_08",
    ]
    # Adapt only the PostgreSQL JSON spelling; execute the official DDL bodies.
    monkeypatch.setattr(SQLiteTypeCompiler, "visit_JSONB", SQLiteTypeCompiler.visit_JSON, raising=False)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT, status TEXT)"))
        connection.execute(text("INSERT INTO users VALUES ('old', 'old@example.test', 'active')"))
        connection.execute(text("CREATE TABLE oauth_client_apps (id TEXT PRIMARY KEY, client_secret_hash TEXT)"))
        connection.execute(text("INSERT INTO oauth_client_apps VALUES ('old-client', 'unchanged-fixture-hash')"))
        connection.execute(text("CREATE TABLE oauth_authorization_codes (id TEXT PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)):
            script.get_revision("20260917_01").module.upgrade()
        connection.execute(text("""
            INSERT INTO apify_collections
                (id, entity_id, user_id, credential_scope, spec, pricing,
                 max_cost_usd, max_credits, quote_expires_at, status, billing_status)
            VALUES ('existing', 'entity', 'user', 'user', '{}', '{}',
                    1, 10, '2099-01-01', 'quoted', 'pending')
        """))
        before = dict(connection.execute(text("SELECT * FROM apify_collections")).mappings().one())
        for item in revisions:
            module = item.module
            asset = root / "scripts/oss_release_assets/migrations" / Path(item.path).name
            if edition == "oss" and asset.exists():
                spec = importlib.util.spec_from_file_location("resident_public_" + item.revision, asset)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
                module.upgrade()  # Existing feature rows/columns remain safe on retry.
        assert dict(connection.execute(text("SELECT * FROM apify_collections")).mappings().one()) == before
        assert connection.execute(text("SELECT email, status, verified_email, email_verified_at FROM users")).one() == (
            "old@example.test", "active", None, None,
        )
        assert connection.execute(text("SELECT client_secret_hash, require_pkce, require_verified_email FROM oauth_client_apps")).one() == (
            "unchanged-fixture-hash", None, 0,
        )
        assert {
            "webchat_resident_bindings", "webchat_resident_create_grants",
            "webchat_resident_maintenance_drafts", "apify_collections",
        } <= set(inspect(connection).get_table_names())
    engine.dispose()
