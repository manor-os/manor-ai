from copy import deepcopy
from pathlib import Path

import pytest

from packages.core.ai.runtime import AIRuntimeRequest, ChatSurface, RuntimeResolver
from packages.core.ai.runtime.prompt_adapter import ChatContext, build_default_prompt_builder
from packages.core.ai.runtime.prompt_sections import workspace_agent_mode, workspace_operation_skill_guidance
from packages.core.ai.runtime import workspace_operation_skills as guides
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.services.builtin_skill_loader import seed_builtin_skills
from packages.core.services.skill_service import invoke_skill


def context(message="", *, surface=ChatSurface.WORKSPACE_CHAT, allowed=None, user_id="user", **kwargs):
    allowed = set(runtime_registered_tool_names()) if allowed is None else set(allowed)
    request = AIRuntimeRequest(surface=surface, entity_id="entity", user_id=user_id, workspace_id="workspace")
    envelope = RuntimeResolver().resolve_trace_envelope(request, allowed_tool_names=allowed)
    return ChatContext(runtime_envelope=envelope, workspace_id="workspace", active_user_message=message,
                       allowed_tool_names=allowed, user_id=user_id, **kwargs)


@pytest.mark.parametrize("message,expected", [
    ("把 workflow 放到 App 主功能，连接 automation 或 Agent Chat", ["workspace-app"]),
    ("修改工作区配置：增加一个智能体并绑定工具", ["workspace-configuration"]),
    ("创建一个 Workflow 并验证部署", ["workspace-workflows"]),
    ("暂停每天的 automation", ["workspace-automations"]),
    ("给现有任务追加要求", ["workspace-tasks"]),
    ("读取工作区知识文档", ["workspace-knowledge"]),
    ("批准待处理请求并检查目标指标", ["workspace-goals-handoffs"]),
    ("hello", []),
    ("Compare Apple stock", []),
])
def test_preload_selects_relevant_guides_without_executing_tools(message, expected):
    ctx = context(message)
    before = ctx.runtime_envelope.effective_allowed_tool_names()
    rendered = workspace_operation_skill_guidance(ctx)
    assert [item["slug"] for item in ctx.runtime_workspace_operation_skills] == ["workspace-operations", *expected]
    assert ctx.runtime_envelope.effective_allowed_tool_names() == before
    assert workspace_agent_mode(ctx) is None
    for slug in guides.WORKSPACE_OPERATION_SKILL_SLUGS - {"workspace-operations", *expected}:
        assert f"### Guide `{slug}`" not in rendered
    assert len(rendered) < (18_000 if "workspace-configuration" in expected else 12_000)


@pytest.mark.parametrize("kwargs", [
    {"surface": ChatSurface.GLOBAL_OWNER_CHAT},
    {"surface": ChatSurface.PUBLIC_CUSTOMER_CHAT},
    {"surface": ChatSurface.WORKFLOW_AGENT_STEP},
    {"surface": ChatSurface.SCHEDULED_AGENT_RUN},
    {"manual_skill_selected": True},
    {"task_id": "existing-task"},
    {"allowed": {"invoke_skill", "search_tools", "read_file"}},
    {"allowed": set()},
])
def test_guides_respect_surface_scope_and_manual_selection(kwargs):
    ctx = context("修改 App", **kwargs)
    assert workspace_operation_skill_guidance(ctx) is None
    assert not ctx.runtime_workspace_operation_skills


def test_all_workspace_actions_and_configuration_patch_types_are_covered():
    from packages.core.ai.runtime.composite_tools import WorkspaceToolAction
    from packages.core.services.workspace_operation_service import OPERATION_PATCH_OP_CATALOG

    specs = [config["workspace_operations"] for config in guides.workspace_operation_guide_configs().values()]
    assert {action for spec in specs for action in spec["workspace_actions"]} == {action.value for action in WorkspaceToolAction}
    assert {patch for spec in specs for patch in spec["patch_ops"]} == {entry.canonical for entry in OPERATION_PATCH_OP_CATALOG}


def test_cached_instructions_do_not_keep_revoked_scope_or_obsolete_trace_metadata():
    from dataclasses import replace

    ctx = context("修改 App")
    workspace_operation_skill_guidance(ctx)
    ctx.runtime_envelope = replace(ctx.runtime_envelope, blocked_tool_names=("manor",))
    assert workspace_operation_skill_guidance(ctx) is None
    assert not ctx.runtime_workspace_operation_skills
    assert "workspace_operation_skills" not in ctx.runtime_envelope.metadata


def test_scope_is_resolved_again_after_a_cached_guide_was_loaded():
    workspace_operation_skill_guidance(context("启动 Workflow"))
    ctx = context("启动 Workflow", allowed={"manor", "invoke_skill", "search_tools"})
    rendered = workspace_operation_skill_guidance(ctx)
    assert "`workspace-workflows`:" not in rendered
    assert "`workspace-automations`:" not in rendered
    assert [row["slug"] for row in ctx.runtime_workspace_operation_skills] == ["workspace-operations"]


def test_shared_cache_contains_only_guide_source_and_tool_contract():
    guides._guide.cache_clear()
    first = workspace_operation_skill_guidance(context("调整 App 布局", user_id="private-user-a"))
    initial = guides._guide.cache_info()
    second = workspace_operation_skill_guidance(context("调整 App 布局", user_id="private-user-b"))
    assert first == second
    assert guides._guide.cache_info().hits == initial.hits + 2
    assert "private-user-a" not in second and "private-user-b" not in first


def test_changed_guide_content_invalidates_only_its_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(guides, "_SKILLS_ROOT", tmp_path)
    original = Path(__file__).resolve().parents[1] / "packages/core/ai/skills"
    for slug in ("workspace-app", "workspace-tasks"):
        directory = tmp_path / slug
        directory.mkdir()
        for filename in ("SKILL.md", "config.json"):
            (directory / filename).write_bytes((original / slug / filename).read_bytes())
    app_before = guides.load_workspace_operation_guide("workspace-app")
    task_before = guides.load_workspace_operation_guide("workspace-tasks")
    source = tmp_path / "workspace-app/SKILL.md"
    source.write_text(source.read_text() + "\nNew reviewed configuration behavior.\n")
    assert guides.load_workspace_operation_guide("workspace-app")[1] != app_before[1]
    assert guides.load_workspace_operation_guide("workspace-tasks") == task_before


def test_changed_tool_contract_invalidates_only_dependent_guide(monkeypatch):
    from packages.core.ai.runtime import tool_registry

    before = guides.load_workspace_operation_guide("workspace-workflows")
    app_before = guides.load_workspace_operation_guide("workspace-app")
    original = tool_registry.runtime_tool_schema

    def schema(name):
        value = original(name)
        if name == "get_workflow":
            value = deepcopy(value)
            value["function"]["parameters"]["properties"]["new_contract_field"] = {"type": "boolean"}
        return value

    monkeypatch.setattr(tool_registry, "runtime_tool_schema", schema)
    assert guides.load_workspace_operation_guide("workspace-workflows")[1] != before[1]
    assert guides.load_workspace_operation_guide("workspace-app") == app_before


@pytest.mark.parametrize("spec", [{"tools": ["invented_tool"]},
                                  {"workspace_operations": {"workspace_actions": ["invented_action"]}},
                                  {"workspace_operations": {"patch_ops": ["invented.patch"]}}])
def test_guide_catalog_cannot_advertise_invented_operations(spec):
    with pytest.raises(ValueError):
        guides.workspace_operation_tool_contract(spec)


@pytest.mark.asyncio
async def test_fixed_guide_prefix_precedes_workspace_user_and_turn_state(monkeypatch):
    monkeypatch.setattr("packages.core.ai.runtime.prompt_sections.agent_identity", lambda ctx: "Actor identity")
    monkeypatch.setattr("packages.core.ai.runtime.prompt_sections.user_context", lambda ctx: f"User {ctx.user_id}")
    first = context("调整 App 布局", user_id="actor-a", extra_context="private-workspace-a")
    second = context("调整 App 布局", user_id="actor-b", extra_context="private-workspace-b")
    first_prompt = await build_default_prompt_builder().build(first)
    second_prompt = await build_default_prompt_builder().build(second)
    prefix_a = first_prompt.split(guides.WORKSPACE_OPERATION_GUIDANCE_END)[0]
    prefix_b = second_prompt.split(guides.WORKSPACE_OPERATION_GUIDANCE_END)[0]
    assert prefix_a == prefix_b
    assert first_prompt.startswith(guides.WORKSPACE_OPERATION_GUIDANCE_START)
    assert "actor-a" not in prefix_b and "private-workspace-a" not in prefix_b
    assert "private-workspace-b" in second_prompt


def test_model_cache_breakpoint_pins_only_shared_guides(monkeypatch):
    from packages.core.ai import llm_client

    monkeypatch.setattr(llm_client, "provider_for_model", lambda _model: "anthropic")
    monkeypatch.setattr(llm_client, "_cache_min_for_model", lambda _model: 1)
    prefix = workspace_operation_skill_guidance(context("调整 App 布局"))
    payloads = [{"model": "test-model", "messages": [{"role": "system", "content": prefix + f"\n\nActor {actor}"},
                    {"role": "user", "content": "request"}]} for actor in ("a", "b")]
    for payload in payloads:
        assert llm_client._apply_prompt_cache(payload)
    first, second = [payload["messages"][0]["content"] for payload in payloads]
    assert first[0] == second[0]
    assert first[0]["cache_control"] == {"type": "ephemeral"}
    assert first[1]["text"] != second[1]["text"]
    assert "cache_control" not in first[1]


@pytest.mark.parametrize("slug", ["workspace-app", "workspace-operations"])
@pytest.mark.asyncio
async def test_seeded_workspace_guide_returns_instructions_without_child_loop(db_session, monkeypatch, slug):
    seeded = {skill.slug: skill for skill in await seed_builtin_skills(db_session)}
    for registered_slug in guides.WORKSPACE_OPERATION_SKILL_SLUGS:
        skill = seeded[registered_slug]
        assert skill.entity_id is None
        assert skill.config["execution_mode"] == "instructions_only"
        assert skill.config["allowed_surfaces"] == ["workspace_chat"]
        assert skill.config["tool_contract_sha256"]
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Guide test", status="active")
    db_session.add(workspace)
    await db_session.flush()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Instruction guide must not prepare or execute a child loop")

    monkeypatch.setattr("packages.core.ai.runtime.runtime_prepare_prompt_skill_tool_surface", forbidden)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_skill_agent_loop", forbidden)
    envelope = RuntimeResolver().resolve_trace_envelope(AIRuntimeRequest(
        surface=ChatSurface.WORKSPACE_CHAT, entity_id=entity_id, workspace_id=workspace.id,
    ), allowed_tool_names={"manor", "invoke_skill"})
    result = await invoke_skill(db_session, slug, entity_id, "修改 App 布局",
                              workspace_id=workspace.id, runtime_envelope=envelope,
                              allowed_tool_names={"manor", "invoke_skill"})
    assert result["instructions_only"] is True
    assert result["rounds"] == 0 and result["tools_used"] == []
    assert result["stop_reason"] == "instructions_loaded"
    assert result["content"].startswith(f"### Guide `{slug}`")
    if slug == "workspace-operations":
        assert "Operation guide index" in result["content"]


@pytest.mark.asyncio
async def test_reseeding_tool_contract_bumps_only_affected_skill_revision(db_session, monkeypatch):
    from packages.core.ai.runtime import tool_registry

    seeded = {skill.slug: skill for skill in await seed_builtin_skills(db_session)}
    workflow_revision = seeded["workspace-workflows"].revision
    app_revision = seeded["workspace-app"].revision
    original = tool_registry.runtime_tool_schema

    def schema(name):
        value = original(name)
        if name == "get_workflow":
            value["function"]["description"] += " New contract."
        return value

    monkeypatch.setattr(tool_registry, "runtime_tool_schema", schema)
    reseeded = {skill.slug: skill for skill in await seed_builtin_skills(db_session)}
    assert reseeded["workspace-workflows"].revision == workflow_revision + 1
    assert reseeded["workspace-app"].revision == app_revision
    repeated = {skill.slug: skill for skill in await seed_builtin_skills(db_session)}
    assert repeated["workspace-workflows"].revision == workflow_revision + 1


@pytest.mark.asyncio
async def test_split_builds_send_message_matched_guides_with_the_turn_not_the_system_prompt(monkeypatch):
    monkeypatch.setattr("packages.core.ai.runtime.prompt_sections.agent_identity", lambda ctx: "Actor identity")
    built = []
    for message in ("创建一个 Workflow 并验证部署", "暂停每天的 automation", "hello"):
        ctx = context(message)
        prompt = await build_default_prompt_builder().build(ctx, split_turn_context=True)
        built.append((prompt, ctx))
    assert len({prompt for prompt, _ in built}) == 1
    assert "### Guide `workspace-workflows`" in built[0][1].runtime_turn_context
    assert "### Guide `workspace-automations`" in built[1][1].runtime_turn_context
    assert "### Guide `workspace-" not in built[2][1].runtime_turn_context
    # The router and index stay in the shared, cache-pinned prefix.
    assert built[0][0].startswith(guides.WORKSPACE_OPERATION_GUIDANCE_START)
    assert [item["slug"] for item in built[0][1].runtime_workspace_operation_skills] == [
        "workspace-operations", "workspace-workflows",
    ]
