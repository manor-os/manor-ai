from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _runtime_envelope(*, mode: str, surface: str = "global_owner_chat"):
    return SimpleNamespace(
        surface=SimpleNamespace(value=surface),
        profile="owner_copilot",
        metadata={"chat_mode": mode},
    )


def _flow_row(*, binding_id: str, workspace_id: str, workspace_name: str):
    entrypoint = SimpleNamespace(
        title="Create product video",
        description="Create a product video from a browser workflow.",
        run_inputs=(),
    )
    binding = SimpleNamespace(
        id=binding_id,
        config={"workspace_blueprint_workflow_slug": "create-product-video-v1"},
    )
    workflow = SimpleNamespace(id=f"workflow-{binding_id}", name="Create product video")
    workspace = SimpleNamespace(id=workspace_id, name=workspace_name)
    return entrypoint, binding, workflow, workspace


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "origin", "origin_data", "scope", "expected"),
    [
        ("workspace_operator", None, {}, "same", True),
        (None, None, {}, "same", False),
        ("background_worker", None, {}, "same", False),
        ("workflow_step", None, {}, "same", False),
        ("background_worker", "task", {}, "same", True),
        ("background_worker", "task", {"scheduled_job_id": "job-a"}, "same", False),
        ("background_worker", "task", {
            "scheduled_job_id": "job-a",
            "_manual_execution": {"user_id": "user-a", "action": "retry"},
        }, "same", True),
        ("background_worker", "task", {}, "foreign_workspace", False),
        ("background_worker", "task", {}, "foreign_entity", False),
        ("workflow_step", "workflow", {}, "same", False),
        ("workflow_step", "workflow", {
            "_workspace_user_request": {"user_id": "user-a"},
        }, "same", True),
        ("workflow_step", "workflow", {
            "_workspace_user_request": {"user_id": "other-user"},
        }, "same", False),
        ("workflow_step", "workflow", {
            "_workspace_user_request": {"user_id": "user-a"},
        }, "foreign_workspace", False),
        ("workflow_step", "workflow", {
            "_workspace_user_request": {"user_id": "user-a"},
        }, "foreign_entity", False),
    ],
)
async def test_workflow_tool_manual_origin_requires_trusted_runtime_lineage(
    profile, origin, origin_data, scope, expected,
) -> None:
    from packages.core.ai.tools.workflow_tools import _workflow_tool_user_requested

    ancestor = SimpleNamespace(
        entity_id="other-entity" if scope == "foreign_entity" else "entity-a",
        workspace_id="other-workspace" if scope == "foreign_workspace" else "workspace-a",
        creator_id="user-a",
        started_by="user-a",
        details=origin_data,
        trigger_data=origin_data,
    )
    db = SimpleNamespace(get=AsyncMock(return_value=ancestor))
    kwargs = {"_runtime_envelope_from_context": SimpleNamespace(
        profile=profile, task_id="task-a" if origin == "task" else None,
    )}
    if origin == "task":
        kwargs["task_id"] = "task-a"
    elif origin == "workflow":
        kwargs["_workflow_run_id_from_context"] = "run-a"

    assert await _workflow_tool_user_requested(
        db, entity_id="entity-a", workspace_id="workspace-a", kwargs=kwargs,
    ) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "surface", "origin", "receipt", "scope", "expected"),
    [
        ("owner_copilot", "global_owner_chat", None, False, "same", True),
        ("workspace_operator", "workspace_chat", None, False, "same", True),
        (None, "global_owner_chat", None, False, "same", False),
        ("unknown", "global_owner_chat", None, False, "same", False),
        ("background_worker", "scheduled_agent_run", None, False, "same", False),
        ("workflow_step", "workflow_agent_step", "workflow", False, "same", False),
        ("workflow_step", "global_owner_chat", "workflow", False, "same", False),
        ("background_worker", "scheduled_agent_run", "task", True, "same", True),
        ("workflow_step", "workflow_agent_step", "workflow", True, "same", True),
        ("workflow_step", "workflow_agent_step", "workflow", True, "foreign", False),
    ],
)
async def test_workspace_flow_start_does_not_promote_background_origin(
    monkeypatch, profile, surface, origin, receipt, scope, expected,
) -> None:
    from packages.core.ai.tools import workflow_tools

    row = _flow_row(
        binding_id="binding-a", workspace_id="workspace-a", workspace_name="Studio",
    )
    ancestor = SimpleNamespace(
        entity_id="entity-a",
        workspace_id="other-workspace" if scope == "foreign" else "workspace-a",
        creator_id="user-a",
        started_by="user-a",
        details={
            "scheduled_job_id": "job-a",
            **({"_manual_execution": {"user_id": "user-a", "action": "retry"}} if receipt else {}),
        },
        trigger_data={"_workspace_user_request": {"user_id": "user-a"}} if receipt else {},
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=ancestor),
        execute=AsyncMock(side_effect=[
            _ScalarResult(SimpleNamespace(id="conversation-a", user_id="user-a")),
            _ScalarResult("old-user-message"),
        ]),
    )
    launch = AsyncMock(return_value=SimpleNamespace(created=True, run=SimpleNamespace(
        id="run-a", workflow_id=row[2].id, workspace_id="workspace-a", binding_id=row[1].id,
        trigger_source="mcp", status="running", current_step_id=None, error=None,
        started_at=None, completed_at=None, variables={}, step_results={}, trigger_data={},
    )))
    monkeypatch.setattr(workflow_tools, "async_session", lambda: _SessionContext(db))
    monkeypatch.setattr(workflow_tools, "_workspace_flow_rows", AsyncMock(return_value=[row]))
    monkeypatch.setattr("packages.core.services.workspace_flow_launcher.launch_workspace_flow", launch)
    context = {
        "_runtime_envelope_from_context": SimpleNamespace(
            profile=profile, surface=surface, metadata={"chat_mode": "flows"},
            task_id="parent-task" if origin == "task" else None,
        ),
    }
    if origin == "workflow":
        context["_workflow_run_id_from_context"] = "parent-run"
    elif origin == "task":
        context["task_id"] = "parent-task"

    result = json.loads(await workflow_tools._start_workspace_flow(
        entity_id="entity-a", user_id="user-a", binding_id=row[1].id,
        conversation_id="conversation-a", source_brief="Start the next flow", **context,
    ))

    assert result["ok"] is True
    assert launch.await_args.kwargs["user_requested"] is expected
    assert launch.await_args.kwargs["source"] == (
        "global_chat" if expected and surface == "global_owner_chat" else "mcp"
    )
    # A historical user message is projection context, never a manual receipt.
    assert launch.await_args.kwargs["origin_message_id"] == "old-user-message"


@pytest.mark.asyncio
async def test_workspace_flow_rows_require_control_for_launch(monkeypatch) -> None:
    from packages.core.ai.tools import workflow_tools

    row = _flow_row(
        binding_id="binding-viewer",
        workspace_id="workspace-viewer",
        workspace_name="Viewer workspace",
    )
    user = SimpleNamespace(id="user-viewer", entity_id="entity-a", role="viewer")
    db = SimpleNamespace(
        get=AsyncMock(return_value=user),
        execute=AsyncMock(return_value=_RowsResult([row[1:]])),
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_workflow_router.normalize_chat_entrypoint",
        lambda _binding, _workflow: row[0],
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_write_workspace_artifacts",
        AsyncMock(return_value=False),
    )

    readable = await workflow_tools._workspace_flow_rows(
        db,
        entity_id="entity-a",
        user_id=user.id,
        require_control=False,
    )
    controllable = await workflow_tools._workspace_flow_rows(
        db,
        entity_id="entity-a",
        user_id=user.id,
        require_control=True,
    )

    assert len(readable) == 1
    assert controllable == []


@pytest.mark.asyncio
async def test_global_flow_start_rejects_binding_choice_when_duplicate_workspace_not_named(
    monkeypatch,
) -> None:
    from packages.core.ai.tools import workflow_tools

    rows = [
        _flow_row(
            binding_id="binding-alpha",
            workspace_id="workspace-alpha",
            workspace_name="Alpha Studio",
        ),
        _flow_row(
            binding_id="binding-beta",
            workspace_id="workspace-beta",
            workspace_name="Beta Studio",
        ),
    ]
    db = SimpleNamespace()
    monkeypatch.setattr(workflow_tools, "async_session", lambda: _SessionContext(db))
    monkeypatch.setattr(
        workflow_tools,
        "_workspace_flow_rows",
        AsyncMock(return_value=rows),
    )

    result = json.loads(await workflow_tools._start_workspace_flow(
        entity_id="entity-a",
        user_id="user-a",
        binding_id="binding-alpha",
        source_brief="Create a product video for our launch.",
        conversation_id="conversation-a",
        _active_user_message_from_context="Create a product video for our launch.",
        _runtime_envelope_from_context=_runtime_envelope(mode="flows"),
    ))

    assert result["ok"] is False
    assert result["error"] == "ambiguous_workflow"
    assert [item["workspace_name"] for item in result["candidates"]] == [
        "Alpha Studio",
        "Beta Studio",
    ]


@pytest.mark.asyncio
async def test_global_flow_start_rejects_target_only_disambiguation(
    monkeypatch,
) -> None:
    from packages.core.ai.tools import workflow_tools

    rows = [
        _flow_row(
            binding_id="binding-alpha",
            workspace_id="workspace-alpha",
            workspace_name="Alpha Studio",
        ),
        _flow_row(
            binding_id="binding-beta",
            workspace_id="workspace-beta",
            workspace_name="Beta Studio",
        ),
    ]
    monkeypatch.setattr(
        workflow_tools,
        "async_session",
        lambda: _SessionContext(SimpleNamespace()),
    )
    monkeypatch.setattr(
        workflow_tools,
        "_workspace_flow_rows",
        AsyncMock(return_value=rows),
    )

    result = json.loads(await workflow_tools._start_workspace_flow(
        entity_id="entity-a",
        user_id="user-a",
        flow="create-product-video-v1",
        target_workspace="Alpha Studio",
        source_brief="Create a product video for our launch.",
        conversation_id="conversation-a",
        _active_user_message_from_context="Create a product video for our launch.",
        _runtime_envelope_from_context=_runtime_envelope(mode="flows"),
    ))

    assert result["error"] == "ambiguous_workflow"


@pytest.mark.asyncio
async def test_global_flow_start_requires_a_flow_selector(monkeypatch) -> None:
    from packages.core.ai.tools import workflow_tools

    monkeypatch.setattr(
        workflow_tools,
        "async_session",
        lambda: _SessionContext(SimpleNamespace()),
    )
    monkeypatch.setattr(
        workflow_tools,
        "_workspace_flow_rows",
        AsyncMock(return_value=[_flow_row(
            binding_id="binding-only",
            workspace_id="workspace-only",
            workspace_name="Only Studio",
        )]),
    )

    result = json.loads(await workflow_tools._start_workspace_flow(
        entity_id="entity-a",
        user_id="user-a",
        conversation_id="conversation-a",
        _active_user_message_from_context="Run a Flow.",
        _runtime_envelope_from_context=_runtime_envelope(mode="flows"),
    ))

    assert result == {"ok": False, "error": "flow_required"}


@pytest.mark.asyncio
async def test_global_flow_start_preserves_original_user_message_as_source_brief(
    monkeypatch,
) -> None:
    from packages.core.ai.tools import workflow_tools

    row = _flow_row(
        binding_id="binding-only",
        workspace_id="workspace-only",
        workspace_name="Only Studio",
    )
    conversation = SimpleNamespace(
        id="conversation-a",
        entity_id="entity-a",
        user_id="user-a",
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[
            _ScalarResult(conversation),
            _ScalarResult("message-a"),
        ]),
    )
    launch = AsyncMock(return_value=SimpleNamespace(
        created=True,
        run=SimpleNamespace(
            id="run-a",
            workflow_id=row[2].id,
            workspace_id=row[3].id,
            binding_id=row[1].id,
            trigger_source="global_chat",
            status="paused",
            current_step_id=None,
            error=None,
            started_at=None,
            completed_at=None,
            variables={},
            step_results={},
            trigger_data={},
        ),
    ))
    monkeypatch.setattr(workflow_tools, "async_session", lambda: _SessionContext(db))
    monkeypatch.setattr(
        workflow_tools,
        "_workspace_flow_rows",
        AsyncMock(return_value=[row]),
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_flow_launcher.launch_workspace_flow",
        launch,
    )
    original = "Create a 60-second Chinese video and keep every listed constraint."

    result = json.loads(await workflow_tools._start_workspace_flow(
        entity_id="entity-a",
        user_id="user-a",
        binding_id=row[1].id,
        source_brief="Create a short video.",
        conversation_id=conversation.id,
        _active_user_message_from_context=original,
        _runtime_envelope_from_context=_runtime_envelope(mode="flows"),
    ))

    assert result["ok"] is True
    assert launch.await_args.kwargs["source_brief"] == original
    assert launch.await_args.kwargs["origin_message_id"] == "message-a"


def test_non_blueprint_flow_slug_uses_stable_definition_id() -> None:
    from packages.core.ai.tools.workflow_tools import _workspace_flow_descriptor

    entrypoint, binding, workflow, workspace = _flow_row(
        binding_id="binding-stable",
        workspace_id="workspace-stable",
        workspace_name="Stable Studio",
    )
    binding.config = {}
    workflow.name = "A mutable title"

    descriptor = _workspace_flow_descriptor(entrypoint, binding, workflow, workspace)

    assert descriptor["flow_slug"] == workflow.id


@pytest.mark.asyncio
async def test_global_auto_mode_cannot_start_workspace_flow() -> None:
    from packages.core.ai.tools.workflow_tools import _start_workspace_flow

    result = json.loads(await _start_workspace_flow(
        entity_id="entity-a",
        user_id="user-a",
        binding_id="binding-a",
        conversation_id="conversation-a",
        _runtime_envelope_from_context=_runtime_envelope(mode="auto"),
    ))

    assert result == {"ok": False, "error": "flows_mode_required"}


@pytest.mark.asyncio
async def test_global_chat_cannot_bypass_launcher_with_legacy_run_tool() -> None:
    from packages.core.ai.tools.workflow_tools import _run_workflow

    result = json.loads(await _run_workflow(
        entity_id="entity-a",
        user_id="user-a",
        workflow="Create product video",
        _runtime_envelope_from_context=_runtime_envelope(mode="flows"),
    ))

    assert result == {"ok": False, "error": "workspace_flow_launcher_required"}


@pytest.mark.asyncio
async def test_workspace_run_acl_distinguishes_read_from_control(monkeypatch) -> None:
    from packages.core.ai.tools import workflow_tools

    user = SimpleNamespace(id="user-viewer", entity_id="entity-a", role="viewer")
    run = SimpleNamespace(
        workspace_id="workspace-private",
        started_by="another-user",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=user))
    can_read = AsyncMock(return_value=True)
    can_control = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_id",
        can_read,
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_control_workspace_run",
        can_control,
    )

    assert await workflow_tools._user_can_read_workflow_run(
        db,
        run=run,
        entity_id="entity-a",
        user_id=user.id,
    ) is True
    assert await workflow_tools._user_can_control_workflow_run(
        db,
        run=run,
        entity_id="entity-a",
        user_id=user.id,
    ) is False


@pytest.mark.asyncio
async def test_revoked_run_starter_no_longer_retains_control(monkeypatch) -> None:
    from packages.core.services.workspace_access import user_can_control_workspace_run

    can_read = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "packages.core.services.workspace_access.user_can_read_workspace_id",
        can_read,
    )
    run = SimpleNamespace(
        entity_id="entity-a",
        workspace_id="workspace-revoked",
        started_by="user-revoked",
    )

    assert await user_can_control_workspace_run(
        SimpleNamespace(),
        run=run,
        user_id="user-revoked",
        entity_role="member",
    ) is False
    can_read.assert_awaited_once()
