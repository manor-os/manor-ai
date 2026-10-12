"""Workspace overlays must preserve the master's unrestricted native scope.

ORM-shaped rows replace persistence only. Scope resolution, tool visibility,
prompt-Skill inheritance, runtime policy, and action authorization stay real.
No model, media job, sandbox, or external action is executed.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import tool_registry
from packages.core.ai.runtime.capabilities import CORE_CAPABILITIES
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.policies import check_runtime_tool_policy
from packages.core.ai.runtime.principals import resolve_runtime_principal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.skills import runtime_prepare_prompt_skill_tool_surface
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_visibility import resolve_runtime_tool_surface
from packages.core.models.mcp import AgentMCPBinding
from packages.core.models.skill import Skill
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.workspace import AgentSubscription, ToolDefinition, Workspace
from packages.core.services import workspace_runtime
from packages.core.services.runtime_authorization import authorize_runtime_action


VIDEO_CONFIG = json.loads((
    Path(__file__).resolve().parents[1] / "packages/core/ai/skills/video-edit/config.json"
).read_text(encoding="utf-8"))
MEDIA_TOOLS = {"video_edit", "wait_media_jobs", "probe_media"}
MCP_TOOLS = {"mcp__elevenlabs__speak", "mcp__other__read"}
CATALOG = set(VIDEO_CONFIG["tools"]) | MCP_TOOLS | {
    "search_tools", "invoke_skill", "sandbox", "manor", "web_search",
    "service_only_tool", "host_only_tool", "unbound_tool", "query_ledger",
    "read_finance_ledger", "record_finance_ledger", "visualize_workspace_ledgers",
    *CORE_CAPABILITIES["automation.manage"].tool_names,
}


@pytest.fixture(autouse=True)
def _isolate_sandbox_owner_registry():
    # No sandbox is created; do not perform the suite's Redis cleanup.
    yield


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        assert len(self.rows) <= 1
        return self.rows[0] if self.rows else None

    def one_or_none(self):
        value = self.scalar_one_or_none()
        return None if value is None else (value,)


class _ScopeDB:
    def __init__(self, *, bindings=(), ledger=False, disabled=(), task=False):
        workspace = Workspace(
            id="workspace", entity_id="entity", name="Video workspace",
            operating_model={
                "capability_bindings": list(bindings),
                "integration_scope": {"disabled_providers": list(disabled)},
            },
            settings={"ledger_contracts": [{
                "contract_id": "manor.finance_ledger/v1", "schema_version": 1,
                "directory": "finance-ledger",
            }]} if ledger else {},
        )
        self.rows = {
            Workspace: [workspace],
            Task: [Task(
                id="task", entity_id="entity", workspace_id="workspace",
                title="Edit existing clips", owner_service_key="video",
                delegate_service_keys=[], details={},
            )] if task else [],
            AgentSubscription: [AgentSubscription(
                id="subscription", entity_id="entity", workspace_id="workspace",
                agent_id="specialist", service_key="video", status="active",
            )],
            ToolDefinition: [ToolDefinition(id="tool", name="list_files", status="active")],
            AgentMCPBinding: [], Skill: [],
            User: [User(id="user", entity_id="entity", role="member", status="active")],
        }

    async def execute(self, statement):
        columns = statement.column_descriptions
        model = columns[0]["entity"]
        assert model in self.rows, f"Unexpected scope query: {model}"
        rows = self.rows[model]
        if columns[0]["expr"] is model:
            return _Rows(rows)
        values = [tuple(getattr(row, column["name"]) for column in columns) for row in rows]
        return _Rows([value[0] for value in values] if len(columns) == 1 else values)


@pytest.fixture
def scope_dependencies(monkeypatch):
    # Persisted role and registry reads are isolated; no scope resolver/gate is mocked.
    can_write = AsyncMock(return_value=True)
    monkeypatch.setattr(workspace_runtime, "user_can_write_workspace_artifacts", can_write)
    monkeypatch.setattr(tool_registry, "runtime_registered_tool_names", lambda **kwargs: tuple(
        name for name in CATALOG if not kwargs.get("prefix") or name.startswith(kwargs["prefix"])
    ))
    return can_write


async def _resolve(db, *, workspace=True, task=False, master=True):
    return await workspace_runtime.resolve_workspace_runtime(
        db, entity_id="entity", user_id="user", workspace_id="workspace" if workspace else None,
        agent_id=None if master else "specialist", task_id="task" if task else None,
        is_master=master, include_prompt_context=False,
        runtime_surface=(ChatSurface.TASK_COMMENT_THREAD if task else
                         ChatSurface.WORKSPACE_CHAT if workspace else ChatSurface.GLOBAL_OWNER_CHAT),
    )


def _visible(scope):
    return set(resolve_runtime_tool_surface(
        CATALOG, bound_tool_names=scope.bound_tool_names,
        is_master=scope.is_master, mcp_allowed_names=scope.mcp_allowed_names,
        tool_profile=scope.tool_profile,
    ).visible_tool_names)


def _envelope(scope):
    surface = ChatSurface.TASK_COMMENT_THREAD if scope.task_id else ChatSurface.WORKSPACE_CHAT
    return RuntimeEnvelope(
        surface=surface, profile=RuntimeProfile.WORKSPACE_OPERATOR,
        principal=resolve_runtime_principal(
            surface=surface, entity_id="entity", user_id="user", workspace_id=scope.workspace_id,
        ),
        entity_id="entity", user_id="user", workspace_id=scope.workspace_id,
        task_id=scope.task_id, tool_profile=scope.tool_profile,
        allowed_tool_names=tuple(_visible(scope)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", [False, True])
@pytest.mark.parametrize("operation_binding", [False, True])
async def test_workspace_main_overlays_preserve_global_master_media_scope(
    scope_dependencies, ledger, operation_binding,
):
    bindings = [{"owner_scope": "workspace_agent", "capability_type": "tool",
                 "tool_name": "host_only_tool"}] if operation_binding else []
    db = _ScopeDB(bindings=bindings, ledger=ledger)
    workspace = await _resolve(db)
    global_chat = await _resolve(db, workspace=False)

    assert workspace.bound_tool_names is global_chat.bound_tool_names is None
    assert workspace.mcp_allowed_names is global_chat.mcp_allowed_names is None
    assert workspace.mcp_scope_unrestricted is global_chat.mcp_scope_unrestricted is True
    assert _visible(workspace) == _visible(global_chat)
    assert MEDIA_TOOLS <= _visible(workspace)
    # Master visibility is not an implicit semantic capability binding.
    assert "automation.manage" not in workspace.capability_ids


@pytest.mark.asyncio
async def test_workspace_main_keeps_explicit_automation_capability_binding(scope_dependencies):
    scope = await _resolve(_ScopeDB(bindings=[{
        "owner_scope": "workspace_agent", "capability_type": "capability",
        "capability_id": "automation.manage",
    }]))
    assert "automation.manage" in scope.capability_ids
    assert scope.bound_tool_names is None
    assert MEDIA_TOOLS <= _visible(scope)


@pytest.mark.asyncio
async def test_video_prompt_child_inherits_master_media_tools_without_losing_policy(scope_dependencies):
    scope = await _resolve(_ScopeDB())
    envelope = _envelope(scope)
    skill = SimpleNamespace(config=VIDEO_CONFIG, tools=VIDEO_CONFIG["tools"], slug="video-edit")
    child = runtime_prepare_prompt_skill_tool_surface(
        skill, allowed_tool_names=envelope.allowed_tool_names, runtime_envelope=envelope,
        get_schemas_for_names=lambda names: [{"function": {"name": name}} for name in names],
        get_registered_tool_names=lambda: CATALOG,
    )

    assert MEDIA_TOOLS <= set(child.skill_tool_names)
    assert child.harness is not None
    assert child.harness.envelope.principal is envelope.principal
    assert child.harness.envelope.workspace_id == "workspace"
    for name in MEDIA_TOOLS:
        assert check_runtime_tool_policy(envelope=child.harness.envelope, tool_name=name).allowed
    assert CORE_CAPABILITIES["sandbox.execute"].required_approval

    blocked = runtime_prepare_prompt_skill_tool_surface(
        skill, allowed_tool_names=envelope.allowed_tool_names,
        runtime_envelope=replace(envelope, blocked_tool_names=("video_edit",)),
        get_schemas_for_names=lambda names: [], get_registered_tool_names=lambda: CATALOG,
    )
    assert not check_runtime_tool_policy(envelope=blocked.harness.envelope, tool_name="video_edit").allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", [False, True])
async def test_task_host_stays_explicit_and_does_not_inherit_service_or_media_tools(scope_dependencies, ledger):
    db = _ScopeDB(task=True, ledger=ledger, bindings=[
        {"owner_scope": "workspace_agent", "capability_type": "tool", "tool_name": "host_only_tool"},
        {"owner_scope": "service", "owner_service_key": "video", "capability_type": "tool", "tool_name": "service_only_tool"},
    ])
    host = await _resolve(db, task=True)

    assert host.bound_tool_names is not None
    assert "host_only_tool" in host.bound_tool_names
    assert "service_only_tool" not in host.bound_tool_names
    assert host.mcp_allowed_names == set()
    assert not host.mcp_scope_unrestricted
    assert host.service_agent_ids == ["specialist"]
    assert not (MEDIA_TOOLS | MCP_TOOLS | {"unbound_tool"}) & _visible(host)
    for name in MEDIA_TOOLS | MCP_TOOLS | {"service_only_tool"}:
        assert not check_runtime_tool_policy(envelope=_envelope(host), tool_name=name).allowed


@pytest.mark.asyncio
async def test_non_master_keeps_direct_and_service_bindings_without_master_media_scope(scope_dependencies):
    db = _ScopeDB(bindings=[
        {"owner_scope": "workspace_agent", "capability_type": "tool", "tool_name": "host_only_tool"},
        {"owner_scope": "service", "owner_service_key": "video", "capability_type": "tool", "tool_name": "service_only_tool"},
    ])
    child = await _resolve(db, master=False)

    assert {"list_files", "service_only_tool"} <= child.bound_tool_names
    assert "host_only_tool" not in child.bound_tool_names
    assert child.mcp_allowed_names == set()
    assert not child.mcp_scope_unrestricted
    assert not (MEDIA_TOOLS | MCP_TOOLS | {"unbound_tool"}) & _visible(child)
    for name in MEDIA_TOOLS:
        assert not check_runtime_tool_policy(envelope=_envelope(child), tool_name=name).allowed


@pytest.mark.asyncio
async def test_disabled_mcp_stays_hidden_and_denied_with_unrestricted_native_master(scope_dependencies):
    db = _ScopeDB(disabled=["elevenlabs"])
    scope = await _resolve(db)
    assert scope.bound_tool_names is None
    assert scope.mcp_allowed_names == {"mcp__other__read"}
    assert "mcp__elevenlabs__speak" not in _visible(scope)
    assert "mcp__other__read" in _visible(scope)
    assert MEDIA_TOOLS <= _visible(scope)

    # Even a stale/forged allowlist cannot bypass the current provider exclusion.
    decision = await authorize_runtime_action(
        db, entity_id="entity", user_id="user", workspace_id="workspace",
        principal_kind="workspace_member", tool_name="mcp__elevenlabs__speak",
        bound_tool_names=MCP_TOOLS, action_key="", capability_id="", access="action",
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
    )
    assert not decision.allowed
    assert decision.matched_rule == "permission.workspace.integration_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,action_key,capability_id", [
    ("video_edit", "sandbox.execute", "sandbox.execute"),
    ("record_finance_ledger", "", ""),
    ("create_scheduled_job", "workspace.automation.create", "automation.manage"),
])
async def test_read_only_master_visibility_never_grants_workspace_write_authority(
    monkeypatch, scope_dependencies, tool_name, action_key, capability_id,
):
    scope_dependencies.return_value = False
    db = _ScopeDB(ledger=True)
    scope = await _resolve(db)
    assert scope.bound_tool_names is None
    assert "automation.manage" not in scope.capability_ids
    assert MEDIA_TOOLS <= _visible(scope)

    monkeypatch.setattr("packages.core.services.resource_access.resolve_user_role", AsyncMock(return_value="member"))
    monkeypatch.setattr("packages.core.services.workspace_access.user_can_write_workspace_artifacts", AsyncMock(return_value=False))
    monkeypatch.setattr("packages.core.services.workspace_access.user_can_manage_workspace", AsyncMock(return_value=False))
    decision = await authorize_runtime_action(
        db, entity_id="entity", user_id="user", workspace_id="workspace",
        principal_kind="workspace_member", tool_name=tool_name,
        bound_tool_names=_visible(scope), action_key=action_key, capability_id=capability_id,
        access="action", runtime_surface=ChatSurface.WORKSPACE_CHAT,
    )
    assert not decision.allowed
    assert decision.matched_rule in {"permission.workspace.write", "permission.workspace.manage"}
