"""Durable email proof and OAuth projection against disposable SQLite only."""

from datetime import datetime, timezone
import asyncio
import importlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import JSON, MetaData, create_engine, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects import postgresql

from packages.core.models.user import User, OAuthAccount
from packages.core.models.oauth_provider import OAuthClientApp
from packages.core.services import oauth_provider_service as oauth
from packages.core.services import authenticated_email_verification as proof
from packages.core.services.auth_service import create_access_token
from packages.core.services.auth_service import get_or_create_oauth_user
from packages.core.services.user_email_proof import email_is_verified, mark_email_verified, provider_verified_claim
from apps.api.routers import auth as routes
from tests.test_oauth_provider_entity_scope import (
    oauth_db as oauth_db,
    client_secret_hash as client_secret_hash,
    CLIENT_ID,
    CLIENT_SECRET,
    REDIRECT_URI,
    USER_ID,
    PRIMARY_ENTITY,
)

pytestmark = pytest.mark.oss_smoke


async def test_active_account_is_not_email_verification_evidence(oauth_db):
    sessions, code = oauth_db
    async with sessions.begin() as db:
        result = await oauth.exchange_code_for_token(
            db,
            code=code,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
        )
    assert result["user"]["email_verified"] is False


async def test_email_change_clears_proof_even_when_changed_back(oauth_db):
    sessions, _ = oauth_db
    async with sessions.begin() as db:
        user = await db.get(User, USER_ID)
        user.verified_email = user.email
        user.email_verified_at = datetime.now(timezone.utc)
        original = user.email
        user.email = "changed@example.test"
        user.email = original
        assert user.verified_email is None
        assert user.email_verified_at is None


def test_client_verification_policy_is_explicit_and_default_off():
    column = OAuthClientApp.__table__.columns["require_verified_email"]
    assert column.nullable is False
    assert str(column.server_default.arg) == "false"


@pytest.fixture
async def proof_store(tmp_path, monkeypatch):
    """Real Lua on one disposable Unix-socket-only Redis; no configured services."""
    binary = shutil.which("redis-server")
    assert binary, "A local redis-server is required for this isolated security regression"
    # Use the platform temp root, not pytest's deeply nested paths: Darwin's
    # socket path limit is 104 bytes, and Linux has no /private/tmp directory.
    directory = tempfile.TemporaryDirectory(prefix="proof-")
    socket = directory.name + "/redis.sock"
    process = subprocess.Popen(
        [
            binary,
            "--port",
            "0",
            "--unixsocket",
            str(socket),
            "--unixsocketperm",
            "700",
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    store = Redis(unix_socket_path=str(socket), decode_responses=True, socket_timeout=0.2, socket_connect_timeout=0.2)
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("Isolated Redis startup: " + process.stdout.read().decode()[-1200:])
            try:
                if await store.ping():
                    break
            except Exception:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("Isolated Redis did not start")

        async def get_store():
            return store

        monkeypatch.setattr(proof, "_get_redis", get_store)
        yield store
    finally:
        await store.aclose()
        process.terminate()
        process.wait(timeout=5)
        directory.cleanup()


async def test_proof_store_uses_platform_temp_directory_and_unix_socket(proof_store):
    options = proof_store.connection_pool.connection_kwargs
    socket_path = Path(options["path"])
    assert socket_path.parent.parent.resolve() == Path(tempfile.gettempdir()).resolve()
    assert len(str(socket_path).encode()) < 104
    assert "host" not in options and "port" not in options
    assert await proof_store.ping()


async def test_proof_store_missing_host_binary_fails_instead_of_skipping(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    fixture = proof_store.__wrapped__(tmp_path, monkeypatch)
    with pytest.raises(AssertionError, match="local redis-server is required"):
        await fixture.__anext__()


@pytest.fixture
async def proof_http(oauth_db, proof_store, monkeypatch):
    sessions, _ = oauth_db
    deliveries = []

    async def mail(email, code):
        deliveries.append((email, code))
        return True

    monkeypatch.setattr(routes, "send_verification_email", mail)
    app = FastAPI()
    app.include_router(routes.router)

    async def database():
        async with sessions() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[routes.get_db] = database
    bearer = create_access_token(USER_ID, PRIMARY_ENTITY, "owner", token_version=3)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {bearer}"}
    ) as http:
        yield http, sessions, deliveries, proof_store


BASE = "/api/v1/auth/me/email-verification"


async def test_authenticated_proof_real_http_sql_redis_is_one_time_and_not_login(proof_http):
    http, sessions, deliveries, _ = proof_http
    before = (await http.get(BASE)).json()
    assert before == {"email": "oauth@example.test", "email_verified": False}
    assert (await http.post(BASE + "/send")).json()["sent"] is True
    response = await http.post(BASE + "/confirm", json={"code": deliveries[-1][1]})
    assert response.status_code == 200
    assert response.json() == {"email": before["email"], "email_verified": True}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert (await http.post(BASE + "/confirm", json={"code": deliveries[-1][1]})).status_code == 400
    async with sessions() as db:
        user = await db.get(User, USER_ID)
        assert (user.status, user.token_version, user.totp_enabled) == ("active", 3, False)
        assert email_is_verified(user)
    denied = await http.post(BASE + "/send", headers={"Authorization": "Bearer invalid"})
    assert denied.status_code == 401
    assert len(deliveries) == 1


async def test_concurrent_confirm_consumes_once(proof_http):
    http, sessions, deliveries, _ = proof_http
    await http.post(BASE + "/send")
    responses = await asyncio.gather(
        *[http.post(BASE + "/confirm", json={"code": deliveries[-1][1]}) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [200, 400]
    async with sessions() as db:
        assert email_is_verified(await db.get(User, USER_ID))


@pytest.mark.parametrize("change", ["email", "password", "version", "pending", "deleted", "expired"])
async def test_challenge_is_bound_to_current_identity_and_expiry(proof_http, monkeypatch, change):
    http, sessions, deliveries, _ = proof_http
    await http.post(BASE + "/send")
    async with sessions.begin() as db:
        user = await db.get(User, USER_ID)
        if change == "email":
            user.email = "new@example.test"
        elif change == "password":
            user.password_hash = "changed-password-hash"
        elif change == "version":
            user.token_version += 1
        elif change == "pending":
            user.status = "pending"
        elif change == "deleted":
            user.deleted_at = datetime.now(timezone.utc)
    if change == "expired":
        now = proof.time.time()
        monkeypatch.setattr(proof.time, "time", lambda: now + proof.TTL + 1)
    response = await http.post(BASE + "/confirm", json={"code": deliveries[-1][1]})
    assert response.status_code in {400, 401, 403}
    async with sessions() as db:
        assert not email_is_verified(await db.get(User, USER_ID))


async def test_resend_does_not_reset_verification_budget(proof_http):
    http, sessions, deliveries, _ = proof_http
    await http.post(BASE + "/send")
    for _ in range(5):
        assert (await http.post(BASE + "/confirm", json={"code": "WRONG234"})).status_code == 400
    await http.post(BASE + "/send")
    response = await http.post(BASE + "/confirm", json={"code": deliveries[-1][1]})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    async with sessions() as db:
        assert not email_is_verified(await db.get(User, USER_ID))


async def test_atomic_multibucket_limits_and_stable_ttl_are_separate_from_login(proof_store):
    async def charge():
        try:
            await proof._store_and_budget("synthetic@example.test", "test-ip", "send")
            return True
        except proof.EmailProofError as exc:
            assert exc.status == 429
            return False

    assert sum(await asyncio.gather(*[charge() for _ in range(12)])) == 3
    keys = await proof_store.keys("manor:{email-proof}:send:*")
    assert len(keys) == 2
    assert [int(await proof_store.get(key)) for key in keys] == [12, 12]
    for key in keys:
        await proof_store.expire(key, 300)
    assert not await charge()
    assert all([0 < await proof_store.ttl(key) <= 300 for key in keys])
    assert not await proof_store.keys("auth:fail:*")
    assert await proof._store_and_budget("synthetic@example.test", "test-ip", "confirm") is proof_store


@pytest.mark.parametrize("failure", ["budget", "write", "read", "consume", "mail"])
async def test_storage_and_mail_fail_closed_without_sensitive_errors(proof_http, monkeypatch, failure):
    http, sessions, deliveries, store = proof_http

    async def broken(*args, **kwargs):
        raise RuntimeError("sensitive-provider-detail-must-not-leak")

    if failure in {"read", "consume"}:
        await http.post(BASE + "/send")
    if failure == "budget":
        monkeypatch.setattr(store, "eval", broken)
    elif failure == "write":
        monkeypatch.setattr(store, "setex", broken)
    elif failure == "read":
        monkeypatch.setattr(store, "get", broken)
    elif failure == "consume":
        original = store.eval

        async def eval(script, *args):
            if script == proof._CONSUME:
                return await broken()
            return await original(script, *args)

        monkeypatch.setattr(store, "eval", eval)
    else:
        monkeypatch.setattr(routes, "send_verification_email", broken)
    response = await http.post(
        BASE + ("/confirm" if deliveries else "/send"), json={"code": deliveries[-1][1]} if deliveries else None
    )
    assert response.status_code == 503
    assert "sensitive-provider" not in response.text
    async with sessions() as db:
        assert not email_is_verified(await db.get(User, USER_ID))


@pytest.mark.parametrize("verified", [False, True])
async def test_oauth_policy_opt_in_enforces_consent_and_exchange(oauth_db, verified):
    sessions, legacy_code = oauth_db
    async with sessions.begin() as db:
        client = await db.scalar(select(OAuthClientApp))
        client.require_verified_email = True
        user = await db.get(User, USER_ID)
        if verified:
            mark_email_verified(user, user.email)
        # Use primary team for this direct consent test.
        client.restricted_entity_id = None
        kwargs = dict(client=client, user=user, redirect_uri=REDIRECT_URI, scope=["email"], state="same-state")
        if verified:
            await oauth.create_authorization_code(db, **kwargs)
        else:
            with pytest.raises(PermissionError, match="email_verification_required"):
                await oauth.create_authorization_code(db, **kwargs)
    async with sessions.begin() as db:
        kwargs = dict(code=legacy_code, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI)
        if verified:
            assert (await oauth.exchange_code_for_token(db, **kwargs))["user"]["email_verified"] is True
        else:
            with pytest.raises(ValueError, match="email_verification_required"):
                await oauth.exchange_code_for_token(db, **kwargs)


async def test_sql_email_change_cannot_project_previous_email_proof(oauth_db):
    sessions, code = oauth_db
    async with sessions.begin() as db:
        user = await db.get(User, USER_ID)
        mark_email_verified(user, user.email)
    async with sessions.begin() as db:
        await db.execute(update(User).values(email="sql-changed@example.test"))
    async with sessions.begin() as db:
        result = await oauth.exchange_code_for_token(
            db, code=code, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI
        )
        assert result["user"]["email_verified"] is False


@pytest.mark.parametrize(
    "claim,expected",
    [(True, True), ("true", True), (None, False), (False, False), (1, False), ("false", False), ("", False)],
)
def test_only_explicit_provider_verification_claims(claim, expected):
    assert provider_verified_claim(claim) is expected


def test_migration_preserves_existing_accounts_without_inventing_proof():
    migration = importlib.import_module("packages.core.migrations.versions.20260917_08_user_email_proof")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT, status TEXT)"))
        connection.execute(text("INSERT INTO users VALUES ('old', 'old@example.test', 'active')"))
        connection.execute(text("CREATE TABLE oauth_client_apps (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO oauth_client_apps VALUES ('old-client')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        assert connection.execute(text("SELECT status, verified_email, email_verified_at FROM users")).one() == (
            "active",
            None,
            None,
        )
        assert connection.execute(text("SELECT require_verified_email FROM oauth_client_apps")).scalar() == 0
    engine.dispose()


@pytest.mark.parametrize("provider", ["google", "apple"])
@pytest.mark.parametrize("claim", [False, None])
async def test_unverified_unknown_provider_subject_cannot_inherit_existing_verified_identity(oauth_db, provider, claim):
    sessions, _ = oauth_db
    metadata = MetaData()
    table = OAuthAccount.__table__.to_metadata(metadata)
    for column in table.columns:
        if isinstance(column.type, JSONB):
            column.type = JSON()
    async with sessions.kw["bind"].begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with sessions.begin() as db:
        user = await db.get(User, USER_ID)
        mark_email_verified(user, user.email)
    async with sessions.begin() as db:
        with pytest.raises(ValueError, match="provider_email_verification_required"):
            await get_or_create_oauth_user(
                db,
                provider=provider,
                provider_user_id="untrusted-new-subject",
                email="oauth@example.test",
                email_verified=claim,
            )
        assert not (await db.scalars(select(OAuthAccount))).all()
        assert email_is_verified(await db.get(User, USER_ID))


@pytest.mark.parametrize("known_subject", [False, True])
async def test_verified_first_link_and_known_subject_keep_exact_identity(oauth_db, monkeypatch, known_subject):
    sessions, _ = oauth_db
    metadata = MetaData()
    table = OAuthAccount.__table__.to_metadata(metadata)
    for column in table.columns:
        if isinstance(column.type, JSONB):
            column.type = JSON()
    async with sessions.kw["bind"].begin() as connection:
        await connection.run_sync(metadata.create_all)
    monkeypatch.setattr(
        "packages.core.services.oauth_account_credentials.store_oauth_account_tokens", lambda *a, **k: None
    )
    if known_subject:
        async with sessions.begin() as db:
            db.add(OAuthAccount(user_id=USER_ID, provider="google", provider_user_id="stable-subject"))
            user = await db.get(User, USER_ID)
            mark_email_verified(user, user.email)
    async with sessions.begin() as db:
        user, created = await get_or_create_oauth_user(
            db,
            provider="google",
            provider_user_id="stable-subject",
            email="different-claim@example.test" if known_subject else "oauth@example.test",
            email_verified=not known_subject,
        )
        assert (user.id, user.email, created) == (USER_ID, "oauth@example.test", False)
        assert email_is_verified(user)


async def test_bootstrap_omission_preserves_verified_email_requirement(oauth_db, monkeypatch):
    sessions, _ = oauth_db
    monkeypatch.setattr(oauth, "_discover_client_slugs", lambda: ["PROOF_TEST"])
    for suffix, value in {"SECRET": CLIENT_SECRET, "CLIENT_ID": CLIENT_ID, "REDIRECT_URIS": REDIRECT_URI}.items():
        monkeypatch.setenv(f"MANOR_OAUTH_CLIENT_PROOF_TEST_{suffix}", value)
    monkeypatch.delenv("MANOR_OAUTH_CLIENT_PROOF_TEST_REQUIRE_VERIFIED_EMAIL", raising=False)
    async with sessions.begin() as db:
        await db.execute(update(OAuthClientApp).values(require_verified_email=True))
    async with sessions.begin() as db:
        await oauth.seed_clients_from_env(db)
    async with sessions() as db:
        assert (await db.scalar(select(OAuthClientApp))).require_verified_email is True



def test_bootstrap_verified_email_flag_is_explicit_and_does_not_change_other_clients(monkeypatch):
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_TEST_SECRET", "synthetic-only")
    monkeypatch.delenv("MANOR_OAUTH_CLIENT_TEST_REQUIRE_VERIFIED_EMAIL", raising=False)
    assert oauth._read_client_config("TEST")["require_verified_email"] is None
    for value, expected in [("true", True), ("false", False)]:
        monkeypatch.setenv("MANOR_OAUTH_CLIENT_TEST_REQUIRE_VERIFIED_EMAIL", value)
        assert oauth._read_client_config("TEST")["require_verified_email"] is expected
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_TEST_REQUIRE_VERIFIED_EMAIL", "invalid")
    with pytest.raises(ValueError, match="invalid_require_verified_email_configuration"):
        oauth._read_client_config("TEST")


async def test_exchange_refreshes_stale_identity_after_concurrent_email_change(oauth_db, monkeypatch):
    """Two real SQLite sessions reproduce a stale ORM identity before lock acquisition.

    Also assert the actual statement uses PostgreSQL FOR UPDATE. SQLite cannot
    prove PostgreSQL lock-wait behavior and this test does not claim to do so.
    """
    sessions, code = oauth_db
    async with sessions.begin() as db:
        user = await db.get(User, USER_ID)
        mark_email_verified(user, user.email)
        await db.execute(update(OAuthClientApp).values(require_verified_email=True))

    started = asyncio.Event()
    changed = asyncio.Event()
    user_statements = []
    minted = []
    monkeypatch.setattr(oauth, "create_access_token", lambda *a, **k: minted.append(True))

    async def redeem():
        async with sessions.begin() as db:
            retained_identity = await db.get(User, USER_ID)
            assert email_is_verified(retained_identity)
            execute = db.execute

            async def pause_before_identity_select(statement, *args, **kwargs):
                if statement.is_select and any(
                    column.get("entity") is User for column in statement.column_descriptions
                ):
                    user_statements.append(statement)
                    started.set()
                    await asyncio.wait_for(changed.wait(), 5)
                return await execute(statement, *args, **kwargs)

            monkeypatch.setattr(db, "execute", pause_before_identity_select)
            with pytest.raises(ValueError, match="email_verification_required"):
                await oauth.exchange_code_for_token(
                    db, code=code, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                    redirect_uri=REDIRECT_URI,
                )
            assert retained_identity.email == "changed-before-lock@example.test"
            assert not email_is_verified(retained_identity)

    async def change_email():
        await asyncio.wait_for(started.wait(), 5)
        async with sessions.begin() as db:
            user = await db.get(User, USER_ID)
            user.email = "changed-before-lock@example.test"
        changed.set()

    await asyncio.wait_for(asyncio.gather(redeem(), change_email()), 10)
    assert not minted
    assert len(user_statements) == 1
    assert "FOR UPDATE" in str(user_statements[0].compile(dialect=postgresql.dialect()))
    assert user_statements[0].get_execution_options()["populate_existing"] is True
