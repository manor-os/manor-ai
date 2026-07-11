import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.services import agent_provisioning_service
from packages.core.services.agent_provisioning_service import (
    CustomAgentSpec,
    provision_custom_agent,
)
from packages.core.services.skill_generator import generate_skill
from packages.core.services.skill_service import create_skill


@pytest.mark.asyncio
async def test_create_skill_replaces_placeholder_identity(db_session):
    skill = await create_skill(
        db_session,
        entity_id="ent_skill_identity",
        name="unknown",
        system_prompt="# Support Ticket Triage\n\nUse this skill when support tickets need routing.",
        slug="unknown",
        description="Support Ticket Triage",
    )

    assert skill.name == "Support Ticket Triage"
    assert skill.slug == "support_ticket_triage"

    duplicate = await create_skill(
        db_session,
        entity_id="ent_skill_identity",
        name="Support Ticket Triage",
        system_prompt="Handle support tickets.",
        description="Support Ticket Triage",
    )

    assert duplicate.slug == "support_ticket_triage_2"


@pytest.mark.asyncio
async def test_workspace_missing_skill_specs_reuse_llm_selected_existing_skill(
    db_session,
    monkeypatch,
):
    existing = await create_skill(
        db_session,
        entity_id="ent_skill_reuse",
        name="Support Ticket Triage",
        system_prompt="Route support tickets by urgency and owner.",
        slug="support_ticket_triage",
        description="Use this skill when support tickets need triage, routing, and priority labeling.",
        tools=["workspace_search"],
    )

    async def fake_selector(**kwargs):
        requested = kwargs["requested_skill"]
        candidates = kwargs["candidates"]
        assert requested["name"] == "Ticket Routing"
        assert any(candidate["id"] == existing.id for candidate in candidates)
        return json.dumps(
            {
                "reuse": True,
                "skill_id": existing.id,
                "confidence": 0.93,
                "reason": "Existing skill covers support ticket triage.",
            }
        )

    monkeypatch.setattr(
        agent_provisioning_service,
        "_execute_skill_reuse_selector_completion",
        fake_selector,
    )

    result = await provision_custom_agent(
        db_session,
        entity_id="ent_skill_reuse",
        spec=CustomAgentSpec(
            agent_name="Support Agent",
            system_prompt="You are Support Agent. Handle support workflows and stay in scope.",
            source="auto_workspace_setup",
            workspace_id="ws_support",
            workspace_name="Support Ops",
            service_key="support_triage",
            missing_skill_specs=[
                {
                    "name": "Ticket Routing",
                    "system_prompt": "Route support tickets by priority and owner.",
                    "description": "Ticket triage and routing.",
                    "tools": ["workspace_search"],
                }
            ],
        ),
    )

    assert result.created_skills == []
    assert result.bound_skills == ["support_ticket_triage"]

    skills = (await db_session.execute(select(Skill).where(Skill.entity_id == "ent_skill_reuse"))).scalars().all()
    assert [skill.id for skill in skills] == [existing.id]

    binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == result.agent_id,
                AgentSkillBinding.skill_id == existing.id,
            )
        )
    ).scalar_one()
    assert binding.status == "active"
    [context] = binding.config["contexts"]
    assert context["source"] == "auto_workspace_setup"
    assert context["agent_id"] == result.agent_id
    assert context["agent_name"] == "Support Agent"
    assert context["workspace_id"] == "ws_support"
    assert context["workspace_name"] == "Support Ops"
    assert context["service_key"] == "support_triage"
    assert context["requested_skill"]["name"] == "Ticket Routing"
    assert context["match"]["type"] == "llm_reuse"
    assert context["match"]["confidence"] == 0.93

    from apps.api.routers.skills import _binding_contexts_for_skills

    contexts = await _binding_contexts_for_skills(
        db_session,
        [existing],
        entity_id="ent_skill_reuse",
    )
    [api_context] = contexts[existing.id]
    assert api_context["binding_id"] == binding.id
    assert api_context["agent_name"] == "Support Agent"
    assert api_context["workspace_name"] == "Support Ops"
    assert api_context["match"]["type"] == "llm_reuse"


@pytest.mark.asyncio
async def test_workspace_missing_skill_specs_preserve_requested_slug(db_session, monkeypatch):
    async def no_existing_skill(**kwargs):
        return json.dumps({"reuse": False, "confidence": 0, "reason": "No matching skill."})

    monkeypatch.setattr(
        agent_provisioning_service,
        "_execute_skill_reuse_selector_completion",
        no_existing_skill,
    )

    result = await provision_custom_agent(
        db_session,
        entity_id="ent_skill_slug_preserve",
        spec=CustomAgentSpec(
            agent_name="Consulting Discovery Agent",
            system_prompt="You qualify consulting opportunities.",
            source="auto_workspace_setup",
            workspace_id="ws_consulting",
            workspace_name="Consulting",
            service_key="client_discovery",
            skill_bindings=["consulting-discovery-brief"],
            missing_skill_specs=[
                {
                    "name": "Consulting Discovery Brief",
                    "slug": "consulting-discovery-brief",
                    "system_prompt": "Prepare concise discovery briefs.",
                    "description": "Discovery workflow.",
                    "tools": ["rag", "workspace_agent", "generate_file"],
                }
            ],
        ),
    )

    skill = (
        await db_session.execute(
            select(Skill).where(
                Skill.entity_id == "ent_skill_slug_preserve",
                Skill.slug == "consulting-discovery-brief",
            )
        )
    ).scalar_one()
    binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == result.agent_id,
                AgentSkillBinding.skill_id == skill.id,
            )
        )
    ).scalar_one()

    assert result.created_skills == ["consulting-discovery-brief"]
    assert skill.slug == "consulting-discovery-brief"
    assert binding.status == "active"


@pytest.mark.asyncio
async def test_generated_scheduled_job_skill_records_source_metadata(
    db_session,
    monkeypatch,
):
    async def fake_generation_completion(*args, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "name": "daily-support-summary",
                    "slug": "daily_support_summary",
                    "display_name": "Daily Support Summary",
                    "description": "Use this skill when a scheduled support summary should be produced.",
                    "system_prompt": "# Daily Support Summary\n\nSummarize support work.",
                    "tools": ["workspace_search"],
                    "input_schema": {},
                    "output_format": "markdown",
                    "category": "automation",
                    "tags": ["support"],
                    "complexity": "worker",
                }
            ),
            usage={},
        )

    async def fake_review_completion(*args, **kwargs):
        return SimpleNamespace(content="PASS", usage={})

    monkeypatch.setattr(
        "packages.core.services.skill_generator.runtime_execute_skill_generation_completion",
        fake_generation_completion,
    )
    monkeypatch.setattr(
        "packages.core.services.skill_generator.runtime_execute_skill_review_completion",
        fake_review_completion,
    )

    skill = await generate_skill(
        prompt="Scheduled automation: Daily support summary",
        entity_id="ent_scheduled_skill",
        db=db_session,
        category="automation",
        tags=["auto-generated", "scheduled-job", "job_support_daily"],
        config_overrides={
            "source": "scheduled_job",
            "generation_source": "llm-generated",
            "scheduled_job_id": "job_support_daily",
            "workspace_id": "ws_support",
            "agent_id": "agent_support",
        },
    )

    assert skill.config["source"] == "scheduled_job"
    assert skill.config["generation_source"] == "llm-generated"
    assert skill.config["scheduled_job_id"] == "job_support_daily"
    assert skill.config["workspace_id"] == "ws_support"
    assert skill.config["agent_id"] == "agent_support"
