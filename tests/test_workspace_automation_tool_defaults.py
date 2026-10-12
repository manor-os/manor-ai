"""Workspace Chat automations must schedule an Agent, not a manual Task."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import AIRuntimeRequest, RuntimeResolver
from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_execution import runtime_prepare_tool_execution
from packages.core.ai.tools import cron_tools, manor_tool
from packages.core.constants.agents import MANOR_AGENT_ID


pytestmark = [pytest.mark.asyncio, pytest.mark.oss_regression]


def _envelope(surface: ChatSurface, *, agent_id: str | None = "agent-1") -> SimpleNamespace:
    return SimpleNamespace(
        surface=surface,
        workspace_id="workspace-1",
        conversation_id="conversation-1",
        user_id="user-1",
        agent_id=agent_id,
    )


async def test_runtime_injects_workspace_surface_before_scheduling(monkeypatch) -> None:
    calls = []

    async def allow_approval(self, request):
        return RuntimeApprovalDecision.allow(self.classify_request(request))

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return "created"

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", allow_approval)
    monkeypatch.setattr(cron_tools, "runtime_create_scheduled_job_action", fake_create)
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKSPACE_CHAT,
            entity_id="entity-1",
            user_id="user-1",
            agent_id="agent-1",
            workspace_id="workspace-1",
            conversation_id="conversation-1",
        ),
        tool_schemas=[cron_tools.get_tools()[0][0]],
        allowed_tool_names={"create_scheduled_job"},
    )
    prepared = await runtime_prepare_tool_execution(
        tool_name="create_scheduled_job",
        arguments={
            "name": "Daily scripts",
            "schedule_kind": "cron",
            "cron_expr": "0 9 * * *",
            "payload_message": "Create today's Reel script",
        },
        runtime_envelope=envelope,
    )

    assert not prepared.blocked
    assert prepared.arguments["_runtime_envelope_from_context"] is envelope
    assert await cron_tools._create_job_handler(entity_id="entity-1", **prepared.arguments) == "created"
    assert calls[0]["agent_id"] == ""
    assert calls[0]["caller_agent_id"] == "agent-1"
    assert calls[0]["workspace_id"] == "workspace-1"


async def test_runtime_scheduling_enforces_workspace_write_at_persistence(monkeypatch) -> None:
    from packages.core import database
    from packages.core.ai.runtime.scheduling import runtime_create_scheduled_job_action
    from packages.core.services import scheduler_service
    from packages.core.services import workspace_access

    calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def deny_workspace_write(_db, **kwargs):
        calls.append(kwargs)
        return False

    async def unexpected_create(*_args, **_kwargs):
        pytest.fail("An unwritable Workspace must not persist an automation")

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(workspace_access, "user_can_write_workspace_id", deny_workspace_write)
    monkeypatch.setattr(scheduler_service, "create_scheduled_job", unexpected_create)
    response = json.loads(await runtime_create_scheduled_job_action(
        entity_id="entity-1",
        name="Daily scripts",
        cron_expr="0 9 * * *",
        workspace_id="workspace-1",
        user_id="user-1",
        agent_id="agent-1",
    ))

    assert response["ok"] is False
    assert response["error"] == "Workspace not found or not writable"
    assert calls[0]["workspace_id"] == "workspace-1"
    assert calls[0]["user_id"] == "user-1"


@pytest.mark.parametrize(
    ("surface", "runtime_agent_id", "supplied_agent_id", "expected_caller_id"),
    [
        (ChatSurface.WORKSPACE_CHAT, "agent-1", "", "agent-1"),
        (ChatSurface.WORKSPACE_CHAT, None, "", MANOR_AGENT_ID),
        (ChatSurface.WORKSPACE_CHAT, "agent-1", "agent-2", "agent-1"),
        (ChatSurface.AGENT_DM, "agent-1", "", "agent-1"),
    ],
)
async def test_direct_scheduled_job_passes_requested_and_caller_agents_separately(
    monkeypatch, surface, runtime_agent_id, supplied_agent_id, expected_caller_id,
) -> None:
    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return "created"

    monkeypatch.setattr(cron_tools, "runtime_create_scheduled_job_action", fake_create)
    result = await cron_tools._create_job_handler(
        entity_id="entity-1",
        name="Daily scripts",
        payload_message="Create today's Reel script",
        agent_id=supplied_agent_id,
        _runtime_envelope_from_context=_envelope(surface, agent_id=runtime_agent_id),
    )

    assert result == "created"
    assert calls[0]["agent_id"] == supplied_agent_id
    assert calls[0]["caller_agent_id"] == expected_caller_id
    assert calls[0]["workspace_id"] == "workspace-1"


@pytest.mark.parametrize(
    ("surface", "supplied_agent_id", "expected_agent_id"),
    [
        (ChatSurface.WORKSPACE_CHAT, "", None),
        (ChatSurface.WORKSPACE_CHAT, "agent-2", "agent-2"),
        (ChatSurface.AGENT_DM, "", None),
    ],
)
async def test_composite_scheduled_job_passes_requested_and_caller_agents_separately(
    monkeypatch, surface, supplied_agent_id, expected_agent_id,
) -> None:
    from packages.core import database

    calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return "created"

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(manor_tool, "runtime_manor_create_scheduled_job", fake_create)
    params = {"name": "Daily scripts", "payload_message": "Create today's Reel script"}
    if supplied_agent_id:
        params["agent_id"] = supplied_agent_id
    result = await manor_tool._manor_handler(
        entity_id="entity-1",
        action="create_scheduled_job",
        params=params,
        _runtime_envelope_from_context=_envelope(surface),
    )

    assert result == "created"
    assert calls[0]["params"].get("agent_id") == expected_agent_id
    assert calls[0]["caller_agent_id"] == "agent-1"
    assert calls[0]["workspace_id"] == "workspace-1"
