"""Workspace config changes must produce the reviewed Agent and Skill bindings."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.models.workspace import WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations


def _state():
    return operations._sync_operating_model_from_state({
        "operating_model": {},
        "services": [
            {"key": "market_research", "name": "Market Research"},
            {"key": "marketing_content", "name": "Marketing Content"},
            {"key": "lead_nurturing", "name": "Lead Nurturing"},
        ],
        "agent_mappings": [], "rules": [], "governance_policy": {},
    })


def test_staging_marketing_video_object_cannot_erase_existing_services():
    before = _state()
    state = deepcopy(before)
    # Shape of staging operation 01M27ZKK6C4961KHVF53PB40TT: accepted as
    # services=[] while claiming the requested Agent/Skills were applied.
    patch = {"op": "operating_model.update", "payload": {"services": {
        "marketing_video": {
            "name": "Marketing Video Producer",
            "agent": "B2B Foodservice Marketing Video Producer",
            "skills": [{"name": "Video Script", "skill_key": "video_script"}],
        },
    }}}
    with pytest.raises(ValueError, match="services.*array"):
        operations._apply_single_patch(state, patch)
    assert state == before


@pytest.mark.parametrize("op", ["operating_model.update", "operating_model.replace"])
@pytest.mark.parametrize("key,bad_value", [
    *[(key, {}) for key in (
        "services", "goals", "rules", "automations", "capability_bindings",
        "skill_bindings", "processes", "agent_mappings",
    )],
    *[(key, []) for key in (
        "evaluation", "knowledge", "knowledge_policy", "integration_scope",
        "channel_config", "strategist", "governance", "governance_policy",
    )],
])
def test_wrong_model_section_type_is_rejected_before_any_state_mutation(op, key, bad_value):
    before = _state()
    state = deepcopy(before)
    with pytest.raises(ValueError, match=key):
        operations._apply_single_patch(state, {"op": op, "payload": {key: bad_value}})
    assert state == before


@pytest.mark.parametrize("op", ["service_role.upsert", "operating_model.update"])
@pytest.mark.parametrize("field,value", [("agent", "Video Agent"), ("skills", ["video_script"])])
def test_service_metadata_cannot_impersonate_agent_or_skill_provisioning(op, field, value):
    service = {"key": "video", "name": "Video", field: value}
    payload = {"service": service} if op == "service_role.upsert" else {"services": [service]}
    with pytest.raises(ValueError, match="agent_mapping.upsert"):
        operations._apply_single_patch(_state(), {"op": op, "payload": payload})


@pytest.mark.parametrize("selection", [
    {"recommended_agent_id": "new-agent"},
    {"strategy": "create_custom", "create_agent_draft": {"agent_name": "Video Agent"}},
])
def test_selecting_an_agent_does_not_retain_the_previous_identity(selection):
    state = _state()
    state["agent_mappings"] = [{
        "service_key": "marketing_content", "agent_id": "old-agent",
        "custom_prompt": "Workspace-specific context", "config": {"assignee_kind": "agent"},
    }]
    after = operations._apply_single_patch(state, {"op": "agent_mapping.upsert", "payload": {
        "mapping": {"service_key": "marketing_content", **selection},
    }})
    mapping = after["agent_mappings"][0]
    assert not mapping.get("agent_id")
    assert mapping["custom_prompt"] == "Workspace-specific context"
    assert mapping["config"] == {"assignee_kind": "agent"}
    assert all(mapping[key] == value for key, value in selection.items())


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping,path", [
    ({"service_key": "marketing_content"}, "agent_id"),
    ({"agent_id": "agent"}, "service_key"),
    ({"service_key": "marketing_content", "agent_id": "agent", "config": []}, "config"),
    ({"service_key": "marketing_content", "strategy": "create_custom", "create_agent_draft": []}, "create_agent_draft"),
])
async def test_unmaterializable_mapping_is_invalid_instead_of_successfully_skipped(mapping, path):
    before = _state()
    after = deepcopy(before)
    after["agent_mappings"] = [mapping]
    draft = WorkspaceOperationDraft(
        id="draft", entity_id="entity", workspace_id="workspace", status="open",
        current_state=after, patches=[],
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_Rows([
        SimpleNamespace(id="agent", entity_id="entity", is_template=False),
    ])))
    validation = await operations.validate_operation_draft(db, draft, current_state=before)
    assert validation["valid"] is False
    assert any(error["path"] == f"agent_mappings[0].{path}" for error in validation["errors"])


@pytest.mark.asyncio
async def test_subscription_config_is_materialized_and_counted_as_updated(monkeypatch):
    subscription = SimpleNamespace(
        agent_id="agent", service_key="marketing_content", custom_prompt="Existing prompt",
        config={"assignee_kind": "agent"}, status="active",
    )
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Rows([subscription]), _Rows([])]), flush=AsyncMock(),
    )
    map_agent = AsyncMock(return_value=subscription)
    monkeypatch.setattr("packages.core.services.workspace_service.map_agent_to_service", map_agent)
    workspace = SimpleNamespace(id="workspace", entity_id="entity", name="Campaign")
    result = await operations._sync_agent_mappings_from_operation_state(db, workspace, [{
        "service_key": "marketing_content", "agent_id": "agent",
        "config": {"assignee_kind": "ai"},
    }], deactivate_missing=False)
    assert subscription.config == {"assignee_kind": "ai"}
    assert result["updated"] == 1
    assert result["unchanged"] == 0


@pytest.mark.asyncio
async def test_requested_skill_design_is_not_discarded_for_an_existing_same_name_agent(monkeypatch):
    old_agent = SimpleNamespace(id="old-agent", name="Video Agent")
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_Rows([]), _Rows([old_agent])]), flush=AsyncMock())
    provision = AsyncMock(return_value=("new-agent", "Video Agent"))
    map_agent = AsyncMock(return_value=SimpleNamespace(config={}))
    monkeypatch.setattr("packages.core.services.workspace_setup_service._provision_custom_agent_for_mapping", provision)
    monkeypatch.setattr("packages.core.services.workspace_service.map_agent_to_service", map_agent)
    workspace = SimpleNamespace(id="workspace", entity_id="entity", name="Campaign", operating_context="", primary_work="")
    result = await operations._sync_agent_mappings_from_operation_state(db, workspace, [{
        "service_key": "video", "strategy": "create_custom", "create_agent_draft": {
            "agent_name": "Video Agent", "skill_bindings": ["video-script"],
        },
    }], deactivate_missing=False, requester_user_id="requester")
    assert result["custom_agents"] == 1
    assert result["reused_existing_agents"] == 0
    assert provision.await_args.kwargs["requester_user_id"] == "requester"
    assert map_agent.await_args.args[4] == "new-agent"


@pytest.mark.asyncio
async def test_workspace_provision_adapter_preserves_the_actor_skill_boundary(monkeypatch):
    from packages.core.services.workspace_setup_service import _provision_custom_agent_for_mapping
    provision = AsyncMock(return_value=SimpleNamespace(agent_id="agent", agent_name="Video", warnings=[]))
    monkeypatch.setattr("packages.core.services.agent_provisioning_service.provision_custom_agent", provision)
    await _provision_custom_agent_for_mapping(
        SimpleNamespace(), entity_id="entity", workspace_id="workspace", workspace_name="Campaign",
        operating_context="", primary_work="", requester_user_id="requester",
        mapping={"service_key": "video", "create_agent_draft": {"skill_bindings": ["video-script"]}},
    )
    assert provision.await_args.kwargs["requester_user_id"] == "requester"
    assert provision.await_args.kwargs["spec"].skill_bindings == ["video-script"]


@pytest.mark.parametrize("op,payload", [
    ("goals.replace", {"goals": {"sales": {"target_value": 5}}}),
    ("rules.replace", {"rules": {"approval": {"description": "Ask before publishing"}}}),
    ("agent_mappings.replace", {"mappings": {"video": {"agent_id": "agent"}}}),
    ("capability_binding.upsert", {"bindings": [{"tool_name": "read_file"}, "lost tool"]}),
    ("evaluation.update", {"evaluation": []}),
    ("goal.upsert", {"goal": []}),
    ("rule.update", {"rule_key": "approval", "rule": []}),
    ("skill_binding.upsert", {"binding": ["video-script"]}),
    ("channel.upsert", {"channel": []}),
    ("process.upsert", {"process": []}),
    ("task.prepare", {"task": []}),
])
def test_standalone_wrappers_and_replacements_cannot_silently_discard_configuration(op, payload):
    before = _state()
    state = deepcopy(before)
    with pytest.raises(ValueError):
        operations._apply_single_patch(state, {"op": op, "payload": payload})
    assert state == before


@pytest.mark.asyncio
async def test_open_legacy_draft_with_already_coerced_service_snapshot_cannot_apply():
    before = _state()
    after = deepcopy(before)
    after["services"] = []
    after["operating_model"]["services"] = []
    draft = WorkspaceOperationDraft(
        id="draft", entity_id="entity", workspace_id="workspace", status="open", current_state=after,
        patches=[{"op": "operating_model.update", "payload": {"services": {"video": {"agent": "Video"}}}}],
    )
    validation = await operations.validate_operation_draft(SimpleNamespace(), draft, current_state=before)
    assert validation["valid"] is False
    assert any(error["code"] == "invalid_patch_shape" for error in validation["errors"])


@pytest.mark.asyncio
async def test_adding_a_service_cannot_redeploy_the_agent_from_another_service():
    before = _state()
    before["agent_mappings"] = [{"service_key": "marketing_content", "agent_id": "agent"}]
    after = deepcopy(before)
    after["agent_mappings"].append({"service_key": "lead_nurturing", "recommended_agent_id": "agent"})
    draft = WorkspaceOperationDraft(
        id="draft", entity_id="entity", workspace_id="workspace", status="open", current_state=after, patches=[],
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=_Rows([
        SimpleNamespace(id="agent", entity_id="entity", is_template=False),
    ])))
    validation = await operations.validate_operation_draft(db, draft, current_state=before)
    assert validation["valid"] is False
    assert any("already mapped" in error["message"] for error in validation["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_back", [False, True])
async def test_unmapped_agent_reuses_its_exact_subscription_on_readdition(db_session, switch_back):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.services.workspace_service import map_agent_to_service, unmap_agent_from_service

    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Campaign", status="active")
    original = Agent(id=generate_ulid(), entity_id=entity_id, name="Original", status="active")
    replacement = Agent(id=generate_ulid(), entity_id=entity_id, name="Replacement", status="active")
    db_session.add_all([workspace, original, replacement])
    await db_session.flush()
    subscription = await map_agent_to_service(db_session, workspace.id, entity_id, "video", original.id)
    original_subscription_id = subscription.id
    await unmap_agent_from_service(db_session, workspace.id, entity_id, "video")
    if switch_back:
        await map_agent_to_service(db_session, workspace.id, entity_id, "video", replacement.id)
    restored = await map_agent_to_service(db_session, workspace.id, entity_id, "video", original.id)
    assert restored.id == original_subscription_id
    assert restored.status == "active"
    active = list((await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace.id, AgentSubscription.status == "active",
    ))).scalars().all())
    assert [row.id for row in active] == [original_subscription_id]


async def _seed_agent_operation(db_session, *, accessible=True):
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import Skill
    from packages.core.models.user import User
    from packages.core.models.workspace import Agent, ToolDefinition, Workspace
    from packages.core.services.agent_provisioning_service import spec_from_create_agent_draft
    from packages.core.services.workspace_service import map_agent_to_service

    entity_id = generate_ulid()
    requester = User(id=generate_ulid(), entity_id=entity_id, role="member", status="active",
                     email=f"agent-configuration-{generate_ulid()}@example.com", password_hash="test")
    owner = User(id=generate_ulid(), entity_id=entity_id, role="member", status="active",
                 email=f"skill-owner-{generate_ulid()}@example.com", password_hash="test")
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Campaign", status="paused",
                          operating_model={"services": _state()["services"]})
    skill = Skill(id=generate_ulid(), entity_id=entity_id, name="Video scripting",
                  slug=f"video-script-{generate_ulid()}", system_prompt="Write a factual video script.",
                  owner_user_id=requester.id if accessible else owner.id,
                  visibility="private", is_public=False, status="active")
    agents = [Agent(id=generate_ulid(), entity_id=entity_id, name=row["name"], status="active")
              for row in _state()["services"]]
    db_session.add_all([requester, owner, workspace, skill, *agents])
    await db_session.flush()
    subscriptions = []
    for service, agent in zip(_state()["services"], agents, strict=True):
        subscriptions.append(await map_agent_to_service(db_session, workspace.id, entity_id, service["key"], agent.id))
    design = {"agent_name": "Video Agent", "system_prompt": "Produce marketing videos.",
              "skill_bindings": [skill.id], "tool_bindings": ["read_file"]}
    tool_names = spec_from_create_agent_draft(design, service_key="marketing_video").tool_bindings
    existing = set((await db_session.execute(select(ToolDefinition.name).where(ToolDefinition.name.in_(tool_names)))).scalars().all())
    db_session.add_all([ToolDefinition(id=generate_ulid(), name=name, status="active") for name in tool_names if name not in existing])
    await db_session.flush()
    return SimpleNamespace(workspace=workspace, requester=requester, skill=skill, design=design,
                           original_ids=[sub.id for sub in subscriptions], tool_names=set(tool_names))


def _add_video_patches(design):
    return [
        {"op": "service_role.upsert", "payload": {"key": "marketing_video", "name": "Marketing Video"}},
        {"op": "agent_mapping.upsert", "payload": {"mapping": {
            "service_key": "marketing_video", "strategy": "create_custom", "create_agent_draft": design,
        }}},
    ]


@pytest.mark.asyncio
async def test_approved_additive_agent_operation_persists_its_requested_skill_and_tools(db_session):
    from sqlalchemy import select
    from packages.core.models.skill import AgentSkillBinding
    from packages.core.models.workspace import AgentSubscription, AgentToolBinding, ToolDefinition, Workspace

    seeded = await _seed_agent_operation(db_session)
    workspace_id, entity_id = seeded.workspace.id, seeded.workspace.entity_id
    draft = await operations.create_operation_draft(db_session, workspace_id, entity_id,
        user_id=seeded.requester.id, initial_patches=_add_video_patches(seeded.design))
    assert draft.validation["valid"], draft.validation
    result = await operations.apply_operation_draft(db_session, draft.id, entity_id, workspace_id,
        user_id=seeded.requester.id, user_confirmation=True)
    assert result["agent_mappings"]["created"] == 1
    assert result["agent_mappings"]["custom_agents"] == 1
    assert result["agent_mappings"]["skipped"] == 0
    await db_session.commit()
    db_session.expire_all()
    workspace = await db_session.get(Workspace, workspace_id)
    subscriptions = list((await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.status == "active",
    ))).scalars().all())
    assert len(workspace.operating_model["services"]) == 4
    assert len(subscriptions) == 4
    assert set(seeded.original_ids).issubset({sub.id for sub in subscriptions})
    video = next(sub for sub in subscriptions if sub.service_key == "marketing_video")
    skill_ids = set((await db_session.execute(select(AgentSkillBinding.skill_id).where(
        AgentSkillBinding.agent_id == video.agent_id,
    ))).scalars().all())
    assert seeded.design["skill_bindings"][0] in skill_ids
    tools = set((await db_session.execute(select(ToolDefinition.name).join(
        AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id,
    ).where(AgentToolBinding.agent_id == video.agent_id))).scalars().all())
    assert seeded.tool_names.issubset(tools)
    persisted_video = next(mapping for mapping in workspace.operating_model["agent_mappings"]
                           if mapping["service_key"] == "marketing_video")
    assert persisted_video["agent_id"] == video.agent_id
    assert "create_agent_draft" not in persisted_video
    # Legacy applied operations retained a create_custom design in JSON. An
    # additive repair must keep the live deployment and canonicalize it once.
    legacy_mappings = [mapping for mapping in workspace.operating_model["agent_mappings"]
                       if mapping["service_key"] != "marketing_video"]
    legacy_mappings.append(_add_video_patches(seeded.design)[1]["payload"]["mapping"])
    workspace.operating_model = {**workspace.operating_model, "agent_mappings": legacy_mappings}
    await db_session.flush()
    for _ in range(2):
        repaired = await operations.repair_workspace_operation_runtime(db_session, workspace_id, entity_id)
        assert repaired["agent_mappings"]["custom_agents"] == 0
    repaired_video = next(mapping for mapping in workspace.operating_model["agent_mappings"]
                          if mapping["service_key"] == "marketing_video")
    assert repaired_video["agent_id"] == video.agent_id


@pytest.mark.asyncio
async def test_echoed_creation_design_does_not_redeploy_a_live_service(db_session):
    """Staging 2026-11-01: a Workspace created from a draft kept the draft's
    create_custom design in its operating model. The Rules page saves the whole
    model back, so the first saved rule provisioned a second Agent, rewired the
    service to it, and stranded the Task running on the first one."""
    from sqlalchemy import func, select
    from packages.core.models.workspace import Agent, AgentSubscription

    seeded = await _seed_agent_operation(db_session)
    workspace = seeded.workspace
    workspace_id, entity_id = workspace.id, workspace.entity_id
    before = {sub.service_key: sub.agent_id for sub in (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.status == "active",
    ))).scalars().all()}
    agents_before = await db_session.scalar(select(func.count()).select_from(Agent).where(Agent.entity_id == entity_id))
    # What finalize_setup left behind: the draft's design rows, not the deployment.
    workspace.operating_model = {**workspace.operating_model, "agent_mappings": [
        {"service_key": key, "strategy": "create_custom", "create_agent_draft": {
            "agent_name": f"{key} writer", "system_prompt": f"Handle {key}.",
        }} for key in before
    ]}
    await db_session.flush()

    echoed = deepcopy(workspace.operating_model)
    echoed["rules"] = [{"name": "Sign off", "description": "End every note with: Reviewed by DSA."}]
    draft = await operations.create_operation_draft(db_session, workspace_id, entity_id,
        user_id=seeded.requester.id,
        initial_patches=[{"op": "operating_model.replace", "payload": {"operating_model": echoed}}])
    assert draft.validation["valid"], draft.validation
    result = await operations.apply_operation_draft(db_session, draft.id, entity_id, workspace_id,
        user_id=seeded.requester.id, user_confirmation=True)

    assert result["agent_mappings"]["custom_agents"] == 0
    after = {sub.service_key: sub.agent_id for sub in (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.status == "active",
    ))).scalars().all()}
    assert after == before
    assert await db_session.scalar(
        select(func.count()).select_from(Agent).where(Agent.entity_id == entity_id)
    ) == agents_before


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_unavailable_requested_skill_leaves_agent_operation_unapplied(db_session, missing):
    from sqlalchemy import select
    from packages.core.models.workspace import AgentSubscription, Workspace
    from packages.core.services.workspace_setup_service import WorkspaceProvisioningError

    seeded = await _seed_agent_operation(db_session, accessible=False)
    workspace_id, entity_id, requester_id = seeded.workspace.id, seeded.workspace.entity_id, seeded.requester.id
    if missing:
        seeded.design["skill_bindings"] = ["missing-video-skill"]
    draft = await operations.create_operation_draft(db_session, workspace_id, entity_id,
        user_id=requester_id, initial_patches=_add_video_patches(seeded.design))
    draft_id = draft.id
    await db_session.commit()
    with pytest.raises(WorkspaceProvisioningError, match="skill not found"):
        async with db_session.begin_nested():
            await operations.apply_operation_draft(db_session, draft_id, entity_id, workspace_id,
                user_id=requester_id, user_confirmation=True)
    db_session.expire_all()
    workspace = await db_session.get(Workspace, workspace_id)
    retained_draft = await db_session.get(WorkspaceOperationDraft, draft_id)
    assert workspace.operation_revision == 0
    assert len(workspace.operating_model["services"]) == 3
    assert retained_draft.status == "open"
    subscriptions = list((await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.status == "active",
    ))).scalars().all())
    assert {sub.id for sub in subscriptions} == set(seeded.original_ids)


@pytest.mark.parametrize("op,field,state_key", [
    ("rules.replace", "rules", "rules"),
    ("goals.replace", "goals", "goals"),
    ("agent_mappings.replace", "mappings", "agent_mappings"),
])
def test_collection_replacement_requires_explicit_array_to_clear(op, field, state_key):
    before = _state()
    state = deepcopy(before)
    with pytest.raises(ValueError, match="explicit"):
        operations._apply_single_patch(state, {"op": op, "payload": {}})
    assert state == before
    cleared = operations._apply_single_patch(state, {"op": op, "payload": {field: []}})
    assert cleared[state_key] == []
