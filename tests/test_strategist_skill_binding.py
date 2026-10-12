from __future__ import annotations

import pytest
from sqlalchemy import select
from types import SimpleNamespace

from packages.core.ai.runtime import runtime_strategist_system_prompt
from packages.core.models.base import generate_ulid
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.strategist import service
from packages.core.strategist import context
from packages.core.strategist.proposal import Deliverable, EstimatedImpact, Proposal, ProposedTask


def _proposal(skill_slug: str) -> Proposal:
    return Proposal(
        review_id=generate_ulid(),
        summary="Record one release state.",
        tasks=[ProposedTask(
            title="Record release state",
            owner_service_key="release_operations",
            required_skills=[skill_slug],
            deliverables=[Deliverable(
                name="release_state",
                kind="file",
                shape="DocumentResult",
                acceptance="Saved and read back",
                usage="Release intake",
            )],
        )],
    )


def test_unlinked_estimated_impact_has_no_metric_delta() -> None:
    impact = EstimatedImpact(goal_id=None, metric_delta=10.0, rationale="Intake clarity")
    assert impact.metric_delta is None


@pytest.mark.asyncio
async def test_strategist_persists_only_owner_bound_required_skill(db_session) -> None:
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Release", status="paused")
    agent = Agent(id=generate_ulid(), entity_id=workspace.entity_id, name="Coordinator", status="active")
    subscription = AgentSubscription(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="release_operations", status="active",
    )
    skill = Skill(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        name="Release intake", slug="jr-release-intake-state", system_prompt="Intake", status="active",
        description="Record an isolated release state.",
        input_schema={"type": "object", "required": ["source_ref"], "properties": {
            "source_ref": {"type": "string", "description": "Canonical source Document reference."},
            "private_default": {"default": "must-not-enter-strategist-context"},
        }},
    )
    binding = AgentSkillBinding(agent_id=agent.id, skill_id=skill.id, status="active")
    db_session.add_all([workspace, agent, subscription, skill, binding])
    await db_session.flush()

    proposal = _proposal(skill.slug)
    task_ids = await service._persist_tasks(db_session, workspace, proposal)
    task = (await db_session.execute(select(Task).where(Task.id == task_ids[0]))).scalar_one()

    assert task.required_skills == [skill.slug]
    assert task.owner_subscription_id == subscription.id
    assert task.details["strategist_review_id"] == proposal.review_id

    available = await context._bound_skills_by_service(db_session, [subscription], workspace.entity_id, workspace.id)
    assert available == {"release_operations": [skill.slug]}
    contracts = await context._bound_skill_contracts_by_service(
        db_session, [subscription], workspace.entity_id, workspace.id,
    )
    assert contracts["release_operations"][0]["required_inputs"] == {
        "source_ref": "Canonical source Document reference.",
    }
    assert "must-not-enter-strategist-context" not in str(contracts)


def test_strategist_v2_prompt_names_owner_skills() -> None:
    agent = SimpleNamespace(id="agent", name="Coordinator")
    ctx = SimpleNamespace(
        workspace=SimpleNamespace(name="Release"),
        subscriptions=[SimpleNamespace(agent_id="agent", service_key="release_operations", config={"assignee_kind": "agent"})],
        agents_by_id={"agent": agent},
        allowed_service_keys=["release_operations"],
        bound_skills_by_service={"release_operations": ["jr-release-intake-state"]},
        bound_skill_contracts_by_service={"release_operations": [{
            "slug": "jr-release-intake-state", "description": "Record a release.",
            "required_inputs": {"source_ref": "Canonical Document reference"},
        }]},
        strategist_template={},
    )

    prompt = runtime_strategist_system_prompt(ctx, briefing_mode=True)
    assert "jr-release-intake-state" in prompt
    assert '"required_skills"' in prompt
    assert "source_ref" in prompt
    assert "Canonical Document reference" in prompt
    assert "depends_on_task_keys" in prompt


@pytest.mark.asyncio
async def test_strategist_rejects_skill_not_bound_to_owner(db_session) -> None:
    workspace = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Release", status="paused")
    owner = Agent(id=generate_ulid(), entity_id=workspace.entity_id, name="Coordinator", status="active")
    other = Agent(id=generate_ulid(), entity_id=workspace.entity_id, name="Specialist", status="active")
    subscription = AgentSubscription(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        agent_id=owner.id, service_key="release_operations", status="active",
    )
    skill = Skill(
        id=generate_ulid(), entity_id=workspace.entity_id, workspace_id=workspace.id,
        name="Other skill", slug="other-skill", system_prompt="Other", status="active",
    )
    binding = AgentSkillBinding(agent_id=other.id, skill_id=skill.id, status="active")
    db_session.add_all([workspace, owner, other, subscription, skill, binding])
    await db_session.flush()

    with pytest.raises(service.StrategistError, match="other-skill"):
        await service._persist_tasks(db_session, workspace, _proposal(skill.slug))
    assert (await db_session.execute(select(Task).where(Task.workspace_id == workspace.id))).scalars().all() == []


@pytest.mark.asyncio
async def test_strategist_rejects_owner_binding_from_another_workspace(db_session) -> None:
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Release A", status="paused")
    foreign_workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Release B", status="paused")
    owner = Agent(id=generate_ulid(), entity_id=entity_id, name="Coordinator", status="active")
    subscription = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=owner.id, service_key="release_operations", status="active",
    )
    skill = Skill(
        id=generate_ulid(), entity_id=entity_id, workspace_id=foreign_workspace.id,
        name="Foreign intake", slug="foreign-intake", system_prompt="Intake", status="active",
    )
    db_session.add_all([
        workspace, foreign_workspace, owner, subscription, skill,
        AgentSkillBinding(agent_id=owner.id, skill_id=skill.id, status="active"),
    ])
    await db_session.flush()

    with pytest.raises(service.StrategistError, match="foreign-intake"):
        await service._persist_tasks(db_session, workspace, _proposal(skill.slug))
    assert await context._bound_skills_by_service(db_session, [subscription], entity_id, workspace.id) == {}
