from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool_action
from packages.core.ai.runtime.approval_messages import describe_runtime_approval_action
from packages.core.ai.runtime.approvals import (
    RuntimeApprovalRequest,
    runtime_capability_id_for_action_key,
)
from packages.core.ai.runtime.authorization_receipts import (
    WorkspaceFileAuthorizationResourceFactory,
)
from packages.core.ai.runtime.profiles import WORKSPACE_AGENT_TOOL_PROFILE
from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory, SandboxToolAction
from packages.core.ai.runtime.streams import runtime_tool_arguments_for_chat
from packages.core.ai.runtime.tool_registry import (
    runtime_registered_tool_surface_from_schemas,
)
from packages.core.ai.runtime.tool_visibility import eager_tool_names_for_profile
from packages.core.ai.runtime.tool_search import runtime_search_tool_registry_candidates
from packages.core.ai.tool_pool import ToolPool
from packages.core.ai.tools import cron_tools, manor_tool, sandbox_tools, workspace_agent_tools


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_tool_pool_keeps_undiscoverable_compatibility_aliases_executable() -> None:
    pool = ToolPool()

    def handler():
        return "ok"

    pool.register("canonical", _schema("canonical"), handler)
    pool.register(
        "legacy_alias",
        _schema("legacy_alias"),
        handler,
        discoverable=False,
    )

    assert pool.get("legacy_alias") is not None
    assert pool.registered_tool_names() == ("canonical",)
    assert pool.registered_tool_names(include_undiscoverable=True) == (
        "canonical",
        "legacy_alias",
    )
    assert tuple(name for name, _schema in pool.registered_tool_schemas()) == (
        "canonical",
    )



async def _allow_sandbox_access(**_kwargs):
    return None

@pytest.mark.asyncio
async def test_sandbox_composite_routes_action_and_context(monkeypatch) -> None:
    seen: dict = {}

    async def fake_exec(**kwargs):
        seen.update(kwargs)
        return "done"

    monkeypatch.setattr(sandbox_tools, "_sandbox_exec", fake_exec)

    result = await sandbox_tools._sandbox_handler(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        action="exec",
        params={
            "sandbox_id": "sandbox-1",
            "command": "pwd",
            "entity_id": "forged-entity",
            "user_id": "forged-user",
        },
        _agent_id_from_context="agent-1",
    )

    assert result == "done"
    assert seen["entity_id"] == "entity-1"
    assert seen["user_id"] == "user-1"
    assert seen["conversation_id"] == "conversation-1"
    assert seen["sandbox_id"] == "sandbox-1"
    assert seen["command"] == "pwd"
    assert seen["_agent_id_from_context"] == "agent-1"


@pytest.mark.asyncio
async def test_sandbox_composite_accepts_provider_flattened_action_params(
    monkeypatch,
) -> None:
    seen: dict = {}

    async def fake_read_file(**kwargs):
        seen.update(kwargs)
        return "done"

    monkeypatch.setattr(sandbox_tools, "_sandbox_read_file", fake_read_file)

    result = await sandbox_tools._sandbox_handler(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        action="read_file",
        sandbox_id="sandbox-1",
        path="/skill/SKILL.md",
    )

    assert result == "done"
    assert seen["sandbox_id"] == "sandbox-1"
    assert seen["path"] == "/skill/SKILL.md"


@pytest.mark.asyncio
async def test_sandbox_read_file_honors_provider_pagination(monkeypatch) -> None:
    class FakeClient:
        async def read_file(self, **_kwargs):
            return type(
                "ReadResult",
                (),
                {
                    "path": "/skill/SKILL.md",
                    "content": "abcdefghij",
                    "size": 10,
                    "truncated": False,
                },
            )()

        async def close(self):
            return None

    async def fake_client(_sandbox_id):
        return FakeClient()

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", fake_client)
    # Paging is under test, not the binding check (covered in
    # tests/test_sandbox_binding_scope.py).
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", _allow_sandbox_access)

    result = json.loads(
        await sandbox_tools._sandbox_read_file(
            sandbox_id="sandbox-1",
            path="/skill/SKILL.md",
            offset=3,
            limit=4,
        )
    )

    assert result["content"] == "defg"
    assert result["offset"] == 3
    assert result["returned_chars"] == 4
    assert result["next_offset"] == 7
    assert result["truncated"] is True
    assert "offset=7" in result["hint"]


@pytest.mark.asyncio
async def test_sandbox_read_file_pages_at_the_context_budget(monkeypatch) -> None:
    """A whole-file read of a 20 KB SKILL.md used to return 20 000 chars that
    the agentic loop then compacted to ~1 700 while next_offset still said
    20 000. One page is now sized to survive the context budget intact."""

    class FakeClient:
        async def read_file(self, **_kwargs):
            return type(
                "ReadResult",
                (),
                {"path": "/skill/SKILL.md", "content": "x" * 30_000, "size": 30_000, "truncated": False},
            )()

        async def close(self):
            return None

    async def fake_client(_sandbox_id):
        return FakeClient()

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", fake_client)
    # Paging is under test, not the binding check (covered in
    # tests/test_sandbox_binding_scope.py).
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", _allow_sandbox_access)

    whole = json.loads(await sandbox_tools._sandbox_read_file(sandbox_id="sandbox-1", path="/skill/SKILL.md"))
    assert whole["returned_chars"] == sandbox_tools.READ_FILE_PAGE_MAX_CHARS
    assert whole["next_offset"] == sandbox_tools.READ_FILE_PAGE_MAX_CHARS
    assert whole["truncated"] is True

    oversized = json.loads(
        await sandbox_tools._sandbox_read_file(sandbox_id="sandbox-1", path="/skill/SKILL.md", limit=50_000)
    )
    assert oversized["returned_chars"] == sandbox_tools.READ_FILE_PAGE_MAX_CHARS

    from packages.core.ai.agentic_loop import PAGED_TEXT_RESULT_MAX_CHARS, _compact_tool_result_for_context

    raw = await sandbox_tools._sandbox_read_file(sandbox_id="sandbox-1", path="/skill/SKILL.md")
    assert len(raw) <= PAGED_TEXT_RESULT_MAX_CHARS
    assert _compact_tool_result_for_context("sandbox", raw) == raw, "one page reaches the model uncut"


@pytest.mark.asyncio
async def test_sandbox_read_file_does_not_advertise_unservable_service_page(
    monkeypatch,
) -> None:
    class FakeClient:
        async def read_file(self, **_kwargs):
            return type(
                "ReadResult",
                (),
                {
                    "path": "/skill/SKILL.md",
                    "content": "service-capped",
                    "size": 100_000,
                    "truncated": True,
                },
            )()

        async def close(self):
            return None

    async def fake_client(_sandbox_id):
        return FakeClient()

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", fake_client)
    # Paging is under test, not the binding check (covered in
    # tests/test_sandbox_binding_scope.py).
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", _allow_sandbox_access)

    result = json.loads(
        await sandbox_tools._sandbox_read_file(
            sandbox_id="sandbox-1",
            path="/skill/SKILL.md",
        )
    )

    assert result["content"] == "service-capped"
    assert result["next_offset"] is None
    assert result["truncated"] is True
    assert "sandbox service read limit" in result["hint"]


def test_sandbox_flattened_params_share_governed_composite_normalization() -> None:
    normalized = RuntimeCompositeToolCallFactory.create(
        "sandbox",
        {
            "action": "exec",
            "sandbox_id": "flattened-sandbox",
            "command": "touch flattened.txt",
            "params": {
                "sandbox_id": "nested-sandbox",
                "command": "touch nested.txt",
            },
        },
    )

    assert normalized.tool_name == "sandbox_exec"
    assert normalized.arguments == {
        "sandbox_id": "nested-sandbox",
        "command": "touch nested.txt",
    }


def test_sandbox_catalog_exposes_one_composite_and_nine_legacy_aliases(monkeypatch) -> None:
    monkeypatch.setattr(sandbox_tools, "_sandbox_available", lambda: True)

    assert [schema["function"]["name"] for schema, _ in sandbox_tools.get_tools()] == [
        "sandbox"
    ]
    assert {
        schema["function"]["name"]
        for schema, _ in sandbox_tools.get_legacy_tools()
    } == {
        "sandbox_create",
        "sandbox_exec",
        "sandbox_status",
        "sandbox_respond",
        "sandbox_cancel",
        "sandbox_read_file",
        "sandbox_write_file",
        "sandbox_save_result",
        "sandbox_destroy",
    }


def test_sandbox_create_schema_is_generic_only() -> None:
    parameters = sandbox_tools._SANDBOX_CREATE_SCHEMA["function"]["parameters"]
    composite_description = sandbox_tools.SANDBOX_SCHEMA["function"]["parameters"][
        "properties"
    ]["params"]["description"]

    assert parameters["properties"] == {}
    assert "create: no params" in composite_description


def test_sandbox_background_actions_share_composite_authorization_contract() -> None:
    status = RuntimeCompositeToolCallFactory.create(
        "sandbox",
        {
            "action": "status",
            "params": {"sandbox_id": "sandbox-1", "execution_id": "execution-1"},
        },
    )
    cancel = RuntimeCompositeToolCallFactory.create(
        "sandbox",
        {
            "action": "cancel",
            "params": {"sandbox_id": "sandbox-1", "execution_id": "execution-1"},
        },
    )
    respond = RuntimeCompositeToolCallFactory.create(
        "sandbox",
        {
            "action": "respond",
            "params": {
                "sandbox_id": "sandbox-1",
                "execution_id": "execution-1",
                "event_id": "input-1",
                "payload": {"choice": "b"},
            },
        },
    )

    assert status.tool_name == "sandbox_status"
    assert respond.tool_name == "sandbox_respond"
    assert cancel.tool_name == "sandbox_cancel"
    assert classify_runtime_tool_action(status.tool_name, status.arguments) is None
    respond_action = classify_runtime_tool_action(
        respond.tool_name,
        respond.arguments,
    )
    assert respond_action is not None
    assert respond_action.action_key == "sandbox.respond"
    cancel_action = classify_runtime_tool_action(cancel.tool_name, cancel.arguments)
    assert cancel_action is not None
    assert cancel_action.action_key == "sandbox.cancel"
    assert runtime_capability_id_for_action_key("sandbox_status") == "sandbox.execute"
    assert runtime_capability_id_for_action_key("sandbox_respond") == "sandbox.execute"
    assert runtime_capability_id_for_action_key("sandbox_cancel") == "sandbox.execute"


@pytest.mark.asyncio
async def test_sandbox_composite_validation_uses_structured_error_contract() -> None:
    result = await sandbox_tools._sandbox_handler(action="create", params=[])

    assert json.loads(result) == {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": "params must be an object",
        },
    }


@pytest.mark.asyncio
async def test_manor_workspace_routes_to_workspace_composite(monkeypatch) -> None:
    seen: dict = {}

    async def fake_workspace_agent_handler(**kwargs):
        seen.update(kwargs)
        return json.dumps({"ok": True})

    monkeypatch.setattr(
        workspace_agent_tools,
        "_workspace_agent_handler",
        fake_workspace_agent_handler,
    )

    result = await manor_tool._dispatch_action(
        "workspace",
        {
            "action": "create_task",
            "params": {"title": "Prepare brief"},
        },
        "entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        conversation_id="conversation-1",
    )

    assert json.loads(result) == {"ok": True}
    assert seen["action"] == "create_task"
    assert seen["params"] == {"title": "Prepare brief"}
    assert seen["entity_id"] == "entity-1"
    assert seen["workspace_id"] == "workspace-1"
    assert seen["_agent_id_from_context"] == "agent-1"


@pytest.mark.asyncio
async def test_manor_invoke_skill_preserves_runtime_authorization_context(monkeypatch) -> None:
    from packages.core import database

    seen: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return None

    async def fake_runtime_invoke_skill_action(**kwargs):
        seen.update(kwargs)
        return "invoked"

    envelope = SimpleNamespace(
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-1",
        workspace_id="workspace-1",
        conversation_id="conversation-1",
        task_id="task-1",
    )
    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        manor_tool,
        "runtime_invoke_skill_action",
        fake_runtime_invoke_skill_action,
    )

    result = await manor_tool._manor_handler(
        entity_id="entity-1",
        action="invoke_skill",
        params={"skill": "private-helper", "input": "Run it."},
        _agent_id_from_context="agent-1",
        _user_id_from_context="user-1",
        _manual_skill_selected_from_context=True,
        _manual_skill_ids_from_context=["skill-1"],
        _manual_skill_slugs_from_context=["private-helper"],
        _allowed_tool_names_from_context=["invoke_skill"],
        _runtime_envelope_from_context=envelope,
        workspace_id="workspace-1",
        conversation_id="conversation-1",
        task_id="task-1",
    )

    assert result == "invoked"
    assert seen["entity_id"] == "entity-1"
    assert seen["skill_id"] == "private-helper"
    assert seen["input_text"] == "Run it."
    assert seen["user_id"] == "user-1"
    assert seen["conversation_id"] == "conversation-1"
    runtime_context = seen["runtime_context"]
    assert runtime_context.agent_id == "agent-1"
    assert runtime_context.user_id == "user-1"
    assert runtime_context.manual_skill_selected is True
    assert runtime_context.manual_skill_ids == frozenset({"skill-1"})
    assert runtime_context.manual_skill_slugs == frozenset({"private-helper"})
    assert runtime_context.allowed_tool_names == frozenset({"invoke_skill"})
    assert runtime_context.runtime_envelope is envelope


@pytest.mark.asyncio
async def test_cron_create_passes_requested_agent_with_trusted_fallback(monkeypatch) -> None:
    seen: dict = {}

    async def fake_runtime_create_scheduled_job_action(**kwargs):
        seen.update(kwargs)
        return "created"

    monkeypatch.setattr(
        cron_tools,
        "runtime_create_scheduled_job_action",
        fake_runtime_create_scheduled_job_action,
    )

    result = await cron_tools._create_job_handler(
        entity_id="entity-1",
        name="Daily report",
        schedule_kind="cron",
        payload_message="Prepare the report.",
        cron_expr="0 9 * * *",
        agent_id="model-selected-agent",
        _agent_id_from_context="calling-agent",
        _user_id_from_context="user-1",
        _runtime_envelope_from_context=SimpleNamespace(
            entity_id="entity-1",
            agent_id="calling-agent",
            user_id="user-1",
            workspace_id=None,
            conversation_id=None,
            task_id=None,
            metadata={"agent_subscription_id": "calling-subscription"},
        ),
    )

    assert result == "created"
    assert seen["agent_id"] == "model-selected-agent"
    assert seen["caller_agent_id"] == "calling-agent"
    assert seen["caller_agent_subscription_id"] == "calling-subscription"


@pytest.mark.asyncio
async def test_cron_create_uses_envelope_only_calling_agent(monkeypatch) -> None:
    seen: dict = {}

    async def fake_runtime_create_scheduled_job_action(**kwargs):
        seen.update(kwargs)
        return "created"

    monkeypatch.setattr(
        cron_tools,
        "runtime_create_scheduled_job_action",
        fake_runtime_create_scheduled_job_action,
    )

    result = await cron_tools._create_job_handler(
        entity_id="entity-1",
        name="Daily report",
        schedule_kind="cron",
        payload_message="Prepare the report.",
        cron_expr="0 9 * * *",
        _runtime_envelope_from_context=SimpleNamespace(
            entity_id="entity-1",
            agent_id="envelope-agent",
            user_id="user-1",
            workspace_id="workspace-1",
            conversation_id=None,
            task_id=None,
            metadata={"agent_subscription_id": "envelope-subscription"},
        ),
    )

    assert result == "created"
    assert seen["caller_agent_id"] == "envelope-agent"
    assert seen["caller_agent_subscription_id"] == "envelope-subscription"
    assert seen["workspace_id"] == "workspace-1"


@pytest.mark.asyncio
async def test_manor_create_scheduled_job_passes_trusted_calling_agent(monkeypatch) -> None:
    from packages.core import database

    seen: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return None

    async def fake_runtime_manor_create_scheduled_job(**kwargs):
        seen.update(kwargs)
        return "created"

    envelope = SimpleNamespace(
        entity_id="entity-1",
        user_id="user-1",
        agent_id=None,
        workspace_id="workspace-1",
        conversation_id="conversation-1",
        task_id=None,
        metadata={"agent_subscription_id": "calling-subscription"},
    )
    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        manor_tool,
        "runtime_manor_create_scheduled_job",
        fake_runtime_manor_create_scheduled_job,
    )

    result = await manor_tool._manor_handler(
        entity_id="entity-1",
        action="create_scheduled_job",
        params={
            "name": "Daily report",
            "schedule_kind": "cron",
            "payload_message": "Prepare the report.",
            "cron_expr": "0 9 * * *",
            "agent_id": "model-selected-agent",
        },
        _agent_id_from_context=None,
        _user_id_from_context="user-1",
        _runtime_envelope_from_context=envelope,
        workspace_id="workspace-1",
        conversation_id="conversation-1",
    )

    assert result == "created"
    assert seen["caller_agent_id"] == "manor-master"
    assert seen["caller_agent_subscription_id"] == "calling-subscription"
    assert seen["params"]["agent_id"] == "model-selected-agent"


@pytest.mark.asyncio
async def test_runtime_scheduled_job_receipt_exposes_resolved_agent(monkeypatch) -> None:
    from packages.core import database
    from packages.core.ai.runtime import scheduling
    from packages.core.services import scheduler_service

    class FakeDb:
        async def commit(self):
            return None

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_exc):
            return None

    seen: dict = {}

    async def fake_create_scheduled_job(*_args, **kwargs):
        seen.update(kwargs)
        selection = scheduling.RuntimeScheduledJobAgentSelection(
            agent_id="resolved-agent",
            agent_subscription_id="resolved-subscription",
            source="caller_fallback",
        )
        return SimpleNamespace(
            id="row-1",
            job_id="job-1",
            name=kwargs["name"] if "name" in kwargs else "Daily report",
            schedule_kind=kwargs["schedule_kind"],
            cron_expr=kwargs["cron_expr"],
            every_seconds=kwargs["every_seconds"],
            run_at=kwargs["run_at"],
            timezone=kwargs["timezone_str"],
            enabled=True,
            agent_id=selection.agent_id,
            _runtime_agent_selection=selection,
        )

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        scheduler_service,
        "create_scheduled_job",
        fake_create_scheduled_job,
    )

    payload = json.loads(await scheduling.runtime_create_scheduled_job_action(
        entity_id="entity-1",
        name="Daily report",
        schedule_kind="cron",
        payload_message="",
        cron_expr="0 9 * * *",
        agent_id="missing-agent",
        caller_agent_id="resolved-agent",
        caller_agent_subscription_id="resolved-subscription",
    ))

    assert payload["ok"] is True
    assert payload["automation"]["agent_id"] == "resolved-agent"
    assert payload["automation"]["agent_selection"] == "caller_fallback"
    request = seen["runtime_agent_request"]
    assert request.requested_agent_id == "missing-agent"
    assert request.caller_agent_id == "resolved-agent"
    assert request.caller_agent_subscription_id == "resolved-subscription"


@pytest.mark.asyncio
async def test_workspace_composite_rejects_nested_scope_overrides(monkeypatch) -> None:
    seen: dict = {}

    async def fake_search(**kwargs):
        seen.update(kwargs)
        return json.dumps({"ok": True})

    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_workspace_search",
        fake_search,
    )

    result = await workspace_agent_tools._workspace_agent_handler(
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
        action="search",
        params={
            "query": "launch plan",
            "entity_id": "other-entity",
            "user_id": "other-user",
            "workspace_id": "other-workspace",
            "conversation_id": "other-conversation",
            "task_id": "other-task",
            "actor_agent_id": "other-agent",
        },
    )

    assert json.loads(result) == {"ok": True}
    assert seen == {
        "entity_id": "entity-1",
        "workspace_id": "workspace-1",
        "query": "launch plan",
    }


@pytest.mark.asyncio
async def test_workspace_task_target_requires_runtime_scope_match(monkeypatch) -> None:
    seen: list[dict] = []

    async def fake_update(**kwargs):
        seen.append(kwargs)
        return json.dumps({"updated": True})

    monkeypatch.setattr(
        workspace_agent_tools,
        "_workspace_update_task_runtime_handler",
        fake_update,
    )

    matching_result = await workspace_agent_tools._workspace_agent_handler(
        entity_id="entity-1",
        workspace_id="workspace-1",
        task_id="runtime-task",
        action="update_task_runtime",
        params={"task_id": "runtime-task", "runtime_instructions": "active"},
    )
    conflicting_result = await workspace_agent_tools._workspace_agent_handler(
        entity_id="entity-1",
        workspace_id="workspace-1",
        task_id="runtime-task",
        action="update_task_runtime",
        params={"task_id": "forged-task", "runtime_instructions": "forged"},
    )
    global_result = await workspace_agent_tools._workspace_agent_handler(
        entity_id="entity-1",
        workspace_id="workspace-1",
        action="update_task_runtime",
        params={"task_id": "selected-task", "runtime_instructions": "global"},
    )

    assert json.loads(matching_result) == {"updated": True}
    assert json.loads(conflicting_result) == {
        "error": "task_id conflicts with active runtime task",
    }
    assert json.loads(global_result) == {"updated": True}
    assert seen[0]["task_id"] == "runtime-task"
    assert seen[0]["runtime_instructions"] == "active"
    assert seen[1]["task_id"] == "selected-task"
    assert seen[1]["runtime_instructions"] == "global"


def test_workspace_composite_includes_interruption_resolution_actions() -> None:
    action_schema = workspace_agent_tools.WORKSPACE_AGENT_SCHEMA["function"][
        "parameters"
    ]["properties"]["action"]

    assert {"resolve_hitl", "answer_task_blocker"} <= set(action_schema["enum"])


def test_chat_argument_compaction_preserves_manor_workspace_route() -> None:
    arguments = {
        "action": "workspace",
        "params": {
            "action": "visualize_ledgers",
            "params": {"contract_id": "manor.recruiting_ledger/v1"},
        },
    }

    assert runtime_tool_arguments_for_chat("manor", arguments) == arguments


def test_eager_surfaces_use_composites_and_defer_video_edit() -> None:
    master = eager_tool_names_for_profile(is_master=True)
    workspace = eager_tool_names_for_profile(
        is_master=True,
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )

    assert "sandbox" in master
    assert "video_edit" not in master
    assert "render_response_surface" not in master
    assert "render_response_surface" not in workspace
    assert not {
        "sandbox_create",
        "sandbox_exec",
        "sandbox_status",
        "sandbox_respond",
        "sandbox_cancel",
        "sandbox_read_file",
        "sandbox_write_file",
        "sandbox_save_result",
        "sandbox_destroy",
    }.intersection(master)
    assert "manor" in workspace
    assert "workspace_agent" not in workspace
    assert "workspace_operation" not in workspace


def test_video_edit_remains_discoverable_through_search_tools() -> None:
    pool = ToolPool()
    pool.initialize()

    matches = pool.search("video edit", max_results=10)

    assert "video_edit" in {match["name"] for match in matches}


def test_response_surface_is_loaded_through_search_tools() -> None:
    pool = ToolPool()
    pool.initialize()

    # Structured routing (04cc3909) removed the ``select:`` query form; an
    # exact load is ``mode='exact'`` with ``exact_names``. Natural-language
    # scoring of the literal string now also surfaces other deferred tools.
    matches, _suppressed = runtime_search_tool_registry_candidates(
        tool_schemas=pool.registered_tool_schemas(),
        query="",
        exact_names=["render_response_surface"],
    )
    narrow_workspace_surface = runtime_registered_tool_surface_from_schemas(
        pool.registered_tool_schemas(),
        bound_tool_names={"workspace_search"},
        is_master=False,
        tool_profile=WORKSPACE_AGENT_TOOL_PROFILE,
    )

    assert [match["name"] for match in matches] == ["render_response_surface"]
    assert "render_response_surface" in narrow_workspace_surface.deferred_tool_names
    assert "render_response_surface" not in {
        schema["function"]["name"]
        for schema in narrow_workspace_surface.prompt_schemas
    }


def test_composite_approval_classification_matches_legacy_actions() -> None:
    sandbox_composite = classify_runtime_tool_action(
        "sandbox",
        {
            "action": "exec",
            "params": {"sandbox_id": "sandbox-1", "command": "touch result.txt"},
        },
    )
    sandbox_legacy = classify_runtime_tool_action(
        "sandbox_exec",
        {"sandbox_id": "sandbox-1", "command": "touch result.txt"},
    )
    workspace_composite = classify_runtime_tool_action(
        "manor",
        {
            "action": "workspace",
            "params": {
                "action": "create_task",
                "params": {"title": "Prepare brief"},
            },
        },
    )
    workspace_legacy = classify_runtime_tool_action(
        "workspace_agent",
        {"action": "create_task", "params": {"title": "Prepare brief"}},
    )

    assert sandbox_composite == sandbox_legacy
    assert workspace_composite == workspace_legacy


def test_sandbox_save_result_composite_keeps_file_authorization_scope() -> None:
    composite = RuntimeApprovalRequest(
        tool_name="sandbox",
        arguments={
            "action": "save_result",
            "params": {
                "sandbox_id": "sandbox-1",
                "file_path": "/skill/report.pdf",
                "filename": "report.pdf",
            },
        },
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
        task_id="task-1",
    )
    legacy = RuntimeApprovalRequest(
        tool_name="sandbox_save_result",
        arguments={
            "sandbox_id": "sandbox-1",
            "file_path": "/skill/report.pdf",
            "filename": "report.pdf",
        },
        entity_id="entity-1",
        user_id="user-1",
        workspace_id="workspace-1",
        task_id="task-1",
    )

    composite_resources = WorkspaceFileAuthorizationResourceFactory.create(
        request=composite,
        action_key="workspace.file.create",
        resource_id=None,
    )
    legacy_resources = WorkspaceFileAuthorizationResourceFactory.create(
        request=legacy,
        action_key="workspace.file.create",
        resource_id=None,
    )

    assert composite_resources == legacy_resources
    assert tuple(resource.resource_id for resource in composite_resources) == (
        "artifacts/report.pdf",
    )
    assert describe_runtime_approval_action(
        composite.tool_name,
        composite.arguments,
    ) == "Save to Knowledge: report.pdf"


def test_sandbox_create_is_advertised_in_every_coordination_mode(monkeypatch) -> None:
    """Every advertised action must be executable: generic creation now admits
    through the reservation queue on external runners too (it used to be
    refused there while still advertised, 15 failed calls on staging in 30
    days), so one schema serves every deployment."""

    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "http://sandbox-service")
    for mode in ("external-runner", "local"):
        monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", mode)
        schema = sandbox_tools.get_tools()[0][0]["function"]
        assert schema["parameters"]["properties"]["action"]["enum"] == list(SandboxToolAction.values())
