from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest


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


def test_global_chat_allows_draft_start_for_explicit_create_intent():
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workspace_drafts import (
        _workspace_draft_start_allowed,
    )

    assert _workspace_draft_start_allowed(SimpleNamespace(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        metadata={
            "workspace_creation_authorization": {
                "status": "authorized",
                "confidence": 0.99,
            },
        },
    ))


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
async def test_workspace_draft_stream_releases_request_db_before_wrapping():
    from apps.api.routers import workspace_drafts

    lifecycle: list[str] = []

    class FakeDB:
        async def commit(self):
            lifecycle.append("commit")

        async def close(self):
            lifecycle.append("close")

    class FakeLease:
        def wrap(self, source):
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

        async def wrap(self, source):
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

        def wrap(self, stream):
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

        async def wrap(self, source):
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
    from packages.core.services import workspace_draft_service

    draft = SimpleNamespace(
        status="active",
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
        async def wrap(self, source):
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
        async def wrap(self, source):
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
        async def wrap(self, source):
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
