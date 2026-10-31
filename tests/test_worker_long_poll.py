"""Long-poll heartbeats: an idle worker may wait for work addressed to it.

A heartbeat with ``wait_seconds`` that finds nothing to lease holds the
request (never a database transaction) until work targeted at that worker is
queued or the wait ends. Real Redis and real sessions; the second scenario is
a laptop worker's ``knowledge_local`` action, nothing desktop-specific.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.routers.workers import HeartbeatActiveLease, HeartbeatCapacity, HeartbeatRequest, _heartbeat_inner
from packages.core.models.worker import Worker
from packages.core.workers import wake

pytestmark = pytest.mark.asyncio


def _worker(worker_id: str, entity_id: str, user_id: str, *, targeted_only: bool) -> Worker:
    return Worker(
        id=worker_id,
        entity_id=entity_id,
        kind="custom_http",
        display_name=f"Worker {worker_id}",
        version="cli-1",
        capabilities={
            "supported_kinds": ["bash", "action"],
            "supported_providers": ["knowledge_local", "manor_cli"],
            "max_risk_level": "high",
            "targeted_only": targeted_only,
            "daemon": {"running": True},
            "protocol_version": 2,
        },
        trust_level="standard",
        status="active",
        created_by_user_id=user_id,
        last_heartbeat_at=datetime.now(timezone.utc),
    )


async def _account(db, *, entity_id: str, user_id: str, worker_id: str, targeted_only: bool) -> Worker:
    from tests.approval_settings_test_utils import action_rule, initialize_test_account_approval

    await initialize_test_account_approval(
        db, entity_id=entity_id, user_id=user_id, user_rules=[action_rule("save_to_local")],
    )
    worker = _worker(worker_id, entity_id, user_id, targeted_only=targeted_only)
    db.add(worker)
    await db.commit()
    return worker


def _beat(wait_seconds: int, *, active: list | None = None) -> HeartbeatRequest:
    return HeartbeatRequest(
        wait_seconds=wait_seconds,
        capacity=HeartbeatCapacity(can_accept_leases=1),
        active_leases=active or [],
    )


@pytest.fixture
def factory(db_session):
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)




async def test_without_wait_seconds_a_heartbeat_answers_at_once(db_session, factory) -> None:
    worker = await _account(
        db_session, entity_id="ent_lp_0", user_id="user_lp_0", worker_id="worker_lp_0", targeted_only=True,
    )
    async with factory() as db:
        started = time.monotonic()
        response = await _heartbeat_inner(_beat(0), await db.get(Worker, worker.id), db)
    assert time.monotonic() - started < 1.5
    assert response.new_leases == [] and response.long_poll is True


async def test_an_idle_long_poll_waits_without_holding_a_transaction(db_session, factory, monkeypatch) -> None:
    worker = await _account(
        db_session, entity_id="ent_lp_1", user_id="user_lp_1", worker_id="worker_lp_1", targeted_only=True,
    )
    seen: list[bool] = []
    real_wait = wake.wait_for_wake

    async with factory() as db:
        async def watched_wait(worker_id: str, timeout: float) -> bool:
            seen.append(db.in_transaction())
            return await real_wait(worker_id, timeout)

        monkeypatch.setattr(wake, "wait_for_wake", watched_wait)
        loaded = await db.get(Worker, worker.id)
        await db.commit()
        started = time.monotonic()
        response = await _heartbeat_inner(_beat(2), loaded, db)
    elapsed = time.monotonic() - started
    assert 1.8 <= elapsed < 5, elapsed
    assert response.new_leases == []
    assert seen == [False]
    # It asks to be polled again at once: the wait was the interval.
    assert response.next_heartbeat_in_seconds == 1




async def test_an_instruction_queued_for_a_waiting_worker_reaches_it_at_once(db_session, factory) -> None:
    from packages.core.workers.instructions import queue_instruction

    worker = await _account(
        db_session, entity_id="ent_lp_4", user_id="user_lp_4", worker_id="worker_lp_4", targeted_only=True,
    )

    async def poll():
        async with factory() as db:
            return await _heartbeat_inner(_beat(20), await db.get(Worker, worker.id), db), time.monotonic()

    polling = asyncio.create_task(poll())
    await asyncio.sleep(0.5)
    queued_at = time.monotonic()
    assert await queue_instruction("worker_lp_4", "open_view", {"session_id": "s1"})
    response, returned_at = await asyncio.wait_for(polling, timeout=10)
    assert returned_at - queued_at < 1.5
    assert [(i.type, i.payload) for i in response.instructions] == [("open_view", {"session_id": "s1"})]
    # Delivered once.
    async with factory() as db:
        again = await _heartbeat_inner(_beat(0), await db.get(Worker, worker.id), db)
    assert again.instructions == []


async def test_a_wait_longer_than_the_redis_read_timeout_still_wakes(db_session, factory, monkeypatch) -> None:
    """redis-py stops reading after its socket timeout (5 s by default): one
    BLPOP for the whole wait failed there and the worker fell back to its
    timer, so a live view's open_view arrived after the viewer gave up."""
    import redis.asyncio as aioredis

    from packages.core.config import get_settings
    from packages.core.workers.instructions import queue_instruction

    worker = await _account(
        db_session, entity_id="ent_lp_5", user_id="user_lp_5", worker_id="worker_lp_5", targeted_only=True,
    )
    short = aioredis.from_url(get_settings().REDIS_URL, decode_responses=True, socket_timeout=0.5)

    async def short_redis():
        return short

    monkeypatch.setattr(wake, "_redis", short_redis)
    try:
        async def poll():
            async with factory() as db:
                return await _heartbeat_inner(_beat(20), await db.get(Worker, worker.id), db), time.monotonic()

        polling = asyncio.create_task(poll())
        await asyncio.sleep(1.5)  # three read timeouts into the wait
        queued_at = time.monotonic()
        assert await queue_instruction("worker_lp_5", "open_view", {"session_id": "s5"})
        response, returned_at = await asyncio.wait_for(polling, timeout=10)
    finally:
        await short.aclose()
    assert returned_at - queued_at < 1.5
    assert [(i.type, i.payload) for i in response.instructions] == [("open_view", {"session_id": "s5"})]
    assert response.next_heartbeat_in_seconds == 1
