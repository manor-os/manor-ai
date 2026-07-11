"""Page-view dwell time + path normalisation in user_session_service."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models.user_session import UserPageViewLog, UserSessionLog
from packages.core.services.user_session_service import (
    _normalise_path,
    close_user_session,
    start_user_session,
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
