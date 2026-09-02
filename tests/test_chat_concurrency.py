from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from apps.api.routers import chat as chat_router
from apps.api.routers import public_chat as public_chat_router
from packages.core.services import chat_concurrency
from packages.core.services.chat_concurrency import (
    ChatConcurrencyConfig,
    ChatConcurrencyExceeded,
    ChatConcurrencyGate,
)


class FakeRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        zset = self.zsets.setdefault(key, {})
        stale = [member for member, score in zset.items() if min_score <= score <= max_score]
        for member in stale:
            del zset[member]
        return len(stale)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.setdefault(key, {}))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(key, {})
        created = 0
        for member, score in mapping.items():
            created += 0 if member in zset else 1
            zset[member] = score
        return created

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = seconds
        return True

    async def zrem(self, key: str, member: str) -> int:
        zset = self.zsets.setdefault(key, {})
        if member in zset:
            del zset[member]
            return 1
        return 0


class FakeLuaRedis(FakeRedis):
    async def eval(self, script: str, number_of_keys: int, key: str, *args):
        assert number_of_keys == 1
        if "ZADD" in script and "XX" in script:
            token, now, ttl = args
            zset = self.zsets.setdefault(key, {})
            if token not in zset:
                return 0
            zset[token] = float(now)
            self.expires[key] = int(ttl)
            return 1

        token, now, ttl, limit = args
        zset = self.zsets.setdefault(key, {})
        stale = [member for member, score in zset.items() if score <= float(now) - int(ttl)]
        for member in stale:
            del zset[member]
        if len(zset) >= int(limit):
            self.expires[key] = int(ttl)
            return 0
        zset[str(token)] = float(now)
        self.expires[key] = int(ttl)
        return 1


@pytest.mark.asyncio
async def test_chat_gate_reports_local_active_count() -> None:
    gate = ChatConcurrencyGate(
        redis_getter=lambda: None,
        config=ChatConcurrencyConfig(
            enabled=True,
            global_limit=0,
            per_instance_limit=20,
            wait_timeout_seconds=0,
            slot_ttl_seconds=300,
        ),
    )

    assert gate.local_active == 0
    first = await gate.acquire()
    second = await gate.acquire()
    assert gate.local_active == 2

    await first.release()
    assert gate.local_active == 1
    await second.release()
    assert gate.local_active == 0


def test_chat_stream_local_active_count_reads_default_gate(monkeypatch) -> None:
    gate = ChatConcurrencyGate(
        redis_getter=lambda: None,
        config=ChatConcurrencyConfig(
            enabled=True,
            global_limit=0,
            per_instance_limit=20,
            wait_timeout_seconds=0,
            slot_ttl_seconds=300,
        ),
    )
    monkeypatch.setattr(chat_concurrency, "_default_gate", gate)

    assert chat_concurrency.chat_stream_local_active_count() == 0


@pytest.mark.asyncio
async def test_chat_concurrency_gate_rejects_when_global_slots_are_full():
    redis = FakeRedis()
    gate = ChatConcurrencyGate(
        redis_getter=lambda: redis,
        config=ChatConcurrencyConfig(
            enabled=True,
            global_limit=1,
            per_instance_limit=2,
            wait_timeout_seconds=0,
            slot_ttl_seconds=30,
            key="test:chat:active",
        ),
    )

    lease = await gate.acquire(scope="chat")
    with pytest.raises(ChatConcurrencyExceeded):
        await gate.acquire(scope="chat")

    await lease.release()
    second = await gate.acquire(scope="chat")
    await second.release()


@pytest.mark.asyncio
async def test_chat_concurrency_gate_enforces_per_instance_limit():
    gate = ChatConcurrencyGate(
        redis_getter=lambda: None,
        config=ChatConcurrencyConfig(
            enabled=True,
            global_limit=0,
            per_instance_limit=1,
            wait_timeout_seconds=0,
            slot_ttl_seconds=30,
            key="test:chat:active",
        ),
    )

    lease = await gate.acquire(scope="chat")
    with pytest.raises(ChatConcurrencyExceeded):
        await gate.acquire(scope="chat")

    await lease.release()
    second = await gate.acquire(scope="chat")
    await second.release()


@pytest.mark.asyncio
async def test_chat_concurrency_gate_recovers_slots_after_ttl():
    redis = FakeRedis()
    redis.zsets["test:chat:active"] = {"stale-token": time.time() - 120}
    gate = ChatConcurrencyGate(
        redis_getter=lambda: redis,
        config=ChatConcurrencyConfig(
            enabled=True,
            global_limit=1,
            per_instance_limit=1,
            wait_timeout_seconds=0,
            slot_ttl_seconds=30,
            key="test:chat:active",
        ),
    )

    lease = await gate.acquire(scope="chat")

    assert list(redis.zsets["test:chat:active"]) != ["stale-token"]
    await lease.release()


@pytest.mark.asyncio
async def test_chat_concurrency_lease_renews_redis_slot_while_wrapped_stream_is_active():
    redis = FakeLuaRedis()
    now = 1_000.0
    gate = ChatConcurrencyGate(
        redis_getter=lambda: redis,
        config=ChatConcurrencyConfig(
            enabled=True,
            global_limit=1,
            per_instance_limit=1,
            wait_timeout_seconds=0,
            slot_ttl_seconds=30,
            key="test:chat:active",
            heartbeat_interval_seconds=0.01,
        ),
        clock=lambda: now,
    )

    lease = await gate.acquire(scope="chat")
    token = next(iter(redis.zsets["test:chat:active"]))

    async def source():
        nonlocal now
        now = 1_025.0
        await asyncio.sleep(0.03)
        yield "data: ok\n\n"

    chunks = [chunk async for chunk in lease.wrap(source())]

    assert chunks == ["data: ok\n\n"]
    assert redis.zsets["test:chat:active"] == {}
    assert redis.expires["test:chat:active"] == 30
    assert now == 1_025.0
    assert token not in redis.zsets["test:chat:active"]


@pytest.mark.asyncio
async def test_non_streaming_call_renews_beyond_ttl_and_during_cancelled_cleanup():
    redis, now = FakeLuaRedis(), 1_000.0
    gate = ChatConcurrencyGate(
        redis_getter=lambda: redis,
        config=ChatConcurrencyConfig(
            enabled=True, global_limit=1, per_instance_limit=0,
            wait_timeout_seconds=0, slot_ttl_seconds=900, heartbeat_interval_seconds=0.005,
        ),
        clock=lambda: now,
    )
    lease = await gate.acquire(scope="voice")
    lease.renew = AsyncMock(wraps=lease.renew)
    entered, cleaning, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def call():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await settled.wait()

    task = asyncio.create_task(lease.run(call()))
    await asyncio.wait_for(entered.wait(), 2)
    # Advance a long call past its original TTL, allowing regular renewal.
    for now in (1_500.0, 1_901.0):
        async with asyncio.timeout(2):
            while now not in redis.zsets[gate.config.key].values():
                await asyncio.sleep(0.005)
        with pytest.raises(ChatConcurrencyExceeded):
            await gate.acquire(scope="second-voice")
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 2)
    now = 2_500.0
    async with asyncio.timeout(2):
        while now not in redis.zsets[gate.config.key].values():
            await asyncio.sleep(0.005)
    with pytest.raises(ChatConcurrencyExceeded):
        await gate.acquire(scope="while-settling")
    settled.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    renewals = lease.renew.await_count
    await asyncio.sleep(0.02)
    assert lease.renew.await_count == renewals
    await lease.release()
    next_lease = await gate.acquire()
    await next_lease.release()
    assert redis.zsets[gate.config.key] == {}


@pytest.mark.asyncio
async def test_non_streaming_call_stops_when_global_lease_is_lost():
    redis = FakeLuaRedis()
    gate = ChatConcurrencyGate(
        redis_getter=lambda: redis,
        config=ChatConcurrencyConfig(
            enabled=True, global_limit=1, per_instance_limit=1,
            wait_timeout_seconds=0, slot_ttl_seconds=900, heartbeat_interval_seconds=0.005,
        ),
    )
    lease = await gate.acquire(scope="voice")
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def call():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(lease.run(call()))
    await asyncio.wait_for(entered.wait(), 2)
    redis.zsets[gate.config.key].clear()
    with pytest.raises(ChatConcurrencyExceeded, match="lease was lost"):
        await asyncio.wait_for(task, 2)
    assert stopped.is_set()
    await lease.release()
    assert gate.local_active == 0


@pytest.mark.asyncio
async def test_stalled_renewal_stops_first_call_before_another_slot_is_admitted():
    stalled, stopped = asyncio.Event(), asyncio.Event()
    now = 1_000.0

    class StalledRedis(FakeLuaRedis):
        async def eval(self, script, *args):
            if "XX" in script:
                stalled.set()
                await asyncio.Event().wait()
            return await super().eval(script, *args)

    redis = StalledRedis()
    config = ChatConcurrencyConfig(
        enabled=True, global_limit=1, per_instance_limit=0,
        wait_timeout_seconds=0, slot_ttl_seconds=900, heartbeat_interval_seconds=0.005,
    )
    gate = ChatConcurrencyGate(redis_getter=lambda: redis, config=config, clock=lambda: now)
    other_gate = ChatConcurrencyGate(redis_getter=lambda: redis, config=config, clock=lambda: now)
    lease = await gate.acquire(scope="voice")

    async def call():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(lease.run(call()))
    await asyncio.wait_for(stalled.wait(), 2)
    with pytest.raises(ChatConcurrencyExceeded, match="lease was lost"):
        await asyncio.wait_for(task, 2)
    now = 1_901.0
    second = await other_gate.acquire(scope="voice")
    assert stopped.is_set()
    await second.release()
    await lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_non_streaming_call_without_global_limit_finishes_cleanup(cancel):
    gate = ChatConcurrencyGate(
        config=ChatConcurrencyConfig(
            enabled=True, global_limit=0, per_instance_limit=1,
            wait_timeout_seconds=0, slot_ttl_seconds=900,
        ),
    )
    lease = await gate.acquire(scope="voice")
    entered, cleaning, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def call():
        try:
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            return "finished"
        finally:
            cleaning.set()
            await settled.wait()

    task = asyncio.create_task(lease.run(call()))
    await asyncio.wait_for(entered.wait(), 2)
    if cancel:
        task.cancel()
    await asyncio.wait_for(cleaning.wait(), 2)
    assert not task.done()
    settled.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        assert await asyncio.wait_for(task, 2) == "finished"
    await lease.release()
    assert gate.local_active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("realtime", [False, True])
@pytest.mark.parametrize("failure", ["missing", "stalled"])
async def test_voice_endpoint_closes_and_releases_capacity_when_renewal_fails(monkeypatch, realtime, failure):
    from apps.api import chat_voice
    from apps.api.chat_audio import ChatAudioScope

    redis = FakeLuaRedis()
    gate = ChatConcurrencyGate(
        redis_getter=lambda: redis,
        config=ChatConcurrencyConfig(
            enabled=True, global_limit=1, per_instance_limit=1,
            wait_timeout_seconds=0, slot_ttl_seconds=900, heartbeat_interval_seconds=0.005,
        ),
    )
    lease = await gate.acquire(scope="voice")
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def call():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            stopped.set()

    @asynccontextmanager
    async def database():
        yield object()

    ws = SimpleNamespace(
        accept=AsyncMock(), close=AsyncMock(), send_json=AsyncMock(),
        receive_text=AsyncMock(return_value=json.dumps({"type": "start", "token": "test"})),
    )
    monkeypatch.setattr(chat_voice, "async_session", database)
    monkeypatch.setattr(chat_voice, "call_request", lambda *_: object())
    monkeypatch.setattr(chat_voice, "resolve_call_scope", AsyncMock(return_value=ChatAudioScope("entity", "owner")))
    monkeypatch.setattr(chat_voice, "acquire_audio_lease", AsyncMock(return_value=lease))
    monkeypatch.setattr(
        chat_voice, "resolve_realtime_route", AsyncMock(return_value=object() if realtime else None)
    )
    monkeypatch.setattr(
        chat_voice,
        "BrowserVoiceSession" if realtime else "GatewayVoiceSession",
        lambda *_, **__: SimpleNamespace(ready=True, run=call),
    )
    task = asyncio.create_task(chat_voice.live_voice(ws))
    await asyncio.wait_for(entered.wait(), 2)
    if failure == "stalled":
        monkeypatch.setattr(lease, "renew", asyncio.Event().wait)
    else:
        redis.zsets[gate.config.key].clear()
    await asyncio.wait_for(task, 2)
    assert stopped.is_set() and gate.local_active == 0
    assert redis.zsets[gate.config.key] == {}
    ws.close.assert_awaited_once()
    assert ws.send_json.await_args.args[0]["status"] == 503


@pytest.mark.asyncio
async def test_chat_stream_acquires_concurrency_slot_before_attachment_work(monkeypatch):
    async def resolve_scope(*_args, **_kwargs):
        return None, None, None

    async def reject_chat_slot(*, scope: str):
        assert scope == "chat"
        raise HTTPException(status_code=503, detail="chat full")

    async def build_attachments(*_args, **_kwargs):
        raise AssertionError("attachment work should not run when chat capacity is full")

    async def skip_budget(*_args, **_kwargs):
        return False

    monkeypatch.setattr(chat_router, "_require_chat_budget_unless_pending_approval", skip_budget)
    monkeypatch.setattr(chat_router, "_resolve_chat_workspace_scope", resolve_scope)
    monkeypatch.setattr(chat_router, "acquire_chat_stream_lease", reject_chat_slot)
    monkeypatch.setattr(chat_router, "_build_attachments", build_attachments)

    with pytest.raises(HTTPException) as exc_info:
        await chat_router.chat_stream(
            message="hello",
            conversation_id=None,
            agent_id=None,
            workspace_id=None,
            workspace_context=False,
            thread_ref_kind=None,
            thread_ref_id=None,
            document_ids=None,
            manual_skill_ids=None,
            manual_skill_refs=None,
            chat_mode=None,
            chat_mode_payload=None,
            response_surface_submission=None,
            disable_tools=True,
            blocked_tools=None,
            editor_context=None,
            conversation_surface=None,
            ephemeral=False,
            files=[],
            user=SimpleNamespace(id="user-1", entity_id="entity-1"),
            db=object(),
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_chat_stream_releases_concurrency_slot_when_setup_fails_after_acquire(monkeypatch):
    class FakeLease:
        def __init__(self) -> None:
            self.release_count = 0

        async def release(self) -> None:
            self.release_count += 1

        async def wrap(self, source):
            async for chunk in source:
                yield chunk

    class FakeDb:
        async def commit(self) -> None:
            return None

    lease = FakeLease()

    async def resolve_scope(*_args, **_kwargs):
        return None, None, None

    async def acquire_slot(*, scope: str):
        assert scope == "chat"
        return lease

    async def build_attachments(*_args, **_kwargs):
        raise RuntimeError("bad attachment setup")

    async def prepare_manual_skill_turn(*_args, **_kwargs):
        return SimpleNamespace(
            manual_skill_refs=[],
            llm_base_message="hello",
            saved_user_base="hello",
        )

    async def get_conversation(*_args, **_kwargs):
        return SimpleNamespace(id="conv-1")

    async def resolve_approval(*_args, **_kwargs):
        return None, None, True, None

    async def add_message(*_args, **_kwargs):
        return None

    async def create_placeholder(*_args, **_kwargs):
        return SimpleNamespace(id="assistant-1")

    async def skip_budget(*_args, **_kwargs):
        return False

    monkeypatch.setattr(chat_router, "_require_chat_budget_unless_pending_approval", skip_budget)
    monkeypatch.setattr(chat_router, "_resolve_chat_workspace_scope", resolve_scope)
    monkeypatch.setattr(chat_router, "acquire_chat_stream_lease", acquire_slot)
    monkeypatch.setattr(chat_router, "_build_attachments", build_attachments)
    monkeypatch.setattr(chat_router, "_prepare_manual_skill_turn", prepare_manual_skill_turn)
    monkeypatch.setattr(chat_router, "_stream_llm_message_with_attachments", lambda *_args, **_kwargs: "hello")
    monkeypatch.setattr(chat_router, "get_or_create_conversation", get_conversation)
    monkeypatch.setattr(chat_router, "resolve_chat_approval_turn", resolve_approval)
    monkeypatch.setattr(chat_router, "runtime_saved_message_with_file_references", lambda message, _attachments: message)
    monkeypatch.setattr(chat_router, "add_message", add_message)
    monkeypatch.setattr(chat_router, "create_assistant_stream_placeholder", create_placeholder)

    with pytest.raises(RuntimeError, match="bad attachment setup"):
        await chat_router.chat_stream(
            message="hello",
            conversation_id=None,
            agent_id=None,
            workspace_id=None,
            workspace_context=False,
            thread_ref_kind=None,
            thread_ref_id=None,
            document_ids=None,
            manual_skill_ids=None,
            manual_skill_refs=None,
            chat_mode=None,
            chat_mode_payload=None,
            response_surface_submission=None,
            disable_tools=True,
            blocked_tools=None,
            editor_context=None,
            ephemeral=False,
            conversation_surface=None,
            files=[],
            user=SimpleNamespace(id="user-1", entity_id="entity-1"),
            db=FakeDb(),
        )

    assert lease.release_count == 1


@pytest.mark.asyncio
async def test_chat_stream_releases_concurrency_slot_when_cancelled_after_acquire(monkeypatch):
    class FakeLease:
        def __init__(self) -> None:
            self.release_count = 0

        async def release(self) -> None:
            self.release_count += 1

        async def wrap(self, source):
            async for chunk in source:
                yield chunk

    lease = FakeLease()

    async def resolve_scope(*_args, **_kwargs):
        return None, None, None

    async def acquire_slot(*, scope: str):
        assert scope == "chat"
        return lease

    async def cancelled_attachment_work(*_args, **_kwargs):
        raise asyncio.CancelledError()

    async def skip_budget(*_args, **_kwargs):
        return False

    monkeypatch.setattr(chat_router, "_require_chat_budget_unless_pending_approval", skip_budget)
    monkeypatch.setattr(chat_router, "_resolve_chat_workspace_scope", resolve_scope)
    monkeypatch.setattr(chat_router, "acquire_chat_stream_lease", acquire_slot)
    monkeypatch.setattr(chat_router, "_build_attachments", cancelled_attachment_work)

    with pytest.raises(asyncio.CancelledError):
        await chat_router.chat_stream(
            message="hello",
            conversation_id=None,
            agent_id=None,
            workspace_id=None,
            workspace_context=False,
            thread_ref_kind=None,
            thread_ref_id=None,
            document_ids=None,
            manual_skill_ids=None,
            manual_skill_refs=None,
            chat_mode=None,
            chat_mode_payload=None,
            response_surface_submission=None,
            disable_tools=True,
            blocked_tools=None,
            editor_context=None,
            ephemeral=False,
            conversation_surface=None,
            files=[],
            user=SimpleNamespace(id="user-1", entity_id="entity-1"),
            db=object(),
        )

    assert lease.release_count == 1


@pytest.mark.asyncio
async def test_public_chat_stream_acquires_concurrency_slot_before_channel_lookup(monkeypatch):
    async def reject_chat_slot(*, scope: str):
        assert scope == "public-chat"
        raise HTTPException(status_code=503, detail="chat full")

    async def resolve_channel(*_args, **_kwargs):
        raise AssertionError("channel lookup should not run when public chat capacity is full")

    monkeypatch.setattr(public_chat_router, "acquire_chat_stream_lease", reject_chat_slot)
    monkeypatch.setattr(public_chat_router, "_resolve_channel_by_token", resolve_channel)

    with pytest.raises(HTTPException) as exc_info:
        await public_chat_router.stream_message(
            token="public-token",
            request=object(),
            session_id="session-1",
            message="hello",
            files=[],
            db=object(),
        )

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_public_chat_stream_releases_concurrency_slot_when_lookup_fails(monkeypatch):
    class FakeLease:
        def __init__(self) -> None:
            self.release_count = 0

        async def release(self) -> None:
            self.release_count += 1

        async def wrap(self, source):
            async for chunk in source:
                yield chunk

    lease = FakeLease()

    async def acquire_slot(*, scope: str):
        assert scope == "public-chat"
        return lease

    async def missing_channel(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="Chat not found")

    monkeypatch.setattr(public_chat_router, "acquire_chat_stream_lease", acquire_slot)
    monkeypatch.setattr(public_chat_router, "_resolve_channel_by_token", missing_channel)

    with pytest.raises(HTTPException) as exc_info:
        await public_chat_router.stream_message(
            token="missing-token",
            request=object(),
            session_id="session-1",
            message="hello",
            files=[],
            db=object(),
        )

    assert exc_info.value.status_code == 404
    assert lease.release_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subscription_source", "workspace_routable"),
    [("invalid", True), ("contact", False)],
)
async def test_public_chat_stream_rejects_invalid_or_unroutable_subscription(
    monkeypatch,
    subscription_source,
    workspace_routable,
):
    from packages.core.services import agent_subscription_service

    class FakeLease:
        def __init__(self) -> None:
            self.release_count = 0

        async def release(self) -> None:
            self.release_count += 1

        async def wrap(self, source):
            async for chunk in source:
                yield chunk

    lease = FakeLease()

    async def acquire_slot(*, scope: str):
        assert scope == "public-chat"
        return lease

    async def resolve_channel(*_args, **_kwargs):
        return SimpleNamespace(entity_id="entity-1"), SimpleNamespace()

    async def allow_access(*_args, **_kwargs):
        return None

    async def ensure_contact(*_args, **_kwargs):
        return SimpleNamespace(profile={})

    async def resolve_subscription(*_args, **_kwargs):
        return SimpleNamespace(
            source=subscription_source,
            workspace_id="workspace-1",
        )

    async def workspace_is_routable(*_args, **_kwargs):
        return workspace_routable

    monkeypatch.setattr(public_chat_router, "acquire_chat_stream_lease", acquire_slot)
    monkeypatch.setattr(public_chat_router, "_resolve_channel_by_token", resolve_channel)
    monkeypatch.setattr(public_chat_router, "_require_chat_access", allow_access)
    monkeypatch.setattr(
        public_chat_router,
        "_ensure_session_contact_for_user",
        ensure_contact,
    )
    monkeypatch.setattr(
        public_chat_router,
        "channel_workspace_is_routable",
        workspace_is_routable,
    )
    monkeypatch.setattr(
        agent_subscription_service,
        "resolve_subscription",
        resolve_subscription,
    )

    with pytest.raises(HTTPException) as exc_info:
        await public_chat_router.stream_message(
            token="public-token",
            request=object(),
            session_id="session-1",
            message="hello",
            files=[],
            db=object(),
        )

    assert exc_info.value.status_code == 404
    assert lease.release_count == 1
