from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime import (
    AIRuntimeRequest,
    ChatSurface,
    RuntimeApprovalDecision,
    RuntimeHarness,
    RuntimeResolver,
    RuntimeToolContextConflictError,
    runtime_execute_registered_tool,
    runtime_prepare_tool_execution,
    runtime_tool_call_context_from_kwargs,
    runtime_workspace_search,
)
from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_handler,
)


pytestmark = pytest.mark.unit


def _workspace_envelope():
    return RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="ent_1",
            user_id="user_1",
            agent_id="agent_1",
            workspace_id="ws_1",
            conversation_id="conv_1",
            task_id="task_1",
        ),
        tool_schemas=[{"type": "function", "function": {"name": "workspace_search"}}],
        allowed_tool_names={"workspace_search"},
    )


def test_runtime_tool_context_factory_recovers_scope_and_rejects_conflicts() -> None:
    envelope = _workspace_envelope()

    context = runtime_tool_call_context_from_kwargs({}, runtime_envelope=envelope)

    assert context.entity_id == "ent_1"
    assert context.user_id == "user_1"
    assert context.agent_id == "agent_1"
    assert context.workspace_id == "ws_1"
    assert context.conversation_id == "conv_1"
    assert context.task_id == "task_1"

    with pytest.raises(RuntimeToolContextConflictError, match="workspace_id"):
        runtime_tool_call_context_from_kwargs(
            {"workspace_id": "ws_2"},
            runtime_envelope=envelope,
        )


def test_runtime_handler_context_uses_direct_identity_only_without_injection() -> None:
    direct = runtime_tool_call_context_from_handler({}, user_id="direct_user")
    injected = runtime_tool_call_context_from_handler(
        {"_user_id_from_context": "trusted_user"},
        user_id="forged_user",
    )
    anonymous = runtime_tool_call_context_from_handler(
        {"_user_id_from_context": None},
        user_id="forged_user",
    )

    assert direct.user_id == "direct_user"
    assert injected.user_id == "trusted_user"
    assert anonymous.user_id is None


@pytest.mark.asyncio
async def test_runtime_prepare_tool_execution_recovers_scope_from_envelope(monkeypatch) -> None:
    async def allow_tool_request(self, request):
        return RuntimeApprovalDecision.allow(
            self.approval_middleware.classify_request(request)
        )

    monkeypatch.setattr(RuntimeHarness, "guard_tool_request", allow_tool_request)

    prepared = await runtime_prepare_tool_execution(
        tool_name="workspace_search",
        arguments={"query": "customer"},
        runtime_envelope=_workspace_envelope(),
    )

    context = runtime_tool_call_context_from_kwargs(prepared.arguments)
    assert not prepared.blocked
    assert context.entity_id == "ent_1"
    assert context.user_id == "user_1"
    assert context.agent_id == "agent_1"
    assert context.workspace_id == "ws_1"
    assert context.conversation_id == "conv_1"
    assert context.task_id == "task_1"


@pytest.mark.asyncio
async def test_registered_tool_fails_closed_before_start_on_scope_conflict() -> None:
    envelope = _workspace_envelope()
    called = False

    def handler(**_kwargs) -> str:
        nonlocal called
        called = True
        return "unexpected"

    result = await runtime_execute_registered_tool(
        tool_name="workspace_search",
        arguments={"query": "customer", "workspace_id": "ws_2"},
        handler_resolver=lambda _name: handler,
        runtime_envelope=envelope,
    )

    assert result == "Error: Conflicting runtime scope for workspace_id."
    assert called is False
    assert envelope.metadata["runtime_events"] == [{
        "type": "error",
        "tool_name": "workspace_search",
        "message": "Conflicting runtime scope for workspace_id.",
    }]


@pytest.mark.asyncio
async def test_workspace_task_target_conflict_fails_before_approval(monkeypatch) -> None:
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="ent_1",
            user_id="user_1",
            agent_id="agent_1",
            workspace_id="ws_1",
            conversation_id="conv_1",
            task_id="task_1",
        ),
        tool_schemas=[{"type": "function", "function": {"name": "manor"}}],
        allowed_tool_names={"manor"},
    )
    approval_called = False
    handler_called = False

    async def allow_tool_request(self, request):
        nonlocal approval_called
        approval_called = True
        return RuntimeApprovalDecision.allow(
            self.approval_middleware.classify_request(request)
        )

    def handler(**_kwargs) -> str:
        nonlocal handler_called
        handler_called = True
        return "unexpected"

    monkeypatch.setattr(RuntimeHarness, "guard_tool_request", allow_tool_request)

    result = await runtime_execute_registered_tool(
        tool_name="manor",
        arguments={
            "action": "workspace",
            "params": {
                "action": "update_task_runtime",
                "params": {"task_id": "task_2", "runtime_instructions": "forged"},
            },
        },
        handler_resolver=lambda _name: handler,
        runtime_envelope=envelope,
    )

    assert result == "Error: Conflicting runtime scope for task_id."
    assert approval_called is False
    assert handler_called is False
    assert envelope.metadata["runtime_events"] == [{
        "type": "error",
        "tool_name": "manor",
        "message": "Conflicting runtime scope for task_id.",
    }]


@pytest.mark.asyncio
async def test_workspace_search_recovers_entity_scope_from_runtime_envelope(monkeypatch) -> None:
    from packages.core import database
    from packages.core.workspace_chat import context as workspace_context

    calls: list[dict[str, object]] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def fake_workspace_search(db, workspace_id, entity_id, **kwargs):
        calls.append({
            "db": db,
            "workspace_id": workspace_id,
            "entity_id": entity_id,
            **kwargs,
        })
        return "workspace result"

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(workspace_context, "workspace_search", fake_workspace_search)

    result = await runtime_workspace_search(
        workspace_id="ws_1",
        query="approved post",
        _runtime_envelope_from_context=_workspace_envelope(),
    )

    assert result == "workspace result"
    assert calls[0]["workspace_id"] == "ws_1"
    assert calls[0]["entity_id"] == "ent_1"
    assert calls[0]["query"] == "approved post"

    missing_entity = json.loads(await runtime_workspace_search(
        workspace_id="ws_1",
        query="approved post",
    ))
    assert missing_entity == {"error": "No entity context — workspace search cannot run."}
