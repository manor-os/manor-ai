from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest

from packages.core.constants.workspace_drafts import STAFF_REQUEST_TEXT_FIELD


@pytest.mark.asyncio
@pytest.mark.parametrize("selections", [None, [], "account", {"channel:0:telegram": None}])
async def test_draft_fields_reject_malformed_channel_selections(selections):
    from fastapi import HTTPException
    from apps.api.routers.workspace_drafts import update_draft_fields

    with pytest.raises(HTTPException) as error:
        await update_draft_fields(
            "draft", {"blueprint_channel_config_ids": selections},
            user=SimpleNamespace(), db=SimpleNamespace(),
        )
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_workspace_draft_service_rejects_missing_creator_before_persisting():
    from packages.core.services.workspace_draft_service import start_draft

    with pytest.raises(ValueError, match="user context is required"):
        await start_draft(
            SimpleNamespace(),
            entity_id="entity",
            user_id="",
        )


@pytest.mark.asyncio
async def test_chat_draft_start_requires_user_context():
    from packages.core.ai.runtime.workspace_drafts import (
        runtime_start_workspace_draft_action,
    )

    result = json.loads(await runtime_start_workspace_draft_action(
        entity_id="entity",
        user_id="",
        initial_brief="Build a launch workspace",
    ))

    assert result == {"error": "workspace draft user context missing"}


@pytest.mark.asyncio
async def test_global_chat_cannot_start_draft_without_explicit_create_intent():
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workspace_drafts import (
        runtime_start_workspace_draft_action,
    )

    result = json.loads(await runtime_start_workspace_draft_action(
        entity_id="entity",
        user_id="creator",
        initial_brief="Manage my properties",
        runtime_envelope=SimpleNamespace(
            surface=ChatSurface.GLOBAL_OWNER_CHAT,
            metadata={
                "workspace_creation_authorization": {
                    "status": "not_authorized",
                    "confidence": 0.99,
                },
            },
        ),
    ))

    assert result == {
        "error": "workspace_creation_confirmation_required",
        "message": (
            "Start a Workspace draft only after the user explicitly asks to "
            "create a new Workspace."
        ),
    }


@pytest.mark.asyncio
async def test_global_chat_allows_draft_start_for_explicit_create_intent():
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workspace_drafts import (
        _workspace_draft_start_allowed,
    )

    assert await _workspace_draft_start_allowed(SimpleNamespace(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        metadata={
            "workspace_creation_authorization": {
                "status": "authorized",
                "confidence": 0.99,
            },
        },
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize(("published", "allowed"), [
    ({"workspace_creation_authorization": {"status": "authorized", "confidence": 0.95}}, True),
    ({"workspace_creation_authorization": {"status": "not_authorized", "confidence": 0.9}}, False),
    (None, False),
], ids=["authorized", "not-authorized", "never-published"])
async def test_deferred_authorization_is_read_when_the_draft_tool_runs(
    monkeypatch: pytest.MonkeyPatch, published, allowed,
):
    """The classifier runs beside the turn; the gate waits for its verdict and
    fails closed when none arrives."""
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workspace_drafts import (
        _workspace_draft_start_allowed,
    )
    from packages.core.services import chat_intent_routing

    waited: list[tuple[str, float]] = []

    async def fake_wait(key, *, timeout):
        waited.append((key, timeout))
        return published

    monkeypatch.setattr(chat_intent_routing, "wait_for_deferred_chat_intent", fake_wait)

    assert await _workspace_draft_start_allowed(SimpleNamespace(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        metadata={"workspace_creation_authorization": {"status": "deferred", "key": "intent-1"}},
    )) is allowed
    assert waited == [("intent-1", 15.0)]


@pytest.mark.asyncio
async def test_chat_draft_start_checks_workspace_plan_before_architect_spend(
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core import database
    from packages.core.ai.runtime.workspace_drafts import (
        runtime_start_workspace_draft_action,
    )
    from packages.core.services import plan_gate, workspace_draft_service

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    gate = plan_gate.GateResult(
        allowed=False,
        message="Workspace limit reached",
        limit=1,
        current=1,
        plan="Free",
    )
    start_draft = AsyncMock()
    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(plan_gate, "check", AsyncMock(return_value=gate))
    monkeypatch.setattr(workspace_draft_service, "start_draft", start_draft)

    result = json.loads(await runtime_start_workspace_draft_action(
        entity_id="entity",
        user_id="creator",
        initial_brief="Build a launch workspace",
    ))

    assert result == {
        "error": "Workspace limit reached",
        "detail": {
            "message": "Workspace limit reached",
            "limit": 1,
            "current": 1,
            "plan": "Free",
            "kind": "workspaces",
        },
    }
    start_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_ready_lints_with_creator_scope(
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.ai.tools import workspace_arch_tools

    draft = SimpleNamespace(ready=False, status="active", missing=["services"])
    monkeypatch.setattr(
        workspace_arch_tools,
        "_load_draft",
        AsyncMock(return_value=draft),
    )
    lint = AsyncMock(return_value=json.dumps({
        "ok": True,
        "ok_to_finalize": True,
        "issues": [],
    }))
    monkeypatch.setattr(workspace_arch_tools, "_lint_draft", lint)
    db = AsyncMock()

    result = json.loads(await workspace_arch_tools._mark_ready(
        db,
        entity_id="entity",
        user_id="creator",
        draft_id="draft",
    ))

    assert result["ready"] is True
    lint.assert_awaited_once_with(
        db,
        entity_id="entity",
        user_id="creator",
        draft_id="draft",
    )


@pytest.mark.asyncio
async def test_workspace_draft_is_scoped_to_creator(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services.workspace_draft_service import get_draft

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields={},
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(draft)
    await db_session.flush()

    assert await get_draft(db_session, draft.id, entity_id, creator_id) is draft
    assert await get_draft(db_session, draft.id, entity_id, generate_ulid()) is None


@pytest.mark.asyncio
async def test_initial_brief_is_persisted_before_architect_turn(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.services import workspace_draft_service

    async def fake_architect(*args, **kwargs):
        draft = kwargs["draft"]
        assert draft.fields["initial_brief"] == "A tiny design team"
        return "What should the team deliver each week?"

    async def fake_refresh(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_draft_service, "_architect_turn", fake_architect)
    monkeypatch.setattr(
        workspace_draft_service,
        "_refresh_missing_from_lint",
        fake_refresh,
    )

    _, draft = await workspace_draft_service.start_draft(
        db_session,
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        initial_brief="  A tiny design team  ",
    )

    assert draft.fields["initial_brief"] == "A tiny design team"
    assert draft.messages[0]["content"] == "A tiny design team"


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_brief", [None, "", "   "])
async def test_empty_initial_brief_starts_assistant_discovery(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    initial_brief,
):
    from packages.core.models.base import generate_ulid
    from packages.core.services import workspace_draft_service

    architect_turn = AsyncMock(return_value="architect reply")
    monkeypatch.setattr(
        workspace_draft_service,
        "_architect_turn",
        architect_turn,
    )
    monkeypatch.setattr(
        workspace_draft_service,
        "_refresh_missing_from_lint",
        AsyncMock(return_value=None),
    )

    reply, draft = await workspace_draft_service.start_draft(
        db_session,
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
        initial_brief=initial_brief,
    )

    assert reply == "architect reply"
    assert "initial_brief" not in draft.fields
    assert draft.messages == [{"role": "assistant", "content": reply}]
    assert draft.status == "active"
    architect_turn.assert_awaited_once()
    assert architect_turn.await_args.kwargs["user_message"] == "begin"


@pytest.mark.asyncio
async def test_stream_commits_draft_shell_and_emits_exact_id_before_architect(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts

    commits: list[str] = []
    architect_started = asyncio.Event()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def commit(self):
            commits.append("commit")

    async def fake_create_shell(_db, **kwargs):
        assert kwargs["initial_brief"] == "A tiny design team"
        return SimpleNamespace(id="draft_exact", status="active")

    async def fake_process_message(_db, **kwargs):
        assert commits == ["commit"]
        assert kwargs["draft_id"] == "draft_exact"
        assert kwargs["user_message"] == "A tiny design team"
        architect_started.set()
        raise ValueError("stop after start timing check")

    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "create_draft_shell",
        fake_create_shell,
    )
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "process_draft_message",
        fake_process_message,
    )

    draft_id = await workspace_drafts._persist_stream_draft_shell(
        entity_id="entity",
        user_id="creator",
        initial_brief="A tiny design team",
    )
    assert draft_id == "draft_exact"
    assert commits == ["commit"]

    stream = workspace_drafts._stream_turn(
        entity_id="entity",
        user_id="creator",
        draft_id=draft_id,
        user_message="A tiny design team",
        mode="create",
    )
    first_event = await anext(stream)

    assert commits == ["commit"]
    assert not architect_started.is_set()
    assert "event: start" in first_event
    assert '"draft_id": "draft_exact"' in first_event
    assert '"mode": "create"' in first_event

    second_event = await anext(stream)
    assert architect_started.is_set()
    assert "event: error" in second_event
    await stream.aclose()


@pytest.mark.asyncio
async def test_workspace_draft_stream_keeps_quiet_architect_turn_connected(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts

    architect_started = asyncio.Event()
    keep_running = asyncio.Event()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def fake_process_message(_db, **_kwargs):
        architect_started.set()
        await keep_running.wait()
        raise AssertionError("test should close the stream before completion")

    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "process_draft_message",
        fake_process_message,
    )
    monkeypatch.setattr(
        workspace_drafts,
        "_DRAFT_STREAM_KEEPALIVE_SECONDS",
        0.01,
    )

    stream = workspace_drafts._stream_turn(
        entity_id="entity",
        user_id="creator",
        draft_id="draft_exact",
        user_message="Build a collaboration workspace",
        mode="message",
    )

    assert "event: start" in await anext(stream)
    keepalive = await asyncio.wait_for(anext(stream), timeout=0.2)

    assert architect_started.is_set()
    assert "event: keepalive" in keepalive
    assert '"draft_id": "draft_exact"' in keepalive
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("architect_turn", [True, False])
async def test_only_architect_turn_streams_stop_when_they_send_nothing_but_keepalives(architect_turn):
    """A stuck turn's keepalives must not count as progress, or its stream
    slot is renewed for as long as it hangs. Finalize keeps running after a
    disconnect, so it has no stall watchdog."""
    from apps.api.routers import workspace_drafts

    wrapped: dict = {}

    class FakeDB:
        async def commit(self):
            return None

        async def close(self):
            return None

    class FakeLease:
        def wrap(self, source, **watchdog):
            wrapped.update(watchdog)
            return source

    async def source():
        yield workspace_drafts._sse("done", {})

    await workspace_drafts._workspace_draft_streaming_response(
        FakeDB(), FakeLease(), source(), architect_turn=architect_turn,
    )

    if not architect_turn:
        assert wrapped == {}
        return
    assert wrapped["stall_timeout_seconds"] > 0
    is_progress = wrapped["is_progress"]
    assert not is_progress(workspace_drafts._sse("keepalive", {"draft_id": "d"}))
    for event in ("start", "token", "tool_start", "tool_progress", "tool_end", "done"):
        assert is_progress(workspace_drafts._sse(event, {}))
    # The saved request runs a stopped turn again, so nobody is told it stopped.
    assert wrapped["stalled_chunk"]().startswith("event: continuing\n")


@pytest.mark.asyncio
async def test_workspace_draft_stream_releases_request_db_before_wrapping():
    from apps.api.routers import workspace_drafts

    lifecycle: list[str] = []

    class FakeDB:
        async def commit(self):
            lifecycle.append("commit")

        async def close(self):
            lifecycle.append("close")

    class FakeLease:
        def wrap(self, source, **_watchdog):
            assert lifecycle == ["commit", "close"]
            lifecycle.append("wrap")
            return source

    async def source():
        yield "event: done\ndata: {}\n\n"

    response = await workspace_drafts._workspace_draft_streaming_response(
        FakeDB(),
        FakeLease(),
        source(),
        status_code=201,
    )

    assert response.status_code == 201
    assert response.media_type == "text/event-stream"
    assert lifecycle == ["commit", "close", "wrap"]


@pytest.mark.asyncio
async def test_stream_response_is_not_created_until_draft_shell_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts

    calls: list[str] = []

    class FakeLease:
        async def release(self):
            calls.append("release")

        async def wrap(self, source, **_watchdog):
            async for chunk in source:
                yield chunk

    async def fake_acquire(**_kwargs):
        calls.append("lease")
        return FakeLease()

    async def fake_persist(**kwargs):
        assert kwargs["initial_brief"] == "A tiny design team"
        assert kwargs["draft_id"] is None
        calls.append("persist")
        return "draft_exact"

    monkeypatch.setattr(workspace_drafts, "acquire_chat_stream_lease", fake_acquire)
    monkeypatch.setattr(workspace_drafts, "_persist_stream_draft_shell", fake_persist)
    monkeypatch.setattr(
        workspace_drafts,
        "_enforce_new_workspace_draft_plan",
        AsyncMock(return_value=None),
        raising=False,
    )

    response = await workspace_drafts.create_draft_stream(
        workspace_drafts.StartDraftRequest(initial_brief="A tiny design team"),
        user=SimpleNamespace(entity_id="entity", id="creator"),
        db=AsyncMock(),
    )

    assert response.status_code == 201
    assert calls == ["lease", "persist"]


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_brief", [None, "", "   "])
async def test_empty_stream_request_starts_architect_discovery(
    monkeypatch: pytest.MonkeyPatch,
    initial_brief,
):
    from apps.api.routers import workspace_drafts

    class FakeLease:
        async def release(self):
            return None

        def wrap(self, stream, **_watchdog):
            return stream

    stream_turn = Mock(return_value=iter(()))
    monkeypatch.setattr(
        workspace_drafts,
        "acquire_chat_stream_lease",
        AsyncMock(return_value=FakeLease()),
    )
    monkeypatch.setattr(
        workspace_drafts,
        "_persist_stream_draft_shell",
        AsyncMock(return_value="01J00000000000000000000000"),
    )
    monkeypatch.setattr(workspace_drafts, "_stream_turn", stream_turn)

    response = await workspace_drafts.create_draft_stream(
        workspace_drafts.StartDraftRequest(initial_brief=initial_brief),
        user=SimpleNamespace(entity_id="entity", id="creator"),
        db=AsyncMock(),
    )

    assert response.status_code == 201
    stream_turn.assert_called_once_with(
        entity_id="entity",
        user_id="creator",
        draft_id="01J00000000000000000000000",
        user_message="begin",
        mode="create",
    )


@pytest.mark.asyncio
async def test_client_draft_id_reuses_shell_before_first_sse_event(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts

    requested_id = "01J00000000000000000000000"
    stored = None
    commits = 0

    class FakeNestedTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def begin_nested(self):
            return FakeNestedTransaction()

        async def commit(self):
            nonlocal commits
            commits += 1

    async def fake_get_draft(_db, draft_id, entity_id, user_id):
        assert (draft_id, entity_id, user_id) == (
            requested_id,
            "entity",
            "creator",
        )
        return stored

    async def fake_create_shell(_db, **kwargs):
        nonlocal stored
        assert kwargs["draft_id"] == requested_id
        stored = SimpleNamespace(id=requested_id, status="active")
        return stored

    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    get_draft = AsyncMock(side_effect=fake_get_draft)
    create_shell = AsyncMock(side_effect=fake_create_shell)
    monkeypatch.setattr(workspace_drafts.draft_service, "get_draft", get_draft)
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "create_draft_shell",
        create_shell,
    )
    plan_gate = AsyncMock(return_value=None)
    monkeypatch.setattr(
        workspace_drafts,
        "_enforce_new_workspace_draft_plan",
        plan_gate,
    )
    plan_user = SimpleNamespace(entity_id="entity", id="creator")

    first_id = await workspace_drafts._persist_stream_draft_shell(
        entity_id="entity",
        user_id="creator",
        initial_brief="A tiny design team",
        draft_id=requested_id,
        plan_user=plan_user,
    )
    second_id = await workspace_drafts._persist_stream_draft_shell(
        entity_id="entity",
        user_id="creator",
        initial_brief="A different retry payload",
        draft_id=requested_id,
        plan_user=plan_user,
    )

    assert first_id == second_id == requested_id
    assert create_shell.await_count == 1
    assert get_draft.await_count == 2
    assert commits == 2
    plan_gate.assert_awaited_once_with(plan_user, db=ANY)


@pytest.mark.asyncio
async def test_stream_resume_does_not_recheck_new_workspace_plan(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts

    assert "_gate" not in inspect.signature(
        workspace_drafts.create_draft_stream,
    ).parameters

    class FakeLease:
        async def release(self):
            return None

        async def wrap(self, source, **_watchdog):
            async for chunk in source:
                yield chunk

    gate = AsyncMock(side_effect=AssertionError("resume must not check capacity"))
    monkeypatch.setattr(
        workspace_drafts,
        "_enforce_new_workspace_draft_plan",
        gate,
        raising=False,
    )
    monkeypatch.setattr(
        workspace_drafts,
        "acquire_chat_stream_lease",
        AsyncMock(return_value=FakeLease()),
    )
    monkeypatch.setattr(
        workspace_drafts,
        "_resume_stream_draft_shell",
        AsyncMock(return_value="Stored brief"),
    )
    draft_id = "01J00000000000000000000000"
    monkeypatch.setattr(
        workspace_drafts,
        "_persist_stream_draft_shell",
        AsyncMock(return_value=draft_id),
    )

    response = await workspace_drafts.create_draft_stream(
        workspace_drafts.StartDraftRequest(draft_id=draft_id),
        user=SimpleNamespace(entity_id="entity", id="creator"),
        db=AsyncMock(),
    )

    assert response.status_code == 201
    gate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_brief", ["A tiny design team", "begin"])
async def test_resumed_opening_reuses_committed_reply_without_second_architect_turn(
    monkeypatch: pytest.MonkeyPatch,
    initial_brief,
):
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_draft_service

    draft = WorkspaceDraft(
        status="active",
        fields={},
        messages=[
            *([] if initial_brief == "begin" else [{"role": "user", "content": initial_brief}]),
            {"role": "assistant", "content": "What should it deliver?"},
        ],
    )
    monkeypatch.setattr(
        workspace_draft_service,
        "get_draft",
        AsyncMock(return_value=draft),
    )
    architect = AsyncMock()
    monkeypatch.setattr(workspace_draft_service, "_architect_turn", architect)
    streamed: list[tuple[str, dict]] = []

    async def stream_handler(name: str, payload: dict):
        streamed.append((name, payload))

    reply, returned = await workspace_draft_service.process_draft_message(
        AsyncMock(),
        draft_id="draft_exact",
        entity_id="entity",
        user_id="creator",
        user_message=initial_brief,
        stream_handler=stream_handler,
        dedupe_opening=True,
    )

    assert returned is draft
    assert reply == "What should it deliver?"
    assert streamed == [("text_delta", {"content": "What should it deliver?"})]
    architect.assert_not_awaited()
    # The replay drops its turn claim in the write the caller commits.
    assert workspace_draft_service.TURN_CLAIM_FIELD not in draft.fields


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_brief", ["Stored brief", None, ""])
async def test_stream_resume_uses_owned_shell_and_stored_brief(
    monkeypatch: pytest.MonkeyPatch,
    initial_brief,
):
    from apps.api.routers import workspace_drafts

    draft = SimpleNamespace(
        id="draft_exact",
        status="active",
        fields={"initial_brief": initial_brief} if initial_brief is not None else {},
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    get_draft = AsyncMock(return_value=draft)
    monkeypatch.setattr(workspace_drafts.draft_service, "get_draft", get_draft)

    opening = await workspace_drafts._resume_stream_draft_shell(
        entity_id="entity",
        user_id="creator",
        draft_id="draft_exact",
    )

    assert opening == (initial_brief or "begin")
    get_draft.assert_awaited_once_with(
        ANY,
        "draft_exact",
        "entity",
        "creator",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("message", "Draft is abandoned and cannot be changed"),
        ("blueprint", "Draft is abandoned and cannot be changed"),
    ],
)
async def test_terminal_draft_mutations_lock_and_fail_closed(
    operation: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import workspace_draft_service

    draft = SimpleNamespace(status="abandoned")
    get_draft = AsyncMock(return_value=draft)
    monkeypatch.setattr(workspace_draft_service, "get_draft", get_draft)

    with pytest.raises(ValueError, match=expected):
        if operation == "message":
            await workspace_draft_service.process_draft_message(
                AsyncMock(),
                draft_id="draft",
                entity_id="entity",
                user_id="user",
                user_message="change it",
            )
        else:
            await workspace_draft_service.apply_blueprint(
                AsyncMock(),
                draft_id="draft",
                entity_id="entity",
                user_id="user",
                blueprint_id="blueprint",
            )

    get_draft.assert_awaited_once_with(
        ANY,
        "draft",
        "entity",
        "user",
        for_update=True,
        # A message turn re-reads the row each time it locks the draft.
        **({"populate_existing": True} if operation == "message" else {}),
    )


@pytest.mark.asyncio
async def test_workspace_capacity_uses_entity_lock_and_fresh_gate(
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import plan_gate

    class FakeResult:
        def scalar_one_or_none(self):
            return "entity"

    class FakeDB:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return FakeResult()

    db = FakeDB()
    gate_check = AsyncMock(return_value=plan_gate.GateResult(allowed=True))
    monkeypatch.setattr(plan_gate, "is_cloud", lambda: True)
    monkeypatch.setattr(plan_gate, "check", gate_check)

    await plan_gate.enforce_workspace_capacity(db, "entity")

    assert "FOR UPDATE" in str(db.statement)
    gate_check.assert_awaited_once_with(
        db,
        "entity",
        "workspaces",
        use_cache=False,
    )


@pytest.mark.asyncio
async def test_local_workspace_capacity_has_no_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import plan_gate

    db = AsyncMock()
    gate_check = AsyncMock()
    monkeypatch.setattr(plan_gate, "is_cloud", lambda: False)
    monkeypatch.setattr(plan_gate, "check", gate_check)

    await plan_gate.enforce_workspace_capacity(db, "entity")

    db.execute.assert_not_awaited()
    gate_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_stream_preserves_workspace_plan_limit_detail(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts
    from packages.core.services import plan_gate

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    class FakeLease:
        async def wrap(self, source, **_watchdog):
            async for chunk in source:
                yield chunk

    error = plan_gate.WorkspacePlanLimitError(plan_gate.GateResult(
        allowed=False,
        message="Workspace limit reached",
        limit=1,
        current=1,
        plan="Free",
    ))
    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "finalize_draft",
        AsyncMock(side_effect=error),
    )
    monkeypatch.setattr(
        workspace_drafts,
        "acquire_chat_stream_lease",
        AsyncMock(return_value=FakeLease()),
    )

    response = await workspace_drafts.finalize_stream(
        "draft",
        SimpleNamespace(entity_id="entity", id="creator"),
        db=AsyncMock(),
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "event: error" in body
    assert '"message": "Workspace limit reached"' in body
    assert '"kind": "workspaces"' in body


@pytest.mark.asyncio
async def test_finalize_stream_commits_verified_not_ready_state(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts
    from packages.core.services.workspace_draft_service import (
        WorkspaceDraftNotReadyError,
    )

    commits: list[bool] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def commit(self):
            commits.append(True)

    class FakeLease:
        async def wrap(self, source, **_watchdog):
            async for chunk in source:
                yield chunk

    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "finalize_draft",
        AsyncMock(side_effect=WorkspaceDraftNotReadyError(
            "Draft not ready -- still missing: creation_preferences"
        )),
    )
    monkeypatch.setattr(
        workspace_drafts,
        "acquire_chat_stream_lease",
        AsyncMock(return_value=FakeLease()),
    )

    response = await workspace_drafts.finalize_stream(
        "draft",
        SimpleNamespace(entity_id="entity", id="creator"),
        db=AsyncMock(),
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert commits == [True]
    assert "event: error" in body
    assert "creation_preferences" in body


@pytest.mark.asyncio
async def test_finalize_rolls_back_resource_materialization_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """A post-provisioning failure must not use the stale-ready commit path."""
    from fastapi import HTTPException
    from apps.api.routers import workspace_drafts
    from packages.core.services.workspace_draft_service import WorkspaceDraftMaterializationError

    db = AsyncMock()
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "finalize_draft",
        AsyncMock(side_effect=WorkspaceDraftMaterializationError("workflow install failed")),
    )

    with pytest.raises(HTTPException) as error:
        await workspace_drafts.finalize(
            "draft",
            user=SimpleNamespace(entity_id="entity", id="creator"),
            db=db,
        )

    assert error.value.status_code == 400
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_stream_rolls_back_resource_materialization_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts
    from packages.core.services.workspace_draft_service import WorkspaceDraftMaterializationError

    commits: list[bool] = []
    rollbacks: list[bool] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def commit(self):
            commits.append(True)

        async def rollback(self):
            rollbacks.append(True)

    class FakeLease:
        async def wrap(self, source, **_watchdog):
            async for chunk in source:
                yield chunk

    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "finalize_draft",
        AsyncMock(side_effect=WorkspaceDraftMaterializationError("workflow install failed")),
    )
    monkeypatch.setattr(
        workspace_drafts,
        "acquire_chat_stream_lease",
        AsyncMock(return_value=FakeLease()),
    )

    response = await workspace_drafts.finalize_stream(
        "draft",
        SimpleNamespace(entity_id="entity", id="creator"),
        db=AsyncMock(),
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert commits == []
    assert rollbacks == [True]
    assert "event: error" in body
    assert "workflow install failed" in body


@pytest.mark.asyncio
async def test_finalize_stream_done_preserves_post_commit_dispatch_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.api.routers import workspace_drafts
    from packages.core.services import workspace_setup_service

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

    class FakeLease:
        async def wrap(self, source, **_watchdog):
            async for chunk in source:
                yield chunk

    hydrated = SimpleNamespace(model_dump=lambda mode: {"id": "draft", "status": "finalized"})
    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    monkeypatch.setattr(
        workspace_drafts.draft_service,
        "finalize_draft",
        AsyncMock(return_value=("workspace", SimpleNamespace())),
    )
    monkeypatch.setattr(workspace_drafts, "_hydrate_response", AsyncMock(return_value=hydrated))
    monkeypatch.setattr(
        workspace_setup_service,
        "dispatch_workspace_post_commit",
        AsyncMock(side_effect=RuntimeError("strategist queue unavailable")),
    )
    monkeypatch.setattr(
        workspace_setup_service,
        "record_workspace_post_commit_dispatch_failure",
        AsyncMock(),
    )
    monkeypatch.setattr(
        workspace_drafts,
        "acquire_chat_stream_lease",
        AsyncMock(return_value=FakeLease()),
    )

    response = await workspace_drafts.finalize_stream(
        "draft",
        SimpleNamespace(entity_id="entity", id="creator"),
        db=AsyncMock(),
    )
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "event: dispatch_warning" in body
    assert "event: done" in body
    assert '"dispatch_warning": "workspace_startup_dispatch_failed"' in body
    assert "strategist queue unavailable" not in body
    assert '"strategist_eta_seconds": null' in body


async def _committed_draft(db_session, **fields):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft

    draft = WorkspaceDraft(
        entity_id=generate_ulid(), user_id=generate_ulid(), status="active",
        fields=dict(fields), messages=[], missing=[], ready=False,
    )
    db_session.add(draft)
    await db_session.commit()
    return draft


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", ["row_lock", "running_turn"])
async def test_a_message_to_a_busy_draft_is_a_409_not_a_500(db_session, monkeypatch, holder):
    """Regression (staging 2026-11-01): a message sent while the opening turn
    ran hit lock_timeout in claim() and surfaced as an unexpected 500."""
    import time

    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from apps.api.routers import workspace_drafts
    from packages.core.services import workspace_draft_service

    claim = {"id": "other-turn", "expires_at": time.time() + 300}
    draft = await _committed_draft(
        db_session, **({workspace_draft_service.TURN_CLAIM_FIELD: claim} if holder == "running_turn" else {}),
    )
    architect = AsyncMock(return_value="unused")
    monkeypatch.setattr(workspace_draft_service, "_architect_turn", architect)
    user = SimpleNamespace(id=draft.user_id, entity_id=draft.entity_id)
    request = SimpleNamespace(state=SimpleNamespace(auth_claims={}))
    async with AsyncSession(db_session.bind, expire_on_commit=False) as holder_db, \
            AsyncSession(db_session.bind, expire_on_commit=False) as db:
        if holder == "row_lock":
            await workspace_draft_service.get_draft(
                holder_db, draft.id, draft.entity_id, draft.user_id, for_update=True,
            )
            await db.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(HTTPException) as caught:
            await workspace_drafts.post_draft_message(
                draft.id, workspace_drafts.DraftMessageRequest(message="Add a weekly report"),
                request, user=user, db=db,
            )
        await holder_db.rollback()

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "draft_busy"
    assert caught.value.headers["Retry-After"] == str(workspace_draft_service.DRAFT_BUSY_RETRY_SECONDS)
    architect.assert_not_awaited()
    await db_session.refresh(draft)
    assert draft.messages == []
    # Another turn's claim is left alone.
    assert (draft.fields.get(workspace_draft_service.TURN_CLAIM_FIELD) == claim) is (holder == "running_turn")


@pytest.mark.asyncio
async def test_synchronous_create_holds_a_claim_not_the_row_and_is_retryable(db_session, monkeypatch):
    """POST /workspace-drafts runs its opening turn the way /stream does: the
    shell is committed first, the turn owns a claim, so the draft reads as
    working rather than locked. A retry with the same draft_id replays it."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from apps.api.routers import workspace_drafts
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_draft_service

    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(workspace_drafts, "async_session", sessions)
    monkeypatch.setattr(workspace_draft_service, "_refresh_missing_from_lint", AsyncMock())
    seen: dict = {}

    async def architect(db, *, draft, **kwargs):
        # Mid-turn, another connection reads the draft without waiting on a lock.
        async with sessions() as other:
            row = (await other.execute(
                select(WorkspaceDraft).where(WorkspaceDraft.id == draft.id).with_for_update(nowait=True)
            )).scalar_one()
            seen["working"] = (await workspace_drafts._hydrate_response(other, row)).working
            await other.rollback()
        seen["mcp_client"] = kwargs.get("mcp_client", False)
        return "Who orders from the bakery?"

    monkeypatch.setattr(workspace_draft_service, "_architect_turn", architect)
    entity_id, user_id, draft_id = generate_ulid(), generate_ulid(), generate_ulid()
    user = SimpleNamespace(id=user_id, entity_id=entity_id)
    request = SimpleNamespace(state=SimpleNamespace(auth_claims={"act": {"sub": "mcp:claude-code"}}))
    req = workspace_drafts.StartDraftRequest(initial_brief="A bakery", draft_id=draft_id)

    for _ in range(2):
        async with sessions() as db:
            response = await workspace_drafts.create_draft(req, request, _gate=None, user=user, db=db)
        assert response.reply == "Who orders from the bakery?"
        assert response.draft.id == draft_id and response.draft.working is False

    assert seen == {"working": True, "mcp_client": True}
    async with sessions() as db:
        stored = await db.get(WorkspaceDraft, draft_id)
    assert [m["role"] for m in stored.messages] == ["user", "assistant"]  # the retry replayed, no second turn
    assert workspace_draft_service.TURN_CLAIM_FIELD not in stored.fields


@pytest.mark.asyncio
async def test_a_turn_whose_process_died_is_answered_by_the_recovery_sweep(db_session, monkeypatch):
    """Regression (staging 2026-11-02): a release restarted the pod mid-turn;
    the draft kept no message, reported not working and never got a reply.
    The message is saved before the turn, a live turn keeps its claim alive,
    and once the claim expires the sweep runs the turn again."""
    import time

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from apps.api.routers import workspace_drafts
    from packages.core import database
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_draft_service as drafts
    from packages.core.tasks import workspace_draft_tasks

    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", sessions)
    monkeypatch.setattr(workspace_draft_tasks, "async_session", sessions)
    monkeypatch.setattr(drafts, "_refresh_missing_from_lint", AsyncMock())
    monkeypatch.setattr(drafts, "TURN_CLAIM_RENEW_SECONDS", 0.05)
    # The process dies: none of the turn's cleanup runs.
    monkeypatch.setattr(drafts.DraftTurn, "abandon", AsyncMock())
    draft = await _committed_draft(db_session)

    async def stored() -> WorkspaceDraft:
        async with sessions() as db:
            return await db.get(WorkspaceDraft, draft.id)

    class PodRestarted(Exception):
        pass

    claim_renewed = []

    async def dying_turn(db, *, draft, **kwargs):
        before = (await stored()).fields[drafts.TURN_CLAIM_FIELD]["expires_at"]
        await asyncio.sleep(0.3)
        claim_renewed.append((await stored()).fields[drafts.TURN_CLAIM_FIELD]["expires_at"] > before)
        raise PodRestarted()

    monkeypatch.setattr(drafts, "_architect_turn", dying_turn)
    user_message = {"role": "user", "content": "Add a weekly report"}
    staff_request_text = "Set up a weekly report and have Amy review it."
    stored_user_message = {**user_message, STAFF_REQUEST_TEXT_FIELD: staff_request_text}
    async with sessions() as db:
        with pytest.raises(PodRestarted):
            await drafts.process_draft_message(
                db, draft_id=draft.id, entity_id=draft.entity_id, user_id=draft.user_id,
                user_message=user_message["content"], staff_request_text=staff_request_text,
            )
    assert claim_renewed == [True]

    lost = await stored()
    assert lost.messages == [stored_user_message]

    handed_off: list[str] = []
    monkeypatch.setattr(workspace_draft_tasks, "enqueue_draft_turn_recovery", handed_off.append)
    # A claim that has not expired may belong to a live turn: left alone.
    await workspace_draft_tasks.recover_draft_turns()
    assert draft.id not in handed_off
    expired = time.time() + drafts.TURN_CLAIM_TTL_SECONDS + 1
    await workspace_draft_tasks.recover_draft_turns(now=expired)
    await workspace_draft_tasks.recover_draft_turns(now=expired + 1)
    assert handed_off.count(draft.id) == 1  # handed off once

    # From here on the dead turn's claim has expired.
    monkeypatch.setattr(drafts, "_turn_claim_active", lambda claim, now: False)
    async with sessions() as db:
        assert (await workspace_drafts._hydrate_response(db, await stored())).working is True

    seen: dict = {}

    async def recovered_turn(db, *, draft, **kwargs):
        seen.update(user_message=kwargs["user_message"], history=list(draft.messages)[:kwargs["history_len"]])
        return "Added a weekly report to the plan."

    monkeypatch.setattr(drafts, "_architect_turn", recovered_turn)
    assert await workspace_draft_tasks.resume_draft_turn(draft.id) is True

    assert seen == {"user_message": "Add a weekly report", "history": []}
    answered = await stored()
    assert answered.messages == [
        stored_user_message,
        {"role": "assistant", "content": "Added a weekly report to the plan.", "recovered": True},
    ]
    assert drafts.TURN_REQUEST_FIELD not in answered.fields
    assert drafts.TURN_CLAIM_FIELD not in answered.fields
    async with sessions() as db:
        response = await workspace_drafts._hydrate_response(db, answered)
    assert response.working is False
    assert response.messages[0] == user_message


@pytest.mark.asyncio
async def test_a_stopped_turn_is_answered_once_however_its_sender_resends(db_session, monkeypatch):
    """Regression (jr acceptance 2026-11-04): a stream was told "send your message
    again" while recovery was answering it. A stopped turn tells its client it
    continues, and sending the message again spends nothing more."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from apps.api.routers import workspace_drafts
    from packages.core import database
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_draft_service as drafts
    from packages.core.tasks import workspace_draft_tasks

    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", sessions)
    monkeypatch.setattr(workspace_drafts, "async_session", sessions)
    monkeypatch.setattr(workspace_draft_tasks, "async_session", sessions)
    monkeypatch.setattr(drafts, "_refresh_missing_from_lint", AsyncMock())
    draft = await _committed_draft(db_session)
    owner = {"draft_id": draft.id, "entity_id": draft.entity_id, "user_id": draft.user_id}
    handed_off: list[str] = []
    monkeypatch.setattr(workspace_draft_tasks, "enqueue_draft_turn_recovery", handed_off.append)
    started = asyncio.Event()

    async def hanging_turn(db, *, draft, **kwargs):
        started.set()
        await asyncio.sleep(60)

    # What the stall watchdog and a dropped connection do: close the stream. The
    # turn stops, and the sweep runs its saved request again with nobody listening.
    monkeypatch.setattr(drafts, "_architect_turn", hanging_turn)
    stream = workspace_drafts._stream_turn(**owner, user_message="Add a weekly report", mode="message")
    next_frame = asyncio.ensure_future(anext(stream))
    await next_frame
    keepalive_or_end = asyncio.ensure_future(anext(stream))
    await asyncio.wait_for(started.wait(), timeout=5)
    keepalive_or_end.cancel()
    await asyncio.gather(keepalive_or_end, return_exceptions=True)
    await stream.aclose()
    await workspace_draft_tasks.recover_draft_turns()
    assert handed_off.count(draft.id) == 1

    async def failing_turn(db, *, draft, **kwargs):
        raise RuntimeError("provider connection reset")

    monkeypatch.setattr(drafts, "_architect_turn", failing_turn)
    frames = [frame async for frame in workspace_drafts._stream_turn(
        **owner, user_message="Add a weekly report", mode="message",
    )]
    # Saved and run again: the person is not sent to resend, or shown the fault.
    assert frames[-1].startswith("event: continuing\n") and "provider connection" not in "".join(frames)

    # A message that never got saved has nothing running for it.
    frames = [frame async for frame in workspace_drafts._stream_turn(
        **{**owner, "draft_id": "no-such-draft"}, user_message="Add a weekly report", mode="message",
    )]
    assert frames[-1].startswith("event: error\n")

    spent = []

    async def answering_turn(db, *, draft, **kwargs):
        spent.append(kwargs["user_message"])
        return "Added a weekly report to the plan."

    monkeypatch.setattr(drafts, "_architect_turn", answering_turn)
    assert await workspace_draft_tasks.resume_draft_turn(draft.id) is True
    assert spent == ["Add a weekly report"]

    async def send(message: str) -> str:
        async with sessions() as db:
            reply, _ = await drafts.process_draft_message(db, **owner, user_message=message)
            await db.commit()
            return reply

    # The resend that waited out the recovery gets its answer, for free.
    assert await send("Add a weekly report") == "Added a weekly report to the plan."
    assert spent == ["Add a weekly report"]
    # Only once: the same words sent after that answer are a new request.
    await send("Add a weekly report")
    assert spent == ["Add a weekly report", "Add a weekly report"]
    async with sessions() as db:
        stored = await db.get(WorkspaceDraft, draft.id)
    assert [message["role"] for message in stored.messages] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_a_stream_gives_up_waiting_for_another_turn_before_it_counts_as_stalled(monkeypatch):
    """A stall frame says the turn continues, so it must only ever find a saved message."""
    from apps.api.routers import workspace_drafts

    seen: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def fake_process_message(_db, **kwargs):
        seen.update(kwargs)
        raise ValueError("busy")

    async def not_continuing(_db, **_kwargs):
        return False

    monkeypatch.setattr(workspace_drafts, "async_session", FakeSession)
    monkeypatch.setattr(workspace_drafts.draft_service, "process_draft_message", fake_process_message)
    monkeypatch.setattr(workspace_drafts.draft_service, "turn_continues", not_continuing)
    frames = [frame async for frame in workspace_drafts._stream_turn(
        entity_id="e", user_id="u", draft_id="d", user_message="hi", mode="message",
    )]
    assert frames[-1].startswith("event: error\n")
    assert 0 < seen["claim_wait_seconds"] < workspace_drafts._draft_turn_stall_seconds()


@pytest.mark.asyncio
async def test_a_lost_turn_nobody_waited_on_is_left_for_the_user_to_continue(db_session, monkeypatch):
    """Regression (2026-11-03): a turn lost when a machine slept was run again
    2.5 hours later, spending the user's BYOK tokens with nobody watching.
    Past the age limit it is not run again, the draft stops reporting working,
    and the user's next message continues from the same conversation."""
    import time

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from apps.api.routers import workspace_drafts
    from packages.core import database
    from packages.core.models.workspace_draft import WorkspaceDraft
    from packages.core.services import workspace_draft_service as drafts
    from packages.core.tasks import workspace_draft_tasks

    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", sessions)
    monkeypatch.setattr(workspace_draft_tasks, "async_session", sessions)
    monkeypatch.setattr(drafts, "_refresh_missing_from_lint", AsyncMock())
    architect = AsyncMock(return_value="Picked it up.")
    monkeypatch.setattr(drafts, "_architect_turn", architect)
    handed_off: list[str] = []
    monkeypatch.setattr(workspace_draft_tasks, "enqueue_draft_turn_recovery", handed_off.append)
    too_old = time.time() - drafts.TURN_RECOVERY_MAX_AGE_SECONDS - 1
    in_time = time.time() - drafts.TURN_RECOVERY_MAX_AGE_SECONDS + 60

    async def lost_draft(requested_at: float, *, claim_expires_at: float) -> WorkspaceDraft:
        draft = await _committed_draft(db_session, **{
            drafts.TURN_REQUEST_FIELD: {
                "id": "lost", "message": "Add a weekly report", "history_len": 0,
                "requested_at": requested_at, "recoveries": 0,
            },
            drafts.TURN_CLAIM_FIELD: {"id": "lost-claim", "expires_at": claim_expires_at},
        })
        draft.messages = [{"role": "user", "content": "Add a weekly report"}]
        await db_session.commit()
        return draft

    async def stored(draft_id: str) -> WorkspaceDraft:
        async with sessions() as db:
            return await db.get(WorkspaceDraft, draft_id)

    async def working(draft_id: str) -> bool:
        async with sessions() as db:
            return (await workspace_drafts._hydrate_response(db, await stored(draft_id))).working

    expired_claim = time.time() - 1
    old = await lost_draft(too_old, claim_expires_at=expired_claim)
    queued = await lost_draft(too_old, claim_expires_at=expired_claim)
    fresh = await lost_draft(in_time, claim_expires_at=expired_claim)
    # A turn that is still alive is not lost, however long ago it was asked.
    running = await lost_draft(too_old, claim_expires_at=time.time() + 300)

    # Nothing spins even if no sweep runs (e.g. the environment just woke up).
    assert await working(old.id) is False
    assert await working(fresh.id) is True
    assert await working(running.id) is True

    # A recovery already in the queue when the limit passed does not run either.
    assert await workspace_draft_tasks.resume_draft_turn(queued.id) is False
    architect.assert_not_awaited()
    assert drafts.TURN_REQUEST_FIELD not in (await stored(queued.id)).fields
    assert drafts.TURN_CLAIM_FIELD not in (await stored(queued.id)).fields

    await workspace_draft_tasks.recover_draft_turns()
    assert fresh.id in handed_off
    assert old.id not in handed_off and running.id not in handed_off
    architect.assert_not_awaited()
    assert drafts.TURN_REQUEST_FIELD in (await stored(running.id)).fields
    assert (await stored(running.id)).fields[drafts.TURN_CLAIM_FIELD]["id"] == "lost-claim"
    expired = await stored(old.id)
    # A process that wakes after this finds its claim gone and stops at its next write.
    assert drafts.TURN_REQUEST_FIELD not in expired.fields
    assert drafts.TURN_CLAIM_FIELD not in expired.fields
    assert expired.messages == [{"role": "user", "content": "Add a weekly report"}]

    # Inside the limit the lost turn still recovers by itself.
    assert await workspace_draft_tasks.resume_draft_turn(fresh.id) is True
    assert (await stored(fresh.id)).messages[-1] == {"role": "assistant", "content": "Picked it up.", "recovered": True}

    # The user's next message is a new turn that sees the unanswered one.
    architect.reset_mock()
    async with sessions() as db:
        reply, _ = await drafts.process_draft_message(
            db, draft_id=old.id, entity_id=old.entity_id, user_id=old.user_id, user_message="Please continue",
        )
        await db.commit()
    assert reply == "Picked it up."
    assert architect.await_args.kwargs["history_len"] == 1
    assert [m["content"] for m in (await stored(old.id)).messages] == [
        "Add a weekly report", "Please continue", "Picked it up.",
    ]
    assert await working(old.id) is False
