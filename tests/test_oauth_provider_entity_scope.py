"""OAuth code redemption against disposable SQLite, never the configured database.

Exercise real client-secret checks, actor resolution, JWT minting, and competing
transactions. Only copied DDL types are adapted; shared ORM metadata stays intact.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import JSON, MetaData, delete, select, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value
from starlette.requests import Request

from apps.api.deps import get_current_user
from packages.core.models.oauth_provider import OAuthAuthorizationCode, OAuthClientApp, OAuthRefreshToken
from packages.core.models.staff import Department, Staff, StaffRole
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services import auth_service
from packages.core.services import oauth_provider_service as svc

pytestmark = pytest.mark.integration

PRIMARY_ENTITY = "oauth-primary"
SELECTED_ENTITY = "oauth-selected"
USER_ID = "oauth-user"
CLIENT_ID = "oauth-pms"
CLIENT_SECRET = "isolated-oauth-test-secret"
REDIRECT_URI = "https://pms.example.test/auth/callback"


@pytest.fixture(scope="module")
def client_secret_hash():
    return svc.hash_client_secret(CLIENT_SECRET)


@pytest.fixture
async def oauth_db(tmp_path, monkeypatch, client_secret_hash):
    monkeypatch.setattr(auth_service.settings, "JWT_SECRET_KEY", "isolated-oauth-test-signing-key-1234567890")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth.sqlite'}")
    metadata = MetaData()
    for model in (Entity, User, UserMembership, Department, StaffRole, Staff,
                  OAuthClientApp, OAuthAuthorizationCode, OAuthRefreshToken):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, (JSONB, ARRAY)):
                column.type = JSON()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        async with sessions.begin() as db:
            db.add_all([
                Entity(id=PRIMARY_ENTITY, name="Primary"),
                Entity(id=SELECTED_ENTITY, name="Selected"),
                User(id=USER_ID, entity_id=PRIMARY_ENTITY, email="oauth@example.test", password_hash="unused",
                     role="owner", status="active", token_version=3),
                UserMembership(user_id=USER_ID, entity_id=PRIMARY_ENTITY, role="owner", status="active"),
                UserMembership(user_id=USER_ID, entity_id=SELECTED_ENTITY, role="member", status="active"),
                OAuthClientApp(client_id=CLIENT_ID, client_secret_hash=client_secret_hash, name="PMS",
                               redirect_uris=[REDIRECT_URI], allowed_scopes=["openid", "profile", "email"],
                               restricted_entity_id=SELECTED_ENTITY, active=True, access_token_ttl_minutes=1440),
            ])
        # Match get_current_user's request-local selected-team overlay. This
        # must not rewrite the user's saved primary organization or owner role.
        async with sessions.begin() as db:
            user = await db.get(User, USER_ID)
            set_committed_value(user, "entity_id", SELECTED_ENTITY)
            set_committed_value(user, "role", "member")
            client = await svc.get_client_by_id(db, CLIENT_ID)
            code = await svc.create_authorization_code(
                db, client=client, user=user, redirect_uri=REDIRECT_URI,
                scope=["openid", "profile", "email"], state="client-csrf-state",
            )
        yield sessions, code.code
    finally:
        await engine.dispose()


async def _redeem(oauth_db, **overrides):
    sessions, code = oauth_db
    async with sessions.begin() as db:
        return await svc.exchange_code_for_token(db, **{
            "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI, **overrides,
        })


@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_redemption_uses_selected_entity_and_current_member_role(oauth_db, role):
    sessions, code = oauth_db
    async with sessions.begin() as db:
        await db.execute(update(UserMembership).where(
            UserMembership.entity_id == SELECTED_ENTITY,
        ).values(role=role))
    result = await _redeem(oauth_db)
    claims = auth_service.decode_token(result["access_token"])
    assert claims["entity_id"] == result["user"]["entity_id"] == SELECTED_ENTITY
    assert claims["role"] == result["user"]["role"] == role
    assert claims["sub"] == USER_ID
    assert claims["token_version"] == 3
    assert result["expires_in"] == 3600
    async with sessions() as db:
        user = await db.get(User, USER_ID)
        assert (user.entity_id, user.role) == (PRIMARY_ENTITY, "owner")
        row = await db.scalar(select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code))
        assert row.entity_id == SELECTED_ENTITY and row.redeemed_at is not None
        # The issued token must also pass the first-party admission boundary.
        admitted = await get_current_user(
            Request({"type": "http", "method": "GET", "path": "/api/v1/auth/me", "headers": []}),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=result["access_token"]), db,
        )
        assert (admitted.entity_id, admitted.role) == (SELECTED_ENTITY, role)


@pytest.mark.parametrize("change", [
    "membership_inactive", "membership_deleted", "membership_missing",
    "user_inactive", "user_deleted", "entity_deleted", "entity_suspended",
    "staff_inactive", "staff_deleted", "client_restricted_elsewhere", "callback_removed",
])
async def test_redemption_rechecks_current_entity_admission(oauth_db, change):
    sessions, code = oauth_db
    now = datetime.now(timezone.utc)
    async with sessions.begin() as db:
        if change.startswith("membership_"):
            predicate = UserMembership.entity_id == SELECTED_ENTITY
            if change == "membership_missing":
                await db.execute(delete(UserMembership).where(predicate))
            else:
                values = {"status": "inactive"} if change == "membership_inactive" else {"deleted_at": now}
                await db.execute(update(UserMembership).where(predicate).values(**values))
        elif change.startswith("user_"):
            values = {"status": "inactive"} if change == "user_inactive" else {"deleted_at": now}
            await db.execute(update(User).where(User.id == USER_ID).values(**values))
        elif change.startswith("entity_"):
            values = {"deleted_at": now} if change == "entity_deleted" else {
                "settings": {"platform_suspended_at": now.isoformat()},
            }
            await db.execute(update(Entity).where(Entity.id == SELECTED_ENTITY).values(**values))
        elif change.startswith("staff_"):
            db.add(Staff(entity_id=SELECTED_ENTITY, user_id=USER_ID, name="Revoked staff",
                         status="inactive" if change == "staff_inactive" else "active",
                         deleted_at=now if change == "staff_deleted" else None))
        else:
            values = {"restricted_entity_id": PRIMARY_ENTITY} if change == "client_restricted_elsewhere" else {
                "redirect_uris": [],
            }
            await db.execute(update(OAuthClientApp).where(OAuthClientApp.client_id == CLIENT_ID).values(**values))
    with pytest.raises(ValueError, match="^invalid_grant$"):
        await _redeem(oauth_db)
    async with sessions() as db:
        row = await db.scalar(select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code))
        assert row.redeemed_at is None


async def test_redemption_preserves_client_and_redirect_binding(oauth_db):
    sessions, _ = oauth_db
    async with sessions.begin() as db:
        original = await svc.get_client_by_id(db, CLIENT_ID)
        db.add(OAuthClientApp(client_id="other-pms", client_secret_hash=original.client_secret_hash,
                             name="Other", redirect_uris=[REDIRECT_URI], active=True))
    with pytest.raises(ValueError, match="^invalid_grant$"):
        await _redeem(oauth_db, client_id="other-pms")
    with pytest.raises(ValueError, match="^invalid_grant$"):
        await _redeem(oauth_db, redirect_uri="https://pms.example.test/other")
    with pytest.raises(ValueError, match="^invalid_client$"):
        await _redeem(oauth_db, client_secret="wrong")
    await _redeem(oauth_db)
    with pytest.raises(ValueError, match="^invalid_grant$"):
        await _redeem(oauth_db)


async def test_expired_code_cannot_be_redeemed(oauth_db):
    sessions, code = oauth_db
    async with sessions.begin() as db:
        await db.execute(update(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code == code).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
    with pytest.raises(ValueError, match="^invalid_grant$"):
        await _redeem(oauth_db)


async def test_concurrent_redemptions_mint_exactly_one_token(oauth_db, monkeypatch):
    sessions, code = oauth_db
    both_read = asyncio.Event()
    reads = 0
    minted = []
    original_mint = svc.create_access_token

    def mint(*args, **kwargs):
        token = original_mint(*args, **kwargs)
        minted.append(token)
        return token

    monkeypatch.setattr(svc, "create_access_token", mint)

    async def exchange():
        async with sessions.begin() as db:
            original_execute = db.execute

            async def execute(statement, *args, **kwargs):
                nonlocal reads
                result = await original_execute(statement, *args, **kwargs)
                if statement.is_select and any(
                    column.get("entity") is OAuthAuthorizationCode
                    for column in statement.column_descriptions
                ):
                    # Both independent connections must read the unused code
                    # before either request can proceed to redemption.
                    reads += 1
                    if reads == 2:
                        both_read.set()
                    await asyncio.wait_for(both_read.wait(), timeout=5)
                return result

            monkeypatch.setattr(db, "execute", execute)
            return await svc.exchange_code_for_token(
                db, code=code, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI,
            )

    results = await asyncio.wait_for(asyncio.gather(exchange(), exchange(), return_exceptions=True), timeout=10)
    assert sum(isinstance(result, dict) for result in results) == 1, results
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 1 and isinstance(failures[0], ValueError)
    assert str(failures[0]) == "invalid_grant"
    assert len(minted) == 1
    with pytest.raises(ValueError, match="^invalid_grant$"):
        await _redeem(oauth_db)
