"""Planner selects Agent contracts without expanding their execution scope."""
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.planning import (
    runtime_execute_planner_tool_call,
    runtime_planner_system_prompt,
)
from packages.core.ai.runtime.profiles import WORKSPACE_AGENT_TOOL_PROFILE
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_surface_from_schemas
from packages.core.plans.planner import (
    TaskCapabilityGapError,
    _Context,
    _gather_context,
    _require_task_capabilities,
)


@pytest.fixture
def native_tool_pool(monkeypatch):
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools.file_tools import READ_FILE_SCHEMA
    from packages.core.ai.tools.generate_file_tool import GENERATE_FILE_SCHEMA
    from packages.core.ai.runtime import tool_registry

    pool = ToolPool()
    def unexpected_execution(*_args, **_kwargs):
        pytest.fail("Planner discovery must not execute a tool")
    for schema in [GENERATE_FILE_SCHEMA, READ_FILE_SCHEMA]:
        pool.register(schema["function"]["name"], schema, unexpected_execution)
    monkeypatch.setattr(tool_registry, "_tool_pool", lambda: pool)
    return pool


def _context():
    return _Context(
        workspace=None,
        subscriptions=[
            SimpleNamespace(service_key="briefs", id="sub_atlas", agent_id="atlas"),
            SimpleNamespace(service_key="delivery", id="sub_delivery", agent_id="coordinator"),
        ],
        agents_by_id={
            "atlas": SimpleNamespace(id="atlas", name="Atlas", description="Draft reports", system_prompt="private author instructions"),
            "coordinator": SimpleNamespace(id="coordinator", name="Coordinator", description="Save files", system_prompt="private delivery instructions"),
        },
        allowed_service_keys={"briefs", "delivery"},
        provider_actions={},
        agent_tool_names={"coordinator": ["generate_file"]},
        agent_skill_names={"atlas": [{"slug": "draft-report", "name": "Draft Report"}]},
    )


def _prompt(context):
    return runtime_planner_system_prompt(
        subscriptions=context.subscriptions,
        agents_by_id=context.agents_by_id,
        allowed_service_keys=context.allowed_service_keys,
        agent_tool_names=context.agent_tool_names,
        service_tool_names=context.service_tool_names,
        agent_skill_names=context.agent_skill_names,
    )


@pytest.mark.unit
def test_task_capability_preflight_uses_selected_service_scopes_not_union():
    context = _context()
    context.service_tool_names = {"briefs": [], "delivery": ["generate_file"]}
    task = SimpleNamespace(
        id="task-1",
        details={"runtime_context": {"required_capabilities": ["file.write"]}},
    )

    _require_task_capabilities(task, context)

    context.service_tool_names["delivery"] = []
    with pytest.raises(TaskCapabilityGapError) as exc:
        _require_task_capabilities(task, context)
    assert exc.value.task_id == "task-1"
    assert exc.value.missing_capabilities == ("file.write",)
    assert "selected Agent services" in str(exc.value)


@pytest.mark.unit
def test_task_capability_preflight_requires_bound_external_action():
    context = _context()
    context.service_tool_names = {"briefs": [], "delivery": []}
    context.provider_actions = {"instagram": ["social_post.publish"]}
    task = SimpleNamespace(
        id="task-2",
        details={"runtime_context": {"required_capabilities": ["external.social"]}},
    )

    with pytest.raises(TaskCapabilityGapError, match="external.social"):
        _require_task_capabilities(task, context)

    context.service_provider_actions = {
        "delivery": {"instagram": ["social_post.publish"]},
    }
    _require_task_capabilities(task, context)


@pytest.mark.unit
def test_task_capability_preflight_rejects_unknown_persisted_requirement():
    context = _context()
    context.service_tool_names = {"briefs": [], "delivery": ["generate_file"]}
    task = SimpleNamespace(
        id="task-3",
        details={"runtime_context": {
            "required_capabilities": ["file.write", "unknown.capability"],
        }},
    )

    with pytest.raises(TaskCapabilityGapError, match="unknown runtime capability"):
        _require_task_capabilities(task, context)


@pytest.mark.asyncio
async def test_task_preflight_recognizes_mcp_from_agent_workspace_deployment(db_session, native_tool_pool):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    tool_name = "mcp__instagram__social_post.publish"
    native_tool_pool.register(tool_name, {
        "type": "function", "function": {
            "name": tool_name, "parameters": {"type": "object", "properties": {}},
        },
    }, lambda *_args, **_kwargs: pytest.fail("Preflight cannot execute provider work"))
    entity_id, workspace_id, agent_id = (generate_ulid() for _ in range(3))
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Scoped MCP", operating_model={
        "capability_bindings": [{
            "owner_scope": "service", "owner_service_key": "publisher",
            "capability_type": "mcp", "integration_key": "instagram",
            "allowed_tools": ["social_post.publish"],
        }],
    })
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        title="Publish approved content", owner_service_key="publisher",
        details={"runtime_context": {"required_capabilities": ["external.social"]}},
    )
    db_session.add_all([
        workspace, task, Agent(id=agent_id, entity_id=entity_id, name="Publisher", status="active"),
        AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
                          agent_id=agent_id, service_key="publisher", status="active"),
    ])
    await db_session.flush()

    context = await _gather_context(db_session, task)
    assert context.service_mcp_tool_names["publisher"] == [tool_name]
    _require_task_capabilities(task, context)
    descriptor = runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "publisher"}, context=context)
    assert "external.social" in {row["id"] for row in descriptor["capabilities"]}

    workspace.operating_model = {}
    await db_session.flush()
    context = await _gather_context(db_session, task)
    with pytest.raises(TaskCapabilityGapError, match="external.social"):
        _require_task_capabilities(task, context)
    descriptor = runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "publisher"}, context=context)
    assert "external.social" not in {row["id"] for row in descriptor["capabilities"]}


@pytest.mark.unit
def test_planner_attributes_tools_and_skills_to_their_service():
    context = _context()
    prompt = _prompt(context)
    assert '"name":"Atlas","description":"Draft reports"' in prompt
    assert '"name":"Coordinator","description":"Save files"' in prompt
    assert "generate_file" not in prompt and "draft-report" not in prompt
    assert "private author instructions" not in prompt
    assert "Preserve requested authorship and delegate limits" in prompt
    atlas_schema = runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "briefs"}, context=context)
    delivery_schema = runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "delivery"}, context=context)
    assert "file.write" not in {row["id"] for row in atlas_schema["capabilities"]}
    assert "file.write" in {row["id"] for row in delivery_schema["capabilities"]}
    assert "generate_file" not in json.dumps(delivery_schema)

    atlas = runtime_execute_planner_tool_call("list_tools", {"service_key": "briefs"}, context=context)
    delivery = runtime_execute_planner_tool_call("list_tools", {"service_key": "delivery"}, context=context)
    assert atlas["platform_tools"] == []
    assert "file.write" not in {row["capability_id"] for row in atlas["capability_catalog"]}
    assert atlas["skills"] == context.agent_skill_names["atlas"]
    assert delivery["platform_tools"] == ["generate_file"]
    assert delivery["skills"] == []


@pytest.mark.unit
def test_planner_catalog_omits_bindings_outside_allowed_services():
    context = _context()
    context.allowed_service_keys = {"briefs"}
    prompt = _prompt(context)
    advertised = prompt

    assert '"name":"Atlas"' in advertised
    assert "Coordinator" not in advertised
    assert "generate_file" not in advertised
    assert "file.write" not in advertised


@pytest.mark.unit
@pytest.mark.parametrize("tool_profile", [None, WORKSPACE_AGENT_TOOL_PROFILE])
def test_empty_bindings_keep_runtime_helpers_without_borrowing_file_tools(tool_profile):
    registry = [
        (name, {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}})
        for name in ["search_tools", "generate_file"]
    ]
    atlas = runtime_registered_tool_surface_from_schemas(
        registry, bound_tool_names=set(), is_master=False, tool_profile=tool_profile,
    )
    delivery = runtime_registered_tool_surface_from_schemas(
        registry, bound_tool_names={"generate_file"}, is_master=False, tool_profile=tool_profile,
    )

    assert "search_tools" in atlas.visible_tool_names
    assert "generate_file" not in atlas.visible_tool_names
    assert "generate_file" in delivery.visible_tool_names
    assert "Native platform tools are not action providers" in _prompt(_context())


@pytest.mark.asyncio
async def test_planner_context_omits_revoked_tools_and_skills(db_session, native_tool_pool):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition

    entity_id, workspace_id, agent_id = (generate_ulid() for _ in range(3))
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, title="Scoped capabilities")
    active_tool = ToolDefinition(id=generate_ulid(), name="generate_file", status="active")
    revoked_tool = ToolDefinition(id=generate_ulid(), name="read_file", status="disabled")
    skill_rows = [
        Skill(
            id=generate_ulid(), entity_id=entity_id, name=name, slug=name,
            system_prompt="Draft a report", status=status,
        )
        for name, status in [
            ("active-skill", "active"),
            ("revoked-binding", "active"),
            ("disabled-skill", "disabled"),
        ]
    ]
    skill_bindings = [
        AgentSkillBinding(
            id=generate_ulid(), agent_id=agent_id, skill_id=skill.id,
            status="disabled" if skill.slug == "revoked-binding" else "active",
        )
        for skill in skill_rows
    ]
    db_session.add_all([
        task,
        Agent(id=agent_id, entity_id=entity_id, name="Writer", status="active"),
        AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
            agent_id=agent_id, service_key="writer", status="active",
        ),
        active_tool, revoked_tool,
        AgentToolBinding(agent_id=agent_id, tool_id=active_tool.id),
        AgentToolBinding(agent_id=agent_id, tool_id=revoked_tool.id),
        *skill_rows, *skill_bindings,
    ])
    await db_session.flush()

    context = await _gather_context(db_session, task)

    # A write-capable Agent receives the runtime's read companion so it can
    # inspect its generated artifact.  This synthetic grant is independent of
    # the disabled read_file ToolDefinition row above; the disabled binding
    # must not suppress or duplicate the governed companion capability.
    assert context.agent_tool_names == {
        agent_id: [active_tool.name, revoked_tool.name],
    }
    assert [row["slug"] for row in context.agent_skill_names[agent_id]] == ["active-skill"]
    assert len((await db_session.execute(
        select(AgentToolBinding).where(AgentToolBinding.agent_id == agent_id)
    )).scalars().all()) == 2
    assert [binding.status for binding in skill_bindings] == ["active", "disabled", "active"]


@pytest.mark.unit
def test_native_schema_uses_real_registry_without_minting_an_action(native_tool_pool):
    from packages.core.plans.planner import CapabilityError, _enforce_allowlists
    from packages.core.plans.schema import Plan

    context = _context()
    result = runtime_execute_planner_tool_call("get_tool_schema", {
        "provider": "platform", "action_key": "generate_file", "service_key": "delivery",
    }, context=context)
    assert result["available"] is True
    assert result["input_schema"] == native_tool_pool.get_schema("generate_file")["function"]["parameters"]
    assert result["kind"] == "platform_tool"
    assert result["step_kind"] == "subagent"
    assert result["service_key"] == "delivery"
    assert "provider" not in result and "action_key" not in result
    assert "no action provider/action_key binding" in result["hint"]

    valid = Plan.model_validate({"steps": [{
        "key": "save", "kind": "subagent", "service_key": "delivery",
        "params": {"prompt": "Use generate_file to save the report."}, "output_shape": "ArtifactResult",
    }]})
    _enforce_allowlists(valid, context)
    fake_action = Plan.model_validate({"steps": [{
        "key": "save", "kind": "action", "service_key": "delivery",
        "provider": "platform", "action_key": "generate_file", "params": {},
    }]})
    with pytest.raises(CapabilityError, match="provider='platform' not available"):
        _enforce_allowlists(fake_action, context)


@pytest.mark.unit
@pytest.mark.parametrize("service_key, tool_name", [
    ("briefs", "generate_file"), ("delivery", "read_file"),
    ("unknown", "generate_file"), ("", "generate_file"),
    ("delivery", "not_registered"), ("delivery", "mcp__hidden__action"),
])
def test_native_schema_does_not_borrow_or_invent_permissions(native_tool_pool, service_key, tool_name):
    context = _context()
    context.agent_tool_names["coordinator"].extend(["not_registered", "mcp__hidden__action"])
    result = runtime_execute_planner_tool_call("get_tool_schema", {
        "provider": "platform", "action_key": tool_name, "service_key": service_key,
    }, context=context)
    assert result["available"] is False
    assert "input_schema" not in result


@pytest.mark.unit
def test_native_schema_can_infer_the_only_allowed_service(native_tool_pool):
    context = _context()
    context.allowed_service_keys = {"delivery"}
    result = runtime_execute_planner_tool_call("get_tool_schema", {
        "provider": "platform", "action_key": "generate_file",
    }, context=context)
    assert result["available"] is True
    assert result["service_key"] == "delivery"
    assert "error" in runtime_execute_planner_tool_call("list_tools", {"service_key": "briefs"}, context=context)


@pytest.mark.asyncio
async def test_planner_resolves_each_subscription_scope_and_preserves_actor(db_session, native_tool_pool, monkeypatch):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services import workspace_runtime, task_requester_identity

    entity_id, workspace_id, agent_id, actor_id = (generate_ulid() for _ in range(4))
    workspace = Workspace(id=workspace_id, entity_id=entity_id, name="Scoped", operating_model={
        "capability_bindings": [{
            "owner_scope": "service", "owner_service_key": "delivery",
            "capability_type": "tool", "tool_name": "generate_file",
        }],
    })
    subs = [AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        agent_id=agent_id, service_key=key, status="active",
    ) for key in ["briefs", "delivery"]]
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
                title="Save a report", owner_service_key="briefs", delegate_service_keys=["delivery"])
    db_session.add_all([workspace, task, *subs, Agent(id=agent_id, entity_id=entity_id, name="Shared", status="active")])
    await db_session.flush()
    async def actor(_db, _task):
        return actor_id
    monkeypatch.setattr(task_requester_identity, "resolve_task_execution_user_id", actor)
    calls = []
    real_resolve = workspace_runtime.resolve_workspace_runtime
    async def record_resolve(db, **kwargs):
        calls.append(kwargs)
        return await real_resolve(db, **kwargs)
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", record_resolve)

    context = await _gather_context(db_session, task)
    assert context.service_tool_names == {"briefs": [], "delivery": ["generate_file"]}
    assert context.agent_tool_names == {agent_id: ["generate_file"]}
    assert {call["agent_subscription_id"] for call in calls} == {sub.id for sub in subs}
    assert all(call["user_id"] == actor_id and call["task_id"] == task.id
               and call["runtime_surface"] == "scheduled_agent_run"
               and call["include_prompt_context"] is False for call in calls)
    for service_key, available in [("briefs", False), ("delivery", True)]:
        result = runtime_execute_planner_tool_call("get_tool_schema", {
            "provider": "platform", "action_key": "generate_file", "service_key": service_key,
        }, context=context)
        assert result["available"] is available
    prompt = _prompt(context)
    assert "generate_file" not in prompt
    briefs_schema = runtime_execute_planner_tool_call("get_agent_schema", {"service_key": "briefs"}, context=context)
    assert "file.write" not in {row["id"] for row in briefs_schema["capabilities"]}

    wrong_scope = await real_resolve(
        db_session, entity_id=entity_id, user_id=actor_id, agent_id=agent_id,
        agent_subscription_id=generate_ulid(), workspace_id=workspace_id, task_id=task.id,
        runtime_surface="scheduled_agent_run", include_prompt_context=False,
    )
    assert "generate_file" not in (wrong_scope.bound_tool_names or set())

    duplicate_agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Duplicate", status="active")
    db_session.add_all([duplicate_agent, AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        agent_id=duplicate_agent.id, service_key="delivery", status="active",
    )])
    await db_session.flush()
    from packages.core.constants.task import TaskRuntimeKind
    from packages.core.services.task_execution import resolve_task_execution

    ambiguous_context = await _gather_context(db_session, task)
    assert ambiguous_context.subscriptions == []
    assert ambiguous_context.allowed_service_keys == set()
    assert ambiguous_context.service_tool_names == {}
    dispatch = await resolve_task_execution(db_session, task)
    assert dispatch.runtime_kind is TaskRuntimeKind.MANUAL
    assert dispatch.coordinator is None
    task.delegate_service_keys = []
    context = await _gather_context(db_session, task)
    assert context.service_tool_names == {"briefs": []}
