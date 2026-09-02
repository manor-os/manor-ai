"""Page-view dwell time + path normalisation in user_session_service."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import ProgrammingError

import packages.core.database as db_module
from packages.core.services import geo_ip, user_session_service
from packages.core.models.user_session import (
    UserPageViewLog,
    UserSessionLease,
    UserSessionLog,
)
from packages.core.services.user_session_service import (
    _lock_user_session_scope,
    _normalise_path,
    cleanup_expired_user_session_leases,
    close_user_session_compat,
    close_user_session,
    enrich_user_session_geo,
    start_user_session,
    start_user_session_compat,
    touch_user_session,
)


def test_normalise_path_collapses_ids():
    # ULIDs and UUIDs become :id; words stay; numeric ids become :id.
    assert (
        _normalise_path("/workspaces/01JXR12CZN3QV7TXKMHWB8FYAD/tasks/01JXR12CZN3QV7TXKMHWB8FYAE")
        == "/workspaces/:id/tasks/:id"
    )
    assert _normalise_path("/tasks/12345") == "/tasks/:id"
    assert _normalise_path("/dashboard") == "/dashboard"
    assert _normalise_path("/workspaces/abc/tasks?tab=open#x") == "/workspaces/abc/tasks"
    assert _normalise_path("") is None
    assert _normalise_path(None) is None


def test_user_session_lease_has_expiration_first_index():
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in UserSessionLease.__table__.indexes
    }

    assert indexes["ix_user_session_lease_expires"] == ("expires_at",)


@pytest.mark.asyncio
async def test_lease_compat_paths_do_not_fallback_after_schema_errors(monkeypatch):
    db = AsyncMock()
    schema_error = ProgrammingError("statement", {}, RuntimeError("missing schema"))
    minimal_start = AsyncMock(return_value="unsafe-session")
    minimal_close = AsyncMock()
    monkeypatch.setattr(
        user_session_service,
        "start_user_session",
        AsyncMock(side_effect=schema_error),
    )
    monkeypatch.setattr(
        user_session_service,
        "_start_user_session_minimal",
        minimal_start,
    )
    monkeypatch.setattr(
        user_session_service,
        "close_user_session",
        AsyncMock(side_effect=schema_error),
    )
    monkeypatch.setattr(
        user_session_service,
        "_close_user_session_minimal",
        minimal_close,
    )

    with pytest.raises(ProgrammingError):
        await start_user_session_compat(
            db,
            entity_id="entity-1",
            user_id="user-1",
            lease_id="lease-1",
        )
    await close_user_session_compat(
        db,
        session_id="session-1",
        entity_id="entity-1",
        user_id="user-1",
        lease_id="lease-1",
    )

    assert db.rollback.await_count == 2
    minimal_start.assert_not_awaited()
    minimal_close.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_start_defers_geo_enrichment_until_after_lifecycle_commit(
    client,
    monkeypatch,
):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-deferred-geo@test.com",
            "password": "pass123",
            "entity_name": "Session Deferred Geo",
        },
    )
    assert response.status_code == 200
    data = response.json()
    lookup_geo = AsyncMock()
    monkeypatch.setattr(geo_ip, "lookup_geo", lookup_geo)

    async with db_module.async_session() as session:
        row = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            ip_address="8.8.8.8",
            lease_id="deferred-geo-lease",
        )
        await session.commit()
        lookup_geo.assert_not_awaited()

        await enrich_user_session_geo(
            session,
            session_id=row.id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            expected_ip_address="8.8.8.8",
            geo={
                "country_code": "US",
                "country": "United States",
                "city": "Mountain View",
                "latitude": 37.386,
                "longitude": -122.084,
            },
        )
        await session.commit()

    lookup_geo.assert_not_awaited()
    async with db_module.async_session() as session:
        stored = await session.get(UserSessionLog, row.id)
        assert stored is not None
        assert stored.country_code == "US"
        assert stored.city == "Mountain View"


@pytest.mark.asyncio
async def test_geo_enrichment_rejects_a_different_connection_ip(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-geo-ip-fence@test.com",
            "password": "pass123",
            "entity_name": "Session Geo IP Fence",
        },
    )
    assert response.status_code == 200
    data = response.json()
    geo = {
        "country_code": "AU",
        "country": "Australia",
        "city": "Sydney",
    }

    async with db_module.async_session() as session:
        row = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            ip_address="8.8.8.8",
            lease_id="geo-ip-fence-lease",
        )
        session_id = row.id
        await session.commit()

        await enrich_user_session_geo(
            session,
            session_id=session_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            expected_ip_address="1.1.1.1",
            geo=geo,
        )
        await session.commit()

    async with db_module.async_session() as session:
        stored = await session.get(UserSessionLog, session_id)
        assert stored is not None
        assert stored.ip_address == "8.8.8.8"
        assert stored.country_code is None



@pytest.mark.asyncio
async def test_touch_user_session_opens_page_segment_on_first_viewing(client):
    """First ``viewing`` after session start opens a tracking segment
    but does NOT yet produce a UserPageViewLog row."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "page-track1@test.com",
            "password": "pass123",
            "entity_name": "PageTrack",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    async with db_module.async_session() as session:
        row = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            ip_address="10.0.0.1",  # private → no geo
        )
        await touch_user_session(
            session,
            session_id=row.id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            viewing="/dashboard",
        )
        await session.commit()

    async with db_module.async_session() as session:
        row = (
            await session.execute(select(UserSessionLog).where(UserSessionLog.user_id == data["user_id"]))
        ).scalar_one()
        assert row.current_path == "/dashboard"
        assert row.current_path_started_at is not None

        page_count = len(
            (await session.execute(select(UserPageViewLog).where(UserPageViewLog.user_id == data["user_id"])))
            .scalars()
            .all()
        )
        assert page_count == 0


@pytest.mark.asyncio
async def test_navigating_to_new_page_flushes_prior_segment(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "page-track2@test.com",
            "password": "pass123",
            "entity_name": "PageTrack2",
        },
    )
    data = resp.json()

    async with db_module.async_session() as session:
        sess = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
        )
        await touch_user_session(
            session,
            session_id=sess.id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            viewing="/dashboard",
        )
        await session.commit()

        # Backdate the open segment so the close-out produces a
        # non-zero duration without a real sleep.
        sess.current_path_started_at = datetime.now(timezone.utc) - timedelta(seconds=42)
        await session.commit()

        await touch_user_session(
            session,
            session_id=sess.id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            viewing="/tasks",
        )
        await session.commit()

    async with db_module.async_session() as session:
        pages = list(
            (await session.execute(select(UserPageViewLog).where(UserPageViewLog.user_id == data["user_id"])))
            .scalars()
            .all()
        )
        assert len(pages) == 1
        assert pages[0].path == "/dashboard"
        assert pages[0].duration_seconds >= 40
        # Open segment is now /tasks.
        row = (
            await session.execute(select(UserSessionLog).where(UserSessionLog.user_id == data["user_id"]))
        ).scalar_one()
        assert row.current_path == "/tasks"


@pytest.mark.asyncio
async def test_repeat_viewing_same_path_does_not_double_flush(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "page-track3@test.com",
            "password": "pass123",
            "entity_name": "PageTrack3",
        },
    )
    data = resp.json()

    async with db_module.async_session() as session:
        sess = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
        )
        # Two heartbeats on the same path — no segment row should be
        # written until the user navigates AWAY.
        for _ in range(3):
            await touch_user_session(
                session,
                session_id=sess.id,
                entity_id=data["entity_id"],
                user_id=data["user_id"],
                viewing="/dashboard",
            )
        await session.commit()

    async with db_module.async_session() as session:
        pages = list(
            (await session.execute(select(UserPageViewLog).where(UserPageViewLog.user_id == data["user_id"])))
            .scalars()
            .all()
        )
        assert pages == []


@pytest.mark.asyncio
async def test_close_user_session_flushes_open_segment(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "page-track4@test.com",
            "password": "pass123",
            "entity_name": "PageTrack4",
        },
    )
    data = resp.json()

    async with db_module.async_session() as session:
        sess = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
        )
        await touch_user_session(
            session,
            session_id=sess.id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            viewing="/dashboard",
        )
        await session.commit()

        sess.current_path_started_at = datetime.now(timezone.utc) - timedelta(seconds=15)
        await session.commit()

        await close_user_session(
            session,
            session_id=sess.id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
        )
        await session.commit()

    async with db_module.async_session() as session:
        pages = list(
            (await session.execute(select(UserPageViewLog).where(UserPageViewLog.user_id == data["user_id"])))
            .scalars()
            .all()
        )
        assert len(pages) == 1
        assert pages[0].path == "/dashboard"
        assert pages[0].duration_seconds >= 14

        row = (
            await session.execute(select(UserSessionLog).where(UserSessionLog.user_id == data["user_id"]))
        ).scalar_one()
        assert row.status == "closed"
        assert row.current_path is None


@pytest.mark.asyncio
async def test_concurrent_process_leases_share_and_close_one_session(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-leases@test.com",
            "password": "pass123",
            "entity_name": "Session Leases",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    async def start(lease_id: str) -> str:
        async with db_module.async_session() as session:
            row = await start_user_session(
                session,
                entity_id=data["entity_id"],
                user_id=data["user_id"],
                lease_id=lease_id,
            )
            await session.commit()
            return row.id

    first_id, second_id = await asyncio.gather(
        start("api-process-1"),
        start("api-process-2"),
    )
    assert first_id == second_id

    async with db_module.async_session() as session:
        leases = (await session.execute(
            select(UserSessionLease).where(
                UserSessionLease.session_id == first_id,
            )
        )).scalars().all()
        assert {lease.id for lease in leases} == {
            "api-process-1",
            "api-process-2",
        }

    async with db_module.async_session() as session:
        await close_user_session(
            session,
            session_id=first_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="api-process-1",
        )
        await session.commit()

    async with db_module.async_session() as session:
        row = await session.get(UserSessionLog, first_id)
        assert row is not None
        assert row.status == "active"
        assert row.ended_at is None

    async with db_module.async_session() as session:
        await close_user_session(
            session,
            session_id=first_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="api-process-2",
        )
        await session.commit()

    async with db_module.async_session() as session:
        row = await session.get(UserSessionLog, first_id)
        assert row is not None
        assert row.status == "closed"
        assert row.ended_at is not None


@pytest.mark.asyncio
async def test_compat_start_does_not_bypass_a_mismatched_lease(client):
    first = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-lease-scope-1@test.com",
            "password": "pass123",
            "entity_name": "Session Lease Scope One",
        },
    )
    second = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-lease-scope-2@test.com",
            "password": "pass123",
            "entity_name": "Session Lease Scope Two",
        },
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_data = first.json()
    second_data = second.json()

    async with db_module.async_session() as session:
        await start_user_session(
            session,
            entity_id=first_data["entity_id"],
            user_id=first_data["user_id"],
            lease_id="scope-bound-lease",
        )
        await session.commit()

    async with db_module.async_session() as session:
        with pytest.raises(ValueError, match="belongs to another scope"):
            await start_user_session_compat(
                session,
                entity_id=second_data["entity_id"],
                user_id=second_data["user_id"],
                lease_id="scope-bound-lease",
            )
        await session.rollback()

    async with db_module.async_session() as session:
        second_scope_session = (await session.execute(
            select(UserSessionLog.id).where(
                UserSessionLog.entity_id == second_data["entity_id"],
                UserSessionLog.user_id == second_data["user_id"],
            )
        )).scalar_one_or_none()
        assert second_scope_session is None


@pytest.mark.asyncio
async def test_expired_lease_does_not_reactivate_an_obsolete_session(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-lease-expired@test.com",
            "password": "pass123",
            "entity_name": "Session Lease Expired",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    async with db_module.async_session() as session:
        old_session = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="expired-api-process",
        )
        old_session_id = old_session.id
        await session.commit()

    stale_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with db_module.async_session() as session:
        await session.execute(
            update(UserSessionLease)
            .where(UserSessionLease.id == "expired-api-process")
            .values(expires_at=stale_seen_at)
        )
        await session.execute(
            update(UserSessionLog)
            .where(UserSessionLog.id == old_session_id)
            .values(last_seen_at=stale_seen_at)
        )
        await session.commit()

    async with db_module.async_session() as session:
        current_session = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="current-api-process",
        )
        current_session_id = current_session.id
        await session.commit()
    assert current_session_id != old_session_id

    async with db_module.async_session() as session:
        touched = await touch_user_session(
            session,
            session_id=old_session_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="expired-api-process",
        )
        await session.commit()
    assert touched is False

    async with db_module.async_session() as session:
        old_row = await session.get(UserSessionLog, old_session_id)
        leases = (await session.execute(
            select(UserSessionLease).where(
                UserSessionLease.entity_id == data["entity_id"],
                UserSessionLease.user_id == data["user_id"],
            )
        )).scalars().all()
        assert old_row is not None
        assert old_row.status == "closed"
        assert old_row.ended_at == stale_seen_at
        assert old_row.last_seen_at == stale_seen_at
        assert {lease.id for lease in leases} == {"current-api-process"}


@pytest.mark.asyncio
async def test_periodic_cleanup_closes_abandoned_leased_session(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-lease-cleanup@test.com",
            "password": "pass123",
            "entity_name": "Session Lease Cleanup",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    async with db_module.async_session() as session:
        row = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="abandoned-api-process",
        )
        session_id = row.id
        await session.commit()

    stale_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with db_module.async_session() as session:
        await session.execute(
            update(UserSessionLease)
            .where(UserSessionLease.id == "abandoned-api-process")
            .values(expires_at=stale_seen_at)
        )
        await session.execute(
            update(UserSessionLog)
            .where(UserSessionLog.id == session_id)
            .values(last_seen_at=stale_seen_at)
        )
        await session.commit()

    async with db_module.async_session() as session:
        assert await cleanup_expired_user_session_leases(session) == 1
        assert not session.in_transaction()

    async with db_module.async_session() as session:
        row = await session.get(UserSessionLog, session_id)
        lease = await session.get(UserSessionLease, "abandoned-api-process")
        assert row is not None
        assert row.status == "closed"
        assert row.ended_at == stale_seen_at
        assert row.last_seen_at == stale_seen_at
        assert lease is None


@pytest.mark.asyncio
async def test_periodic_cleanup_commits_each_session_scope(monkeypatch):
    db = AsyncMock()
    scope_result = MagicMock()
    scope_result.all.return_value = [
        ("entity-1", "user-1"),
        ("entity-2", "user-2"),
    ]
    db.execute.return_value = scope_result
    lock_scope = AsyncMock(side_effect=[False, True])
    commit_counts: list[int] = []

    async def prune_scope(*_args, **_kwargs):
        commit_counts.append(db.commit.await_count)
        return {f"session-{len(commit_counts)}"}

    monkeypatch.setattr(user_session_service, "_try_lock_user_session_scope", lock_scope)
    monkeypatch.setattr(
        user_session_service,
        "_prune_expired_session_leases",
        prune_scope,
    )

    assert await cleanup_expired_user_session_leases(db) == 1
    assert db.rollback.await_count == 1
    assert db.commit.await_count == 1
    assert commit_counts == [0]


@pytest.mark.asyncio
async def test_leased_heartbeat_repairs_a_legacy_close(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-lease-rolling@test.com",
            "password": "pass123",
            "entity_name": "Session Lease Rolling",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    async with db_module.async_session() as session:
        row = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="new-api-process",
        )
        session_id = row.id
        await session.commit()

    async with db_module.async_session() as session:
        await close_user_session(
            session,
            session_id=session_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
        )
        await session.commit()

    async with db_module.async_session() as session:
        await touch_user_session(
            session,
            session_id=session_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="new-api-process",
        )
        await session.commit()

    async with db_module.async_session() as session:
        row = await session.get(UserSessionLog, session_id)
        assert row is not None
        assert row.status == "active"
        assert row.ended_at is None


@pytest.mark.asyncio
async def test_leased_heartbeat_reads_session_after_scope_lock(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-lease-lock-order@test.com",
            "password": "pass123",
            "entity_name": "Session Lease Lock Order",
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    async with db_module.async_session() as session:
        row = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="api-process-touch",
        )
        session_id = row.id
        await session.commit()

    async with db_module.async_session() as blocker:
        await _lock_user_session_scope(
            blocker,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
        )
        await blocker.execute(
            update(UserSessionLog)
            .where(UserSessionLog.id == session_id)
            .values(heartbeat_count=40)
        )

        async def touch_after_lock() -> None:
            async with db_module.async_session() as session:
                await touch_user_session(
                    session,
                    session_id=session_id,
                    entity_id=data["entity_id"],
                    user_id=data["user_id"],
                    lease_id="api-process-touch",
                )
                await session.commit()

        pending_touch = asyncio.create_task(touch_after_lock())
        await asyncio.sleep(0.05)
        assert not pending_touch.done()
        await blocker.commit()

    await pending_touch
    async with db_module.async_session() as session:
        row = await session.get(UserSessionLog, session_id)
        assert row is not None
        assert row.heartbeat_count == 41


@pytest.mark.asyncio
async def test_leased_session_timestamps_are_sampled_after_scope_lock(
    client,
    monkeypatch,
):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-lease-clock-order@test.com",
            "password": "pass123",
            "entity_name": "Session Lease Clock Order",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    real_datetime = datetime
    lock_acquired = False

    async def mark_scope_locked(*_args, **_kwargs):
        nonlocal lock_acquired
        lock_acquired = True

    class GuardedDateTime:
        @classmethod
        def now(cls, tz=None):
            assert lock_acquired
            return real_datetime(2026, 8, 21, 12, 0, tzinfo=tz)

    monkeypatch.setattr(
        user_session_service,
        "_lock_user_session_scope",
        mark_scope_locked,
    )
    monkeypatch.setattr(user_session_service, "datetime", GuardedDateTime)

    async with db_module.async_session() as session:
        row = await start_user_session(
            session,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="clock-order-process",
        )
        session_id = row.id
        await session.commit()

    lock_acquired = False
    async with db_module.async_session() as session:
        assert await touch_user_session(
            session,
            session_id=session_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="clock-order-process",
        )
        await session.commit()

    lock_acquired = False
    async with db_module.async_session() as session:
        await close_user_session(
            session,
            session_id=session_id,
            entity_id=data["entity_id"],
            user_id=data["user_id"],
            lease_id="clock-order-process",
        )
        await session.commit()
