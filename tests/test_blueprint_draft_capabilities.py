from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.worker import SubscriptionWorker
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_draft_service import apply_blueprint, finalize_draft
from packages.core.services.workspace_setup_service import DEFAULT_FIELDS


pytestmark = pytest.mark.asyncio


def _payload(*, agent_slug: str, skill_slug: str, server_slug: str) -> dict:
    return {
        "manifest": {
            "blueprint_version": "1.1",
            "title": "Blueprint Capability Copy",
            "kind": "operations",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": ["workspace_agent"],
                "mcp_servers": [{"slug": server_slug}],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {
            "skills": [{
                "slug": skill_slug,
                "name": "Blueprint Operations Skill",
                "description": "Perform the copied operating procedure.",
                "system_prompt": "Follow the copied operating procedure exactly.",
                "tools": ["workspace_agent"],
                "version": "1.0.0",
            }],
            "agents": [{
                "slug": agent_slug,
                "name": "Blueprint Operations Agent",
                "description": "Runs the copied workspace capability.",
                "system_prompt": "Run the copied workspace operating capability.",
                "config": {},
                "business_capabilities": ["workspace.operate"],
                "tool_bindings": ["workspace_agent"],
                "skill_bindings": [skill_slug],
                "mcp_bindings": [{
                    "server_slug": server_slug,
                    "allowed_tools": None,
                    "config_override_allowlist": [],
                }],
                "starter_memory": [],
            }],
            "knowledge_packs": [],
        },
        "recipe": {
            "operating_model": {
                "context": "Operate the copied process.",
                "primary_work": "Complete the copied operating procedure.",
                "services": [{
                    "service_key": "blueprint_operations",
                    "name": "Blueprint Operations",
                    "description": "Run the copied operating procedure.",
                    "autonomy_level": "supervised",
                    "owner_role": "workspace_owner",
                }],
            },
            "strategist": {
                "cadence": {"schedule": "daily", "trigger_conditions": ["blocked"]},
                "voice": "concise",
            },
            "prompts": [],
            "subscriptions": [{
                "service_key": "blueprint_operations",
                "agent_slug": agent_slug,
                "config": {},
            }],
            "scheduled_jobs": [],
            "workflows": [],
            "goals": [{
                "title": "Complete copied work",
                "metric_key": "completed_items",
                "target_value": 10,
                "measurement_cadence": "daily",
                "priority": 3,
            }],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }


async def test_apply_blueprint_materializes_agent_capabilities(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    entity_id = generate_ulid()
    agent_slug = f"blueprint-agent-{entity_id}"
    skill_slug = f"blueprint-skill-{entity_id}"
    server_slug = f"blueprint-mcp-{entity_id}"
    payload = _payload(
        agent_slug=agent_slug,
        skill_slug=skill_slug,
        server_slug=server_slug,
    )
    server = MCPServer(
        id=generate_ulid(),
        server_key=server_slug,
        name="Blueprint MCP",
        transport="builtin",
        auth_type="none",
        status="active",
    )
    blueprint = WorkspaceBlueprint(
        id=generate_ulid(),
        entity_id=entity_id,
        slug=f"capability-copy-{entity_id}",
        title="Blueprint Capability Copy",
        payload=payload,
        payload_version="1.1",
        status="published",
    )
    draft = WorkspaceDraft(
        id=generate_ulid(),
        entity_id=entity_id,
        fields=dict(DEFAULT_FIELDS),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([server, blueprint, draft])
    await db_session.commit()

    applied = await apply_blueprint(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
        blueprint_id=blueprint.id,
    )
    mapping = applied.fields["agent_mappings"][0]
    custom = mapping["create_agent_draft"]
    assert mapping["strategy"] == "create_custom"
    assert custom["tool_bindings"] == ["workspace_agent"]
    assert custom["business_capabilities"] == ["workspace.operate"]
    assert custom["skill_bindings"] == [skill_slug]
    assert custom["mcp_bindings"] == [server_slug]
    assert custom["missing_skill_specs"][0]["slug"] == skill_slug
    assert applied.fields["goals"][0]["target"] == 10
    assert applied.fields["goals"][0]["cadence"] == "daily"
    assert applied.fields["_blueprint_operating_model"]["strategist"]["cadence"] == "daily"
    assert applied.ready is True

    async def _do_not_reuse_skill(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.services.agent_provisioning_service."
        "_select_existing_skill_for_missing_spec",
        _do_not_reuse_skill,
    )
    workspace_id, finalized = await finalize_draft(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
    )
    await db_session.commit()
    assert finalized.finalized_workspace_id == workspace_id

    subscription = (await db_session.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.service_key == "blueprint_operations",
        )
    )).scalar_one()
    agent = await db_session.get(Agent, subscription.agent_id)
    assert agent is not None
    assert agent.slug == agent_slug
    assert agent.config["business_capabilities"] == ["workspace.operate"]
    assert len((await db_session.execute(
        select(AgentToolBinding).where(AgentToolBinding.agent_id == agent.id)
    )).scalars().all()) >= 1
    assert len((await db_session.execute(
        select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent.id)
    )).scalars().all()) == 1
    assert len((await db_session.execute(
        select(AgentMCPBinding).where(AgentMCPBinding.agent_id == agent.id)
    )).scalars().all()) == 1
    assert (await db_session.execute(
        select(SubscriptionWorker).where(
            SubscriptionWorker.subscription_id == subscription.id,
        )
    )).scalar_one_or_none() is not None

    skill = (await db_session.execute(
        select(Skill).join(
            AgentSkillBinding,
            AgentSkillBinding.skill_id == Skill.id,
        ).where(AgentSkillBinding.agent_id == agent.id)
    )).scalar_one()
    assert skill.slug == skill_slug.lower()
