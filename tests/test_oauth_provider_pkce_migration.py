"""Exercise public OAuth PKCE and verified-email migrations."""
import importlib
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from packages.core.models.oauth_provider import OAuthAuthorizationCode, OAuthClientApp, PersonalAccessToken

from alembic.migration import MigrationContext
from alembic.operations import Operations



ROOT = Path(__file__).resolve().parents[1]


def _public_migration_root(tmp_path: Path) -> Path:
    migration_root = tmp_path / "migrations"
    versions = migration_root / "versions"
    versions.mkdir(parents=True)
    shutil.copy2(ROOT / "packages/core/migrations/env.py", migration_root / "env.py")


    for source in sorted((ROOT / "packages/core/migrations/versions").glob("*.py")):
        shutil.copy2(source, versions / source.name)
    return migration_root


def _sync_database_url() -> sa.engine.URL:
    return make_url(os.getenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test",
    )).set(drivername="postgresql+psycopg2")




def test_public_oauth_pkce_and_email_proof_migrations_preserve_legacy_rows_and_current_writes(monkeypatch):
    pkce = importlib.import_module("packages.core.migrations.versions.20260917_07_oauth_provider_pkce")
    proof = importlib.import_module("packages.core.migrations.versions.20260917_08_user_email_proof")
    assert pkce.down_revision == "20260917_06"
    assert proof.down_revision == pkce.revision

    base_url = _sync_database_url()
    database_name = f"manor_oss_oauth_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine: sa.Engine | None = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        with tempfile.TemporaryDirectory(prefix="manor-oss-oauth-") as temp_dir:
            config = Config()
            config.set_main_option("script_location", str(_public_migration_root(Path(temp_dir))))
            database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
            config.set_main_option("sqlalchemy.url", database_url)
            monkeypatch.setenv("DATABASE_URL_SYNC", database_url)
            command.upgrade(config, "20260917_06")
            assert ScriptDirectory.from_config(config).get_revision("20260917_06") is not None
            engine = sa.create_engine(base_url.set(database=database_name))
            with engine.begin() as conn:
                assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "20260917_06"
                conn.execute(sa.text("""
                    INSERT INTO oauth_client_apps (id, client_id, client_secret_hash, name)
                    VALUES ('legacy', 'legacy', 'unused', 'Existing')
                """))
                conn.execute(sa.text("""
                    INSERT INTO oauth_authorization_codes
                        (id, code, client_id, user_id, entity_id, redirect_uri, expires_at)
                    VALUES
                        ('old-code', 'old-code', 'legacy', 'user', 'entity',
                         'https://client.example.test/cb', '2099-01-01')
                """))

            command.upgrade(config, pkce.revision)
            command.upgrade(config, pkce.revision)
            command.upgrade(config, proof.revision)
            command.upgrade(config, proof.revision)
            # Current ORM writes also need the public-client columns. Apply only
            # that revision's body: the revisions between carry their own gates.
            public_clients = importlib.import_module(
                "packages.core.migrations.versions.20261020_10_oauth_public_clients")
            metadata = importlib.import_module(
                "packages.core.migrations.versions.20261024_10_oauth_metadata_documents_and_access_tokens")
            with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
                public_clients.upgrade()
                public_clients.upgrade()
                metadata.upgrade()
                metadata.upgrade()

            with engine.begin() as conn:
                assert conn.execute(sa.text("SELECT require_pkce FROM oauth_client_apps WHERE id='legacy'")).scalar_one() is None
                assert conn.execute(sa.text("SELECT code_challenge FROM oauth_authorization_codes WHERE id='old-code'")).scalar_one() is None
                assert conn.execute(sa.text("SELECT require_verified_email FROM oauth_client_apps WHERE id='legacy'")).scalar_one() is False
                with Session(bind=conn) as session:
                    client = OAuthClientApp(
                        client_id="required", client_secret_hash="unused", name="Required",
                        redirect_uris=[], require_pkce=True,
                    )
                    code = OAuthAuthorizationCode(
                        code="new", client_id="required", user_id="user", entity_id="entity",
                        redirect_uri="https://client.example.test/cb", expires_at=datetime.now(timezone.utc),
                        code_challenge="a" * 43, code_challenge_method="S256",
                    )
                    session.add_all([client, code])
                    session.flush()
                    session.expire_all()
                    assert session.get(OAuthClientApp, client.id).require_pkce is True
                    assert session.get(OAuthAuthorizationCode, code.id).code_challenge_method == "S256"
                    url_client = OAuthClientApp(
                        client_id="https://client.example.test/metadata/" + "x" * 160,
                        name="Metadata client", redirect_uris=[], require_pkce=True,
                        metadata_fetched_at=datetime.now(timezone.utc),
                    )
                    pat = PersonalAccessToken(
                        user_id="user", entity_id="entity", name="Read-only automation",
                        audience="manor-mcp", scope="mcp:read", token_hash="a" * 64,
                        token_prefix="prefix", token_version=0,
                    )
                    session.add_all([url_client, pat])
                    session.flush()
                    session.expire_all()
                    assert len(session.get(OAuthClientApp, url_client.id).client_id) > 64
                    assert session.get(OAuthClientApp, url_client.id).metadata_fetched_at is not None
                    assert session.get(PersonalAccessToken, pat.id).token_hash == "a" * 64

            with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
                metadata.downgrade()

            command.downgrade(config, pkce.revision)
            command.downgrade(config, "20260917_06")
            with engine.connect() as conn:
                assert conn.execute(sa.text("SELECT name FROM oauth_client_apps WHERE id='legacy'")).scalar_one() == "Existing"
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
