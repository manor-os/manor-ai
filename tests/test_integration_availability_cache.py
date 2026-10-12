"""Discovery's integration availability is cached per actor until a relevant write commits.

resolve_usable_mcp_providers rebuilt it from ~10 queries on every search_tools
call, each queued behind the chat pod's single database permit. A cached
answer must never outlive a committed change to the rows it came from.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from auth_helpers import register_user_and_get_token
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.user import OAuthAccount, User
from packages.core.services import agent_permission_service as aps
from packages.core.services import integration_availability_cache as availability

KEYS = ("gmail", "slack")


async def _register_owner(client: AsyncClient, username: str) -> tuple[str, str]:
    resp = await register_user_and_get_token(client, json={
        "username": username, "email": f"{username}@test.com",
        "password": "TestPassword123!", "entity_name": f"{username} Corp",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    return data["user_id"], me.json()["entity_id"]


@pytest.fixture(autouse=True)
async def redis_connected():
    """Connect first: search-path cache calls budget 0.1s, which a cold connect can exceed."""
    from packages.core.cache import _get_redis

    assert await _get_redis() is not None


@pytest.fixture
def registry_loads(monkeypatch):
    """Count authoritative registry loads (one per uncached decision batch)."""
    calls: list = []
    real = aps.load_runtime_integration_registry

    async def counted(db, **kwargs):
        calls.append(kwargs.get("provider_keys"))
        return await real(db, **kwargs)

    monkeypatch.setattr(aps, "load_runtime_integration_registry", counted)
    return calls


def _session():
    import packages.core.database as dbmod

    return dbmod.async_session()


async def _usable(user_id: str, entity_id: str) -> frozenset[str]:
    async with _session() as db:
        return await aps.resolve_usable_mcp_providers(
            db, user_id=user_id, entity_id=entity_id, provider_keys=KEYS,
        )


def _gmail(user_id: str) -> OAuthAccount:
    return OAuthAccount(
        id=generate_ulid(), user_id=user_id, provider="gmail",
        provider_user_id=f"{user_id}@example.com", access_token="token",
    )


@pytest.mark.asyncio
async def test_snapshot_is_reused_until_an_account_or_integration_commits(client, registry_loads):
    user_id, entity_id = await _register_owner(client, "availcache_hit")
    assert "gmail" not in await _usable(user_id, entity_id)
    assert len(registry_loads) == 1
    assert "gmail" not in await _usable(user_id, entity_id)
    assert len(registry_loads) == 1

    async with _session() as db:
        db.add(_gmail(user_id))
        await db.commit()
    assert "gmail" in await _usable(user_id, entity_id)
    assert len(registry_loads) == 2

    # An entity-level row reaches every member's snapshot.
    async with _session() as db:
        db.add(Integration(
            id=generate_ulid(), entity_id=entity_id, provider="slack",
            status="active", credentials={"access_token": "xoxb"},
        ))
        await db.commit()
    await _usable(user_id, entity_id)
    assert len(registry_loads) == 3


@pytest.mark.asyncio
async def test_unrelated_and_rolled_back_writes_keep_the_snapshot(client, registry_loads):
    user_id, entity_id = await _register_owner(client, "availcache_keep")
    await _usable(user_id, entity_id)
    assert len(registry_loads) == 1

    async with _session() as db:
        user = await db.get(User, user_id)
        user.last_login_at = datetime.now(UTC)
        await db.flush()
        await db.execute(update(User).where(User.id == user_id).values(last_login_ip="127.0.0.1"))
        await db.commit()
    async with _session() as db:
        db.add(_gmail(user_id))
        await db.flush()
        await db.rollback()
    assert "gmail" not in await _usable(user_id, entity_id)
    assert len(registry_loads) == 1

    # Account defaults live in preferences, written in bulk.
    async with _session() as db:
        await db.execute(update(User).where(User.id == user_id).values(preferences={"x": 1}))
        await db.commit()
    await _usable(user_id, entity_id)
    assert len(registry_loads) == 2


@pytest.mark.asyncio
async def test_fail_open_guesses_are_never_cached(client, monkeypatch, registry_loads):
    user_id, entity_id = await _register_owner(client, "availcache_failopen")
    counted = aps.load_runtime_integration_registry

    async def broken(db, **kwargs):
        raise RuntimeError("registry outage")

    monkeypatch.setattr(aps, "load_runtime_integration_registry", broken)
    assert {"gmail", "slack"} <= await _usable(user_id, entity_id)
    monkeypatch.setattr(aps, "load_runtime_integration_registry", counted)
    assert "gmail" not in await _usable(user_id, entity_id)
    assert len(registry_loads) == 1


@pytest.mark.asyncio
async def test_an_owner_change_reaches_entities_using_their_shared_accounts():
    owner, entity_id, member = generate_ulid(), generate_ulid(), generate_ulid()
    assert await availability.register_dependents([owner], entity_id)
    before = await availability.current_versions(entity_id, member)

    # The owner's own account defaults are not shared with anyone.
    await availability.bump(users_self=[owner])
    assert await availability.current_versions(entity_id, member) == before

    await availability.bump(users=[owner])
    after = await availability.current_versions(entity_id, member)
    assert after[1] != before[1] and after[2] == before[2]


def test_bulk_writes_are_scoped_by_their_where_clause():
    from sqlalchemy import delete, insert

    from packages.core.models.document import Document
    from packages.core.models.staff import StaffRole

    scoped = availability._statement_changes(
        update(User).where(User.id == "u1").values(preferences={}))
    assert scoped["users_self"] == {"u1"} and not scoped["global"]
    assert not availability._statement_changes(
        update(User).where(User.id == "u1").values(last_login_ip="x"))["users"]
    assert availability._statement_changes(
        delete(OAuthAccount).where(OAuthAccount.user_id == "u2"))["users"] == {"u2"}
    assert availability._statement_changes(
        update(StaffRole).where(StaffRole.entity_id == "e1").values(permissions=[]))["entities"] == {"e1"}
    # Rows the statement does not pin down invalidate everything.
    assert availability._statement_changes(update(User).values(status="disabled"))["global"]
    assert availability._statement_changes(
        update(OAuthAccount).where(OAuthAccount.user_id.in_(["a", "b"])).values(access_token=None))["global"]
    assert availability._statement_changes(insert(OAuthAccount).values(user_id="u3"))["global"]
    assert availability._statement_changes(update(Document).values(title="x")) is None


def test_invalidation_runs_after_the_database_permit_is_released():
    """Every session invalidates; the Redis I/O never holds an admission permit."""
    import packages.core.database  # noqa: F401  (installs the hooks)
    from sqlalchemy.orm import Session

    from packages.core.database_runtime import DatabaseSession

    assert "_after_end" in [listener.__name__ for listener in Session().dispatch.after_transaction_end]
    listeners = [listener.__name__ for listener in DatabaseSession().dispatch.after_transaction_end]
    assert listeners.index("_release_admission") < listeners.index("_after_end")
