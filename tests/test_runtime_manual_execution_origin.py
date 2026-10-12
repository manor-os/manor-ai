from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.execution_origin import runtime_tool_is_user_requested
from packages.core.ai.runtime.profiles import RuntimeProfile


def _context(profile, **kwargs):
    return {
        "_runtime_envelope_from_context": SimpleNamespace(profile=profile, task_id=kwargs.get("task_id")),
        **kwargs,
    }


async def _is_manual(kwargs, parent=None):
    db = SimpleNamespace(get=AsyncMock(return_value=parent))
    return await runtime_tool_is_user_requested(
        db, entity_id="entity", workspace_id="workspace", kwargs=kwargs,
    )


@pytest.mark.parametrize("profile", [
    RuntimeProfile.OWNER_COPILOT, RuntimeProfile.WORKSPACE_OPERATOR,
    RuntimeProfile.TASK_WORKER_FEEDBACK, RuntimeProfile.AGENT_DELEGATE,
])
async def test_foreground_runtime_turn_is_user_requested(profile):
    assert await _is_manual(_context(profile))


@pytest.mark.parametrize("kwargs", [
    {},
    {"user_id": "owner", "user_requested": True, "trigger_source": "global_chat"},
    _context("unknown", _active_user_message_from_context="run now"),
    _context(RuntimeProfile.BACKGROUND_WORKER, _active_user_message_from_context="old request"),
    _context(RuntimeProfile.WORKFLOW_STEP),
])
async def test_attribution_or_model_parameters_cannot_create_manual_origin(kwargs):
    assert not await _is_manual(kwargs)


@pytest.mark.parametrize("entity_id,workspace_id,details,expected", [
    ("entity", "workspace", {"_manual_execution": {"user_id": "owner", "action": "start"}}, True),
    ("entity", "workspace", {"scheduled_job_id": "job"}, False),
    ("foreign", "workspace", {"_manual_execution": {"user_id": "owner", "action": "start"}}, False),
    ("entity", "foreign", {"_manual_execution": {"user_id": "owner", "action": "start"}}, False),
])
async def test_background_task_inherits_only_exact_manual_parent(entity_id, workspace_id, details, expected):
    task = SimpleNamespace(entity_id=entity_id, workspace_id=workspace_id, creator_id="owner", details=details)
    assert await _is_manual(_context(RuntimeProfile.BACKGROUND_WORKER, task_id="task"), task) is expected


@pytest.mark.parametrize("entity_id,workspace_id,started_by,trigger_data,expected", [
    ("entity", "workspace", "owner", {"_workspace_user_request": {"user_id": "owner"}}, True),
    ("entity", "workspace", "owner", {}, False),
    ("entity", "workspace", "owner", {"_workspace_user_request": {"user_id": "other"}}, False),
    ("entity", "other", "owner", {"_workspace_user_request": {"user_id": "owner"}}, False),
    ("other", "workspace", "owner", {"_workspace_user_request": {"user_id": "owner"}}, False),
])
async def test_innermost_workflow_owns_background_lineage(entity_id, workspace_id, started_by, trigger_data, expected):
    run = SimpleNamespace(entity_id=entity_id, workspace_id=workspace_id, started_by=started_by, trigger_data=trigger_data)
    assert await _is_manual(_context(
        RuntimeProfile.WORKFLOW_STEP, task_id="manual-task", _workflow_run_id_from_context="run",
    ), run) is expected


async def test_background_envelope_cannot_borrow_another_workspace_parent():
    task = SimpleNamespace(
        entity_id="entity", workspace_id="workspace", creator_id="owner",
        details={"_manual_execution": {"user_id": "owner", "action": "start"}},
    )
    assert not await _is_manual(_context(
        RuntimeProfile.BACKGROUND_WORKER, workspace_id="other", task_id="task",
    ), task)


async def test_public_task_target_cannot_become_background_parent():
    task = SimpleNamespace(
        entity_id="entity", workspace_id="workspace", creator_id="owner", details={},
    )
    assert not await _is_manual({
        "_runtime_envelope_from_context": SimpleNamespace(profile="background_worker", task_id=None),
        "task_id": "someone-elses-manual-task",
    }, task)


@pytest.mark.parametrize("tool_name", [
    "workspace_agent", "workspace_create_task", "workspace_request_strategist_review",
    "update_task", "run_scheduled_job_now", "manor", "start_workspace_flow", "run_workflow",
])
@pytest.mark.parametrize("background", [False, True])
async def test_real_tool_preparation_preserves_origin_for_all_manual_entry_aliases(
    monkeypatch, tool_name, background,
):
    from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.resolver import RuntimeResolver
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_execution import runtime_prepare_tool_execution

    async def allow_approval(self, request):
        return RuntimeApprovalDecision.allow(self.classify_request(request))

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", allow_approval)
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.SCHEDULED_AGENT_RUN if background else ChatSurface.WORKSPACE_CHAT,
            entity_id="entity", user_id="owner", workspace_id="workspace",
            task_id="automatic-task" if background else None,
        ),
        tool_schemas=[{"type": "function", "function": {"name": tool_name}}],
        allowed_tool_names={tool_name},
    )
    arguments = {
        "workspace_agent": {"action": "search", "params": {}},
        "manor": {"action": "workspace", "params": {"action": "search", "params": {}}},
    }.get(tool_name, {})
    prepared = await runtime_prepare_tool_execution(
        tool_name=tool_name, arguments=arguments, entity_id="entity", user_id="owner",
        workspace_id="workspace", task_id=envelope.task_id, runtime_envelope=envelope,
        allowed_tool_names={tool_name},
    )
    assert not prepared.blocked, prepared.blocked_result
    assert prepared.arguments["_runtime_envelope_from_context"] is envelope
    assert await _is_manual(prepared.arguments) is (not background)
