from __future__ import annotations

import copy
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_draft_service as service


def binding_issue(field="tool_bindings", ref="coding_based_video"):
    return {
        "severity": "P0",
        "where": f"agent_mappings.evidence_fact_check.{field}",
        "binding_ref": ref,
        "message": f"binding {ref!r} is not available.",
    }


@pytest.fixture
def repair_context(monkeypatch):
    draft = WorkspaceDraft(
        id="draft-repair", entity_id="entity", user_id="owner", status="ready",
        ready=True, missing=[], messages=[{"role": "user", "content": "先不管目标"}],
        fields={
            "name": "Evidence review", "goals": [], "heartbeat_enabled": False,
            "rules": [],
            "services": [{"service_key": "evidence_fact_check"}],
            "agent_mappings": [{
                "service_key": "evidence_fact_check", "strategy": "create_custom",
                "create_agent_draft": {
                    "agent_name": "Evidence Agent", "system_prompt": "Verify evidence.",
                    "tool_bindings": ["coding_based_video"],
                },
            }],
        },
    )
    db = SimpleNamespace(flush=AsyncMock(), refresh=AsyncMock())
    lint = AsyncMock(side_effect=[
        {"ok": True, "issues": [binding_issue()]}, {"ok": True, "issues": []},
    ])

    async def repair(*_args, **_kwargs):
        fields = copy.deepcopy(draft.fields)
        fields["agent_mappings"][0]["create_agent_draft"]["tool_bindings"] = ["sandbox"]
        draft.fields = fields
        return "已修正代理工具绑定。"

    architect = AsyncMock(side_effect=repair)
    create = AsyncMock(return_value="workspace-created")
    gate = AsyncMock()
    monkeypatch.setattr(service, "get_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(service, "runtime_lint_workspace_draft", lint)
    monkeypatch.setattr("packages.core.ai.runtime.workspace_drafts.runtime_lint_workspace_draft", lint)
    monkeypatch.setattr(service, "runtime_run_workspace_architect_turn", architect)
    monkeypatch.setattr(service, "finalize_setup", create)
    monkeypatch.setattr("packages.core.services.plan_gate.enforce_workspace_capacity", gate)
    return SimpleNamespace(draft=draft, db=db, lint=lint, architect=architect, create=create, gate=gate)


async def finalize(ctx, progress=None):
    return await service.finalize_draft(
        ctx.db, draft_id=ctx.draft.id, entity_id="entity", user_id="owner", progress=progress,
    )


@pytest.mark.asyncio
async def test_creation_repairs_stale_binding_then_materializes_once(repair_context):
    ctx = repair_context
    original_messages = copy.deepcopy(ctx.draft.messages)
    events = []
    workspace_id, draft = await finalize(ctx, lambda *event: events.append(event))

    assert workspace_id == "workspace-created"
    assert draft.status == "finalized"
    assert draft.ready
    ctx.architect.assert_awaited_once()
    repair_args = ctx.architect.call_args.kwargs
    assert repair_args["repair_service_keys"] == ("evidence_fact_check",)
    assert repair_args["user_id"] == "owner"
    assert json.loads(repair_args["user_message"])["validation_issues"] == [binding_issue()]
    assert draft.messages == [
        *original_messages, {"role": "assistant", "content": "已修正代理工具绑定。"},
    ]
    ctx.create.assert_awaited_once()
    installed = ctx.create.call_args.args[0].fields
    assert installed["agent_mappings"][0]["create_agent_draft"]["tool_bindings"] == ["sandbox"]
    assert installed["heartbeat_enabled"] is False and installed["goals"] == []
    assert events == [
        ("agent_bindings_repair_started", {"service_keys": ["evidence_fact_check"]}),
        ("agent_bindings_repair_done", {"ready": True}),
    ]
    # A repeat finalization reuses the committed identity without repair or creation.
    assert (await finalize(ctx))[0] == workspace_id
    ctx.architect.assert_awaited_once()
    ctx.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsuccessful_repair_stops_after_one_turn_and_keeps_draft_editable(repair_context):
    ctx = repair_context
    original_messages = copy.deepcopy(ctx.draft.messages)
    ctx.architect.side_effect = None
    ctx.architect.return_value = "The bindings are repaired."
    ctx.lint.side_effect = None
    ctx.lint.return_value = {"ok": True, "issues": [binding_issue()]}
    events = []
    with pytest.raises(service.WorkspaceDraftNotReadyError):
        await finalize(ctx, lambda *event: events.append(event))
    ctx.architect.assert_awaited_once()
    ctx.create.assert_not_awaited()
    assert ctx.draft.status == "active" and not ctx.draft.ready
    assert ctx.draft.finalized_workspace_id is None
    assert ctx.draft.messages == original_messages
    assert events[-1] == ("agent_bindings_repair_done", {"ready": False})


@pytest.mark.asyncio
@pytest.mark.parametrize("issues,blueprint", [
    ([binding_issue(), {"severity": "P0", "where": "goals", "message": "Confirm goal choice"}], None),
    ([binding_issue("agent_name")], None),
    ([binding_issue()], "frozen-blueprint"),
    ([{**binding_issue(), "where": "agent_mappings.other.tool_bindings"}], None),
])
async def test_non_binding_or_blueprint_failures_do_not_trigger_repair(repair_context, issues, blueprint):
    ctx = repair_context
    ctx.draft.applied_blueprint_id = blueprint
    ctx.lint.side_effect = None
    ctx.lint.return_value = {"ok": True, "issues": issues}
    with pytest.raises(service.WorkspaceDraftNotReadyError):
        await finalize(ctx)
    ctx.architect.assert_not_awaited()
    ctx.create.assert_not_awaited()
    ctx.gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_workspace_plan_rejects_before_billable_repair(repair_context):
    from packages.core.services.plan_gate import GateResult, WorkspacePlanLimitError

    ctx = repair_context
    ctx.gate.side_effect = WorkspacePlanLimitError(GateResult(
        allowed=False, message="Workspace limit reached", limit=1, current=1, plan="Free",
    ))
    with pytest.raises(WorkspacePlanLimitError):
        await finalize(ctx)
    ctx.architect.assert_not_awaited()
    ctx.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_pass", [0, 1])
async def test_unverified_lint_never_materializes(repair_context, failed_pass):
    ctx = repair_context
    original_messages = copy.deepcopy(ctx.draft.messages)
    ctx.lint.side_effect = ([None] if failed_pass == 0 else [
        {"ok": True, "issues": [binding_issue()]}, None,
    ])
    with pytest.raises(ValueError, match="readiness could not be verified"):
        await finalize(ctx)
    ctx.create.assert_not_awaited()
    assert ctx.architect.await_count == failed_pass
    assert ctx.draft.messages == original_messages


@pytest.mark.asyncio
async def test_runtime_abort_stops_repair_without_creation(repair_context):
    from packages.core.ai.runtime.control import RuntimeTurnAborted

    ctx = repair_context
    ctx.architect.side_effect = RuntimeTurnAborted("draft changed during matching")
    with pytest.raises(RuntimeTurnAborted):
        await finalize(ctx)
    ctx.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_finalize_returns_existing_workspace_after_matching_releases_lock(repair_context):
    from packages.core.ai.runtime.control import RuntimeTurnAborted

    ctx = repair_context

    async def concurrent_finalize(*_args, **_kwargs):
        ctx.draft.status = "finalized"
        ctx.draft.finalized_workspace_id = "concurrent-workspace"
        raise RuntimeTurnAborted("draft changed during matching")

    ctx.architect.side_effect = concurrent_finalize
    assert (await finalize(ctx))[0] == "concurrent-workspace"
    ctx.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("ws_propose_rule", {"service_key": "evidence_fact_check"}),
    ("ws_confirm_creation_preferences", {}),
    ("ws_search_capabilities", {"service_key": "other"}),
    ("ws_request_custom_agent", {"service_key": "other"}),
    ("ws_search_capabilities", {"service_key": "evidence_fact_check", "draft_id": "other"}),
    ("ws_lint_draft", {"draft_id": "other"}),
])
async def test_repair_executor_rejects_other_mutations_and_scope(monkeypatch, name, args):
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.ai.tools import workspace_arch_tools

    handler = AsyncMock()
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, name, handler)
    executor = runtime_workspace_architect_tool_executor(
        SimpleNamespace(), draft_id="draft-repair", entity_id="entity", user_id="owner",
        repair_service_keys=("evidence_fact_check",),
    )
    result = json.loads(await executor(name, args))
    assert result == {"ok": False, "error": "outside agent binding repair scope"}
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_repair_executor_preserves_actor_for_allowed_catalog_read(monkeypatch):
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.ai.tools import workspace_arch_tools

    handler = AsyncMock(return_value='{"ok": true}')
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_search_capabilities", handler)
    db = SimpleNamespace()
    executor = runtime_workspace_architect_tool_executor(
        db, draft_id="draft-repair", entity_id="entity", user_id="owner",
        repair_service_keys=("evidence_fact_check",),
    )
    assert json.loads(await executor("ws_search_capabilities", {"service_key": "evidence_fact_check"}))["ok"]
    handler.assert_awaited_once_with(
        db, entity_id="entity", user_id="owner", draft_id="draft-repair", service_key="evidence_fact_check",
    )


@pytest.mark.asyncio
async def test_repair_cannot_rewrite_agent_definition_or_drop_optional_settings(repair_context, monkeypatch):
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.ai.tools import workspace_arch_tools

    ctx = repair_context
    original = ctx.draft.fields["agent_mappings"][0]["create_agent_draft"]
    original.update({
        "missing_skill_specs": [{"name": "Review", "system_prompt": "Use source evidence."}],
        "missing_integrations": [{"provider": "example", "required": True}],
        "skill_binding_refs": [{"marketplace_source": "platform", "marketplace_id": "exact-skill"}],
        "model_configuration": {"model": "saved-model"},
        "mcp_allowed_tools": {"example": ["read"]},
    })
    original = copy.deepcopy(original)

    async def rewrite(_db, **kwargs):
        assert kwargs["agent_name"] == original["agent_name"]
        assert kwargs["system_prompt"] == original["system_prompt"]
        assert kwargs["missing_skill_specs"] == original["missing_skill_specs"]
        assert kwargs["missing_integrations"] == original["missing_integrations"]
        fields = copy.deepcopy(ctx.draft.fields)
        fields["agent_mappings"][0]["create_agent_draft"] = {
            "tool_bindings": ["sandbox"], "mcp_allowed_tools": {"example": None},
        }
        ctx.draft.fields = fields
        return '{"ok": true}'

    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_request_custom_agent", rewrite)
    executor = runtime_workspace_architect_tool_executor(
        ctx.db, draft_id=ctx.draft.id, entity_id="entity", user_id="owner",
        repair_service_keys=("evidence_fact_check",),
    )
    result = json.loads(await executor("ws_request_custom_agent", {
        "service_key": "evidence_fact_check", "agent_name": "Different Agent",
        "system_prompt": "A model-authored replacement prompt that must never replace the saved Agent definition. " * 2,
        "missing_skill_specs": [], "missing_integrations": [],
    }))
    assert result["ok"]
    assert ctx.draft.fields["agent_mappings"][0]["create_agent_draft"] == {
        **original, "tool_bindings": ["sandbox"],
    }


@pytest.mark.asyncio
async def test_repair_preserves_valid_bindings_and_removes_only_current_lint_failures(repair_context, monkeypatch):
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.ai.tools import workspace_arch_tools

    ctx = repair_context
    original = ctx.draft.fields["agent_mappings"][0]["create_agent_draft"]
    original.update({
        "tool_bindings": ["coding_based_video", "web_search"],
        "business_capabilities": ["missing.capability", "file.read"],
        "skill_bindings": ["unavailable-skill", "required-evidence-skill"],
        "mcp_bindings": ["unavailable-server", "google_drive", "github"],
        "mcp_allowed_tools": {"google_drive": ["read_file"], "github": []},
    })
    ctx.lint.side_effect = None
    ctx.lint.return_value = {"ok": True, "issues": [
        binding_issue(),
        binding_issue("business_capabilities", "missing.capability"),
        binding_issue("skill_bindings", "unavailable-skill"),
        binding_issue("mcp_bindings", "unavailable-server"),
    ]}

    async def rematch(_db, **kwargs):
        fields = copy.deepcopy(ctx.draft.fields)
        fields["agent_mappings"][0]["create_agent_draft"] = {
            "tool_bindings": ["sandbox", "web_search"],
            "business_capabilities": ["sandbox.execute"],
            "skill_bindings": [],
            "mcp_bindings": ["google_drive"],
            "mcp_allowed_tools": {"google_drive": None},
        }
        ctx.draft.fields = fields
        return '{"ok": true}'

    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_request_custom_agent", rematch)
    executor = runtime_workspace_architect_tool_executor(
        ctx.db, draft_id=ctx.draft.id, entity_id="entity", user_id="owner",
        repair_service_keys=("evidence_fact_check",),
    )
    result = json.loads(await executor("ws_request_custom_agent", {
        "service_key": "evidence_fact_check", "agent_name": original["agent_name"],
        "system_prompt": "Verify evidence carefully using the user's saved tools, skills, and connected accounts. " * 2,
    }))
    assert result["ok"]
    ctx.lint.assert_awaited_once_with(
        ctx.db, entity_id="entity", draft_id=ctx.draft.id, user_id="owner",
    )
    repaired = ctx.draft.fields["agent_mappings"][0]["create_agent_draft"]
    assert repaired["tool_bindings"] == ["web_search", "sandbox"]
    assert repaired["business_capabilities"] == ["file.read", "sandbox.execute"]
    assert repaired["skill_bindings"] == ["required-evidence-skill"]
    assert repaired["mcp_bindings"] == ["google_drive", "github"]
    assert repaired["mcp_allowed_tools"] == {"google_drive": ["read_file"], "github": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("lint", [None, {"ok": False}, {
    "ok": True, "issues": [{**binding_issue(), "binding_ref": None}],
}])
async def test_repair_does_not_mutate_bindings_without_verified_exact_failures(repair_context, monkeypatch, lint):
    from packages.core.ai.runtime.control import RuntimeTurnAborted
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.ai.tools import workspace_arch_tools

    ctx = repair_context
    original = copy.deepcopy(ctx.draft.fields)
    ctx.lint.side_effect = None
    ctx.lint.return_value = lint
    handler = AsyncMock()
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_request_custom_agent", handler)
    executor = runtime_workspace_architect_tool_executor(
        ctx.db, draft_id=ctx.draft.id, entity_id="entity", user_id="owner",
        repair_service_keys=("evidence_fact_check",),
    )
    with pytest.raises(RuntimeTurnAborted):
        await executor("ws_request_custom_agent", {
            "service_key": "evidence_fact_check", "agent_name": "Evidence Agent",
            "system_prompt": "Verify evidence carefully using the user's saved tools, skills, and connected accounts. " * 2,
        })
    handler.assert_not_awaited()
    assert ctx.draft.fields == original


@pytest.mark.asyncio
async def test_repair_does_not_rematch_service_that_is_already_valid(repair_context, monkeypatch):
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor
    from packages.core.ai.tools import workspace_arch_tools

    ctx = repair_context
    original = copy.deepcopy(ctx.draft.fields)
    ctx.lint.side_effect = None
    ctx.lint.return_value = {"ok": True, "issues": []}
    handler = AsyncMock()
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_request_custom_agent", handler)
    executor = runtime_workspace_architect_tool_executor(
        ctx.db, draft_id=ctx.draft.id, entity_id="entity", user_id="owner",
        repair_service_keys=("evidence_fact_check",),
    )
    result = json.loads(await executor("ws_request_custom_agent", {
        "service_key": "evidence_fact_check", "agent_name": "Evidence Agent",
        "system_prompt": "Verify evidence carefully using the user's saved tools, skills, and connected accounts. " * 2,
    }))
    assert result["ok"] and result["already_valid"]
    handler.assert_not_awaited()
    assert ctx.draft.fields == original


@pytest.mark.asyncio
async def test_architect_repair_uses_bounded_scoped_runtime(repair_context, monkeypatch):
    from packages.core.ai.runtime.workspace_drafts import AGENT_BINDING_REPAIR_TOOLS
    from packages.core.services import workspace_architect

    def prepare(_request, *, tool_schemas):
        return SimpleNamespace(
            envelope=object(), tool_schemas=tool_schemas,
            allowed_tool_names={schema["function"]["name"] for schema in tool_schemas},
        )

    loop = AsyncMock(return_value=SimpleNamespace(content="Repaired", usage={}))
    monkeypatch.setattr(workspace_architect, "runtime_prepare_local_tool_surface_for_turn", prepare)
    monkeypatch.setattr(workspace_architect, "runtime_wrap_tool_executor", lambda _envelope, executor: executor)
    monkeypatch.setattr(workspace_architect, "runtime_execute_workspace_architect_loop", loop)
    assert await workspace_architect.architect_run_turn(
        repair_context.db, draft_id="draft-repair", entity_id="entity", user_id="owner",
        user_message="Saved repair diagnostics", repair_service_keys=("evidence_fact_check",),
    ) == "Repaired"
    args = loop.call_args.kwargs
    assert args["max_rounds"] == 8
    assert args["allowed_tool_names"] == AGENT_BINDING_REPAIR_TOOLS
    assert args["entity_id"] == "entity" and args["user_id"] == "owner"
    assert "automatic pre-creation Agent binding repair" in args["system_prompt"]


@pytest.mark.asyncio
async def test_finalize_stream_stays_connected_during_quiet_repair(monkeypatch):
    from apps.api.routers import workspace_drafts

    release = asyncio.Event()
    commit = AsyncMock()

    class Session:
        async def __aenter__(self):
            return SimpleNamespace(commit=commit)

        async def __aexit__(self, *_exc):
            return None

    class Lease:
        async def wrap(self, source):
            async for chunk in source:
                yield chunk

    async def repair(*_args, **kwargs):
        kwargs["progress"]("agent_bindings_repair_started", {"service_keys": ["evidence_fact_check"]})
        await release.wait()
        kwargs["progress"]("agent_bindings_repair_done", {"ready": False})
        raise service.WorkspaceDraftNotReadyError("Agent bindings still need correction")

    monkeypatch.setattr(workspace_drafts, "async_session", Session)
    monkeypatch.setattr(workspace_drafts, "acquire_chat_stream_lease", AsyncMock(return_value=Lease()))
    monkeypatch.setattr(workspace_drafts, "_DRAFT_STREAM_KEEPALIVE_SECONDS", 0.01)
    monkeypatch.setattr(service, "finalize_draft", repair)
    response = await workspace_drafts.finalize_stream(
        "draft-repair", user=SimpleNamespace(entity_id="entity", id="owner"), db=AsyncMock(),
    )
    stream = response.body_iterator
    try:
        assert "event: start" in await anext(stream)
        assert "event: agent_bindings_repair_started" in await anext(stream)
        assert "event: keepalive" in await asyncio.wait_for(anext(stream), timeout=0.5)
    finally:
        release.set()
    remaining = "".join([chunk async for chunk in stream])
    assert "event: agent_bindings_repair_done" in remaining
    assert "event: error" in remaining
    assert "event: done" not in remaining
    commit.assert_awaited_once()
