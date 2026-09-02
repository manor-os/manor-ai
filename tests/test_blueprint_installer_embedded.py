"""Unit tests for the installer's embedded.skills/agents/knowledge_packs path.

Uses postgres via the ``db_session`` fixture from conftest — see the
top-of-file comment in test_blueprint_exporter_embedded.py for why we
moved off in-memory sqlite (shared MetaData mutation broke downstream
tests in the same pytest process).

Exercises:
  * embedded.skills create new Skill rows (entity-private)
  * embedded.agents create Agent + tool/MCP/skill bindings
  * each Workspace install receives local component ids linked to source ids
  * missing ToolDefinition raises InstallError (fast-fail)
  * missing MCPServer becomes an InstallTodo (not an error)
  * governance preset never_allow blocks bound tools at install time
  * knowledge_pack creates a DocumentGroup; inline_text mode materializes
    searchable starter documents
  * starter_memory rows remain scoped to the installed Workspace
"""

from __future__ import annotations

from decimal import Decimal
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import (
    InstallError,
    InstallMode,
    install_blueprint,
    sync_workspace_live_setup_requirements,
)
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.goal import Goal
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.memory import AgentMemory
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)
from packages.core.models.worker import SubscriptionWorker
from packages.core.services.workspace_readiness import (
    evaluate_workspace_blocking_setup,
)


@pytest.fixture
def entity_id() -> str:
    """Per-test unique entity_id (ULID = 26 chars, matches column width)."""
    return generate_ulid()


def _base_payload(**overrides) -> dict:
    """Minimal v1.1 payload. Caller can override at any nested path with
    dotted keys, e.g. ``_base_payload(**{"embedded.agents": [...]})``."""
    payload = {
        "manifest": {
            "blueprint_version": "1.1",
            "title": "Test",
            "kind": "social_media",
            "description": "T",
        },
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
        },
        "embedded": {
            "skills": [],
            "agents": [],
            "knowledge_packs": [],
        },
        "recipe": {
            "operating_model": {},
            "strategist": None,
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [],
            "goals": [],
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
    for k, v in overrides.items():
        sections = k.split(".")
        cursor = payload
        for s in sections[:-1]:
            cursor = cursor.setdefault(s, {})
        cursor[sections[-1]] = v
    return payload


async def test_simulate_install_materializes_blueprint_experience_into_workspace_settings(
    db_session: AsyncSession,
    entity_id: str,
):
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_base_payload(),
        mode=InstallMode.SIMULATE,
    )
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == result.workspace_id))).scalar_one()
    experience = workspace.settings["simulation_experience"]
    assert workspace.settings["sandbox"] is True
    assert experience["schema_version"] == "1.0"
    assert experience["artifacts"]


async def test_live_install_does_not_persist_simulation_only_settings(
    db_session: AsyncSession,
    entity_id: str,
):
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_base_payload(),
        mode=InstallMode.LIVE,
    )
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == result.workspace_id))).scalar_one()
    assert "sandbox" not in workspace.settings
    assert "simulation_experience" not in workspace.settings


async def test_install_does_not_infer_undeclared_business_ledgers(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "manifest.title": "Recruiting and HR",
            "recipe.operating_model.context": "Manage candidates and employee onboarding.",
            "recipe.operating_model.primary_work": "Run the hiring pipeline.",
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )
    workspace = (await db_session.execute(
        select(Workspace).where(Workspace.id == result.workspace_id)
    )).scalar_one()

    assert "ledger_contracts" not in workspace.settings
    assert "ledger_matching" not in workspace.settings


async def test_simulate_install_keeps_goal_measurement_source_portable(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.goals": [{
                "goal_key": "portable_measurement",
                "title": "Portable measurement",
                "metric_key": "completed_tasks",
                "target_value": 10,
                "measurement_source": {
                    "provider": "workspace_internal",
                    "params": {"mode": "linked_task_impact"},
                },
                "measurement_cadence": "daily",
            }],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    goal = (await db_session.execute(select(Goal).where(
        Goal.workspace_id == result.workspace_id,
    ))).scalar_one()
    exported = await export_workspace(
        db_session,
        result.workspace_id,
        title="Portable simulation Goal",
    )

    assert goal.measurement_source == payload["recipe"]["goals"][0]["measurement_source"]
    assert exported["recipe"]["goals"][0]["measurement_source"] == goal.measurement_source
    assert "_simulate" not in str(exported["recipe"]["goals"])


async def test_install_uses_recipe_goals_as_the_single_identity_source(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.goals": [
                {
                    "goal_key": "paid_signups",
                    "title": "Paid signups",
                    "metric_key": "signup_count",
                    "target_value": 25,
                }
            ],
            "recipe.operating_model.goals": [
                {
                    "goal_key": "phantom_signups",
                    "title": "Phantom signups",
                    "metric_key": "signup_count",
                    "target_value": 999,
                }
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == result.workspace_id))).scalar_one()
    goals = list(
        (await db_session.execute(select(Goal).where(Goal.workspace_id == result.workspace_id))).scalars().all()
    )
    assert [goal.goal_key for goal in goals] == ["paid_signups"]
    assert [goal["goal_key"] for goal in workspace.operating_model["goals"]] == ["paid_signups"]


async def test_install_respects_explicit_no_goal_recipe_over_legacy_contract(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.goals": [],
            "recipe.operating_model.goals": [
                {
                    "goal_key": "legacy_signups",
                    "title": "Legacy signups",
                    "metric_key": "signup_count",
                    "target_value": 25,
                }
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == result.workspace_id))).scalar_one()
    goals = list(
        (await db_session.execute(select(Goal).where(Goal.workspace_id == result.workspace_id))).scalars().all()
    )
    assert goals == []
    assert "goals" not in workspace.operating_model


async def test_goal_numbers_round_trip_without_float_precision_loss(
    db_session: AsyncSession,
    entity_id: str,
):
    from packages.core.services.workspace_operation_service import (
        repair_workspace_operation_runtime,
    )

    payload = _base_payload(
        **{
            "recipe.goals": [
                {
                    "goal_key": "large_exact_target",
                    "title": "Large exact target",
                    "metric_key": "revenue_units",
                    "target_value": "9999999999999999.9999",
                    "baseline_value": "1234567890123456.7890",
                }
            ]
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )
    goal = (
        await db_session.execute(
            select(Goal).where(Goal.workspace_id == result.workspace_id)
        )
    ).scalar_one()
    assert goal.target_value == Decimal("9999999999999999.9999")
    assert goal.baseline_value == Decimal("1234567890123456.7890")

    workspace = (
        await db_session.execute(
            select(Workspace).where(Workspace.id == result.workspace_id)
        )
    ).scalar_one()
    contract_goal = workspace.operating_model["goals"][0]
    assert contract_goal["target_value"] == "9999999999999999.9999"
    assert contract_goal["baseline_value"] == "1234567890123456.7890"

    repaired = await repair_workspace_operation_runtime(
        db_session,
        result.workspace_id,
        entity_id,
    )
    assert repaired is not None
    await db_session.refresh(goal)
    assert goal.target_value == Decimal("9999999999999999.9999")
    assert goal.baseline_value == Decimal("1234567890123456.7890")

    exported = await export_workspace(db_session, result.workspace_id, title="Exact")
    exported_goal = exported["recipe"]["goals"][0]
    assert exported_goal["target_value"] == "9999999999999999.9999"
    assert exported_goal["baseline_value"] == "1234567890123456.7890"
    json.dumps(exported)


# ── Skills ────────────────────────────────────────────────────────────


async def test_embedded_skill_creates_row(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"reply-tone-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.skills": [
                {
                    "slug": slug,
                    "name": "Reply Tone",
                    "system_prompt": "Reply nicely.",
                    "tools": [],
                    "is_public": False,
                    "version": "1.0.0",
                }
            ],
        }
    )
    await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()

    row = (await db_session.execute(select(Skill).where(Skill.entity_id == entity_id, Skill.slug == slug))).scalar_one()
    assert row.name == "Reply Tone"
    assert row.is_public is False  # embedded must be entity-private


async def test_embedded_skill_mints_one_local_id_per_workspace_install(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"reply-tone-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.skills": [{"slug": slug, "name": "T", "system_prompt": "x", "tools": []}],
        }
    )
    first = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    second = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    rows = list(
        (await db_session.execute(select(Skill).where(Skill.entity_id == entity_id, Skill.slug == slug)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {row.workspace_id for row in rows} == {
        first.workspace_id,
        second.workspace_id,
    }
    assert len({row.id for row in rows}) == 2


async def test_blueprint_component_links_are_scoped_by_exact_source_and_workspace(
    db_session: AsyncSession,
    entity_id: str,
):
    source_blueprint_id = f"marketplace:{generate_ulid()}"
    slug = f"linked-skill-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.skills": [
                {
                    "slug": slug,
                    "name": "Linked Skill",
                    "system_prompt": "Use the exact source mapping.",
                    "tools": [],
                }
            ],
        }
    )
    installs = [
        await install_blueprint(
            db_session,
            entity_id=entity_id,
            payload=payload,
            blueprint_id=source_blueprint_id,
            blueprint_slug="same-display-slug",
        )
        for _ in range(2)
    ]

    links = list(
        (
            await db_session.execute(
                select(MarketplaceResourceLink).where(
                    MarketplaceResourceLink.entity_id == entity_id,
                    MarketplaceResourceLink.marketplace_resource_id == source_blueprint_id,
                )
            )
        )
        .scalars()
        .all()
    )
    root_links = [link for link in links if link.local_resource_type == "workspace"]
    skill_links = [link for link in links if link.local_resource_type == "skill"]
    assert {link.scope_id for link in root_links} == {install.workspace_id for install in installs}
    assert {link.scope_id for link in skill_links} == {install.workspace_id for install in installs}
    assert len({link.local_resource_id for link in skill_links}) == 2


# ── Agents ────────────────────────────────────────────────────────────


async def test_embedded_agent_creates_with_tool_bindings(
    db_session: AsyncSession,
    entity_id: str,
):
    tool_name = f"tool.x.post.{entity_id}"
    agent_slug = f"calvin-reply-{entity_id}"
    td = ToolDefinition(
        id=generate_ulid(),
        name=tool_name,
        display_name="X Post",
    )
    db_session.add(td)
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Calvin Reply",
                    "system_prompt": "Reply like Calvin.",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    await db_session.commit()

    agent = (
        await db_session.execute(select(Agent).where(Agent.entity_id == entity_id, Agent.slug == agent_slug))
    ).scalar_one()
    bindings = list(
        (await db_session.execute(select(AgentToolBinding).where(AgentToolBinding.agent_id == agent.id)))
        .scalars()
        .all()
    )
    assert agent.is_public is False
    assert agent.source == "blueprint"
    assert len(bindings) == 1
    assert bindings[0].tool_id == td.id


async def test_existing_agent_reconciles_all_bindings_and_subscription_worker(
    db_session: AsyncSession,
    entity_id: str,
):
    tool_name = f"tool.workspace.copy.{entity_id}"
    skill_slug = f"workspace-copy-skill-{entity_id}"
    agent_slug = f"workspace-copy-agent-{entity_id}"
    server_slug = f"workspace-copy-mcp-{entity_id}"
    tool = ToolDefinition(
        id=generate_ulid(),
        name=tool_name,
        display_name="Workspace Copy",
    )
    skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Existing Skill",
        slug=skill_slug,
        system_prompt="Existing prompt.",
        tools=[],
        is_public=False,
        status="active",
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Existing Agent",
        slug=agent_slug,
        system_prompt="Existing prompt.",
        config={},
        is_public=False,
        status="active",
    )
    server = MCPServer(
        id=generate_ulid(),
        server_key=server_slug,
        name="Workspace Copy MCP",
        transport="builtin",
        auth_type="none",
        status="active",
    )
    db_session.add_all([tool, skill, agent, server])
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [{"slug": server_slug}],
                "skills": [],
                "agents": [],
            },
            "embedded.skills": [
                {
                    "slug": skill_slug,
                    "name": "Existing Skill",
                    "system_prompt": "Blueprint prompt.",
                    "tools": [tool_name],
                }
            ],
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Existing Agent",
                    "system_prompt": "Blueprint prompt.",
                    "config": {},
                    "business_capabilities": ["workspace.search"],
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [
                        {
                            "server_slug": server_slug,
                            "allowed_tools": None,
                            "config_override_allowlist": [],
                        }
                    ],
                    "skill_bindings": [skill_slug],
                    "starter_memory": [],
                }
            ],
            "recipe.subscriptions": [
                {
                    "service_key": "workspace_copy",
                    "agent_slug": agent_slug,
                    "config": {},
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )
    await db_session.commit()

    installed_agent = (
        await db_session.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.workspace_id == result.workspace_id,
                Agent.slug == agent_slug,
            )
        )
    ).scalar_one()
    installed_skill = (
        await db_session.execute(
            select(Skill).where(
                Skill.entity_id == entity_id,
                Skill.workspace_id == result.workspace_id,
                Skill.slug == skill_slug,
            )
        )
    ).scalar_one()
    assert installed_agent.id != agent.id
    assert installed_skill.id != skill.id
    assert installed_agent.config["business_capabilities"] == ["workspace.search"]
    assert installed_skill.tools == [tool_name]
    assert agent.config == {}
    assert skill.tools == []
    assert (
        len(
            (await db_session.execute(select(AgentToolBinding).where(AgentToolBinding.agent_id == installed_agent.id)))
            .scalars()
            .all()
        )
        == 1
    )
    assert (
        len(
            (
                await db_session.execute(
                    select(AgentSkillBinding).where(AgentSkillBinding.agent_id == installed_agent.id)
                )
            )
            .scalars()
            .all()
        )
        == 1
    )
    assert (
        len(
            (await db_session.execute(select(AgentMCPBinding).where(AgentMCPBinding.agent_id == installed_agent.id)))
            .scalars()
            .all()
        )
        == 1
    )

    [subscription] = list(
        (
            await db_session.execute(
                select(AgentSubscription).where(
                    AgentSubscription.workspace_id == result.workspace_id,
                    AgentSubscription.agent_id == installed_agent.id,
                )
            )
        )
        .scalars()
        .all()
    )
    worker_binding = (
        await db_session.execute(
            select(SubscriptionWorker).where(
                SubscriptionWorker.subscription_id == subscription.id,
            )
        )
    ).scalar_one_or_none()
    assert worker_binding is not None


async def test_embedded_agent_missing_tool_raises_install_error(
    db_session: AsyncSession,
    entity_id: str,
):
    tool_name = f"tool.x.unknown.{entity_id}"
    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": f"calvin-reply-{entity_id}",
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    with pytest.raises(InstallError, match="not in this deployment"):
        await install_blueprint(db_session, entity_id=entity_id, payload=payload)


async def test_embedded_agent_missing_mcp_becomes_todo(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": f"calvin-reply-{entity_id}",
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [
                        {
                            "server_slug": f"linear-mcp-{entity_id}",
                            "allowed_tools": ["linear.create_issue"],
                            "config_override_allowlist": ["team_id"],
                        }
                    ],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()
    mcp_todos = [t for t in result.todos if t.kind == "mcp_server"]
    assert len(mcp_todos) == 1
    assert f"linear-mcp-{entity_id}" in mcp_todos[0].detail
    assert mcp_todos[0].blocking is True


async def test_optional_missing_mcp_does_not_block_workspace(
    db_session: AsyncSession,
    entity_id: str,
):
    server_slug = f"manor_mcp_optional_{entity_id.lower()}"
    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [{"slug": server_slug, "required": False}],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [{
                "slug": f"optional-agent-{entity_id}",
                "name": "Optional MCP Agent",
                "system_prompt": "Use the optional MCP when available.",
                "config": {},
                "tool_bindings": [],
                "mcp_bindings": [{
                    "server_slug": server_slug,
                    "allowed_tools": ["optional.read"],
                    "config_override_allowlist": [],
                }],
                "skill_bindings": [],
                "starter_memory": [],
            }],
        }
    )

    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    assert [todo for todo in result.todos if todo.kind == "mcp_server"]
    assert all(todo.blocking is False for todo in result.todos)
    assert workspace.settings["_blueprint"]["blocking_todo_count"] == 0
    assert await evaluate_workspace_blocking_setup(db_session, workspace) is None


async def test_mcp_contract_config_fields_remain_a_live_blocker(
    db_session: AsyncSession,
    entity_id: str,
):
    server_slug = f"manor_mcp_configured_{entity_id.lower()}"
    db_session.add(MCPServer(
        id=generate_ulid(),
        server_key=server_slug,
        name="Configured MCP",
        transport="builtin",
        auth_type="none",
        status="active",
    ))
    await db_session.commit()
    agent_slug = f"configured-agent-{entity_id}"
    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [{
                    "slug": server_slug,
                    "required": True,
                    "config_fields_to_set": ["team_id"],
                }],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [{
                "slug": agent_slug,
                "name": "Configured MCP Agent",
                "system_prompt": "Use the configured MCP.",
                "config": {},
                "tool_bindings": [],
                "mcp_bindings": [{
                    "server_slug": server_slug,
                    "allowed_tools": ["projects.read"],
                    "config_override_allowlist": ["team_id"],
                }],
                "skill_bindings": [],
                "starter_memory": [],
            }],
        }
    )

    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    [todo] = [
        item for item in result.todos if item.kind == "mcp_configuration"
    ]
    assert todo.payload["required_config_fields"] == ["team_id"]
    assert todo.blocking is True
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    blocked = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert blocked is not None and blocked.blocks_work

    binding = (await db_session.execute(
        select(AgentMCPBinding)
        .join(Agent, Agent.id == AgentMCPBinding.agent_id)
        .where(
            Agent.workspace_id == result.workspace_id,
            Agent.slug == agent_slug,
        )
    )).scalar_one()
    binding.config_override = {"team_id": "team-1"}
    await db_session.flush()

    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None
    assert ready.blocks_work is False

    binding.config_override = {}
    await db_session.flush()
    regressed = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert regressed is not None and regressed.blocks_work


async def test_mcp_empty_allowlist_stays_deny_all_and_is_checked_live(
    db_session: AsyncSession,
    entity_id: str,
):
    server_slug = f"manor_mcp_deny_all_{entity_id.lower()}"
    db_session.add(MCPServer(
        id=generate_ulid(),
        server_key=server_slug,
        name="Deny-all MCP",
        transport="builtin",
        auth_type="none",
        status="active",
        default_allowed_tools=["dangerous.write"],
    ))
    await db_session.commit()
    agent_slug = f"deny-all-agent-{entity_id}"
    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [{"slug": server_slug, "required": True}],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [{
                "slug": agent_slug,
                "name": "Deny-all MCP Agent",
                "system_prompt": "The MCP binding is intentionally disabled.",
                "config": {},
                "tool_bindings": [],
                "mcp_bindings": [{
                    "server_slug": server_slug,
                    "allowed_tools": [],
                    "config_override_allowlist": [],
                }],
                "skill_bindings": [],
                "starter_memory": [],
            }],
        }
    )

    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    binding = (await db_session.execute(
        select(AgentMCPBinding)
        .join(Agent, Agent.id == AgentMCPBinding.agent_id)
        .where(Agent.workspace_id == workspace.id, Agent.slug == agent_slug)
    )).scalar_one()
    assert binding.allowed_tools == []

    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None and ready.blocks_work is False

    binding.allowed_tools = None
    await db_session.flush()
    widened = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert widened is not None and widened.blocks_work


async def test_mcp_config_fields_remain_scoped_to_each_agent_binding(
    db_session: AsyncSession,
    entity_id: str,
):
    server_slug = f"manor_mcp_per_agent_{entity_id.lower()}"
    db_session.add(MCPServer(
        id=generate_ulid(),
        server_key=server_slug,
        name="Per-agent MCP",
        transport="builtin",
        auth_type="none",
        status="active",
    ))
    await db_session.commit()
    agent_specs = [
        (f"team-agent-{entity_id}", "team_id"),
        (f"project-agent-{entity_id}", "project_id"),
    ]
    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [],
                "mcp_servers": [{
                    "slug": server_slug,
                    "required": True,
                    "config_fields_to_set": ["team_id", "project_id"],
                }],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": agent_slug,
                    "system_prompt": "Use only this binding's declared configuration.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [{
                        "server_slug": server_slug,
                        "allowed_tools": ["projects.read"],
                        "config_override_allowlist": [field],
                    }],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
                for agent_slug, field in agent_specs
            ],
        }
    )

    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    config_todos = [
        todo for todo in result.todos if todo.kind == "mcp_configuration"
    ]
    assert {
        todo.payload["agent_slug"]: todo.payload["required_config_fields"]
        for todo in config_todos
    } == {
        agent_slug: [field]
        for agent_slug, field in agent_specs
    }


async def test_governance_blocks_embedded_agent_tool(
    db_session: AsyncSession,
    entity_id: str,
):
    tool_name = f"tool.x.delete_account.{entity_id}"
    db_session.add(
        ToolDefinition(
            id=generate_ulid(),
            name=tool_name,
            display_name="X Delete",
        )
    )
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": f"destroyer-{entity_id}",
                    "name": "D",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "policy.governance": {
                "never_allow_actions": ["x.delete_*"],  # matches!
                "max_risk_level": "medium",
            },
        }
    )
    with pytest.raises(InstallError, match="permanently blocked by governance"):
        await install_blueprint(
            db_session,
            entity_id=entity_id,
            payload=payload,
            governance_preset="standard",
        )


async def test_governance_safe_preset_passes_benign_tool(
    db_session: AsyncSession,
    entity_id: str,
):
    """Safe preset only adds wildcard HITL, doesn't expand never_allow —
    a benign tool binding survives. Sanity check on the preview path."""
    tool_name = f"tool.benign.read.{entity_id}"
    agent_slug = f"reader-{entity_id}"
    db_session.add(
        ToolDefinition(
            id=generate_ulid(),
            name=tool_name,
            display_name="Read",
        )
    )
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires": {
                "manor_min_version": None,
                "tools": [tool_name],
                "mcp_servers": [],
                "skills": [],
                "agents": [],
            },
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "R",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [tool_name],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        governance_preset="safe",
    )
    await db_session.commit()
    agent = (await db_session.execute(select(Agent).where(Agent.slug == agent_slug))).scalar_one_or_none()
    assert agent is not None
    assert result.governance_applied is True


# ── Skill binding resolution ──────────────────────────────────────────


async def test_agent_binds_to_just_installed_embedded_skill(
    db_session: AsyncSession,
    entity_id: str,
):
    skill_slug = f"reply-tone-{entity_id}"
    agent_slug = f"calvin-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.skills": [
                {
                    "slug": skill_slug,
                    "name": "RT",
                    "system_prompt": "x",
                    "tools": [],
                }
            ],
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [skill_slug],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    skill = (await db_session.execute(select(Skill).where(Skill.slug == skill_slug))).scalar_one()
    agent = (await db_session.execute(select(Agent).where(Agent.slug == agent_slug))).scalar_one()
    bindings = list(
        (await db_session.execute(select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent.id)))
        .scalars()
        .all()
    )
    assert len(bindings) == 1
    assert bindings[0].skill_id == skill.id
    workspace = await db_session.get(Workspace, result.workspace_id)
    skill_requirements = [
        item
        for item in workspace.settings["_blueprint"]["live_setup_requirements"]
        if item["kind"] == "missing_skill"
    ]
    assert skill_requirements == [{
        "kind": "missing_skill",
        "detail": (
            f"Restore skill {skill_slug!r} for agent {agent_slug!r}."
        ),
        "payload": {
            "skill_slug": skill_slug,
            "marketplace_skill_id": None,
            "installed_skill_id": skill.id,
            "agent_slug": agent_slug,
            "installed_agent_id": agent.id,
        },
        "blocking": True,
    }]

    settings = dict(workspace.settings)
    blueprint = dict(settings["_blueprint"])
    blueprint.pop("live_setup_requirements")
    settings["_blueprint"] = blueprint
    workspace.settings = settings
    await db_session.flush()
    legacy = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert legacy is not None and legacy.blocks_work

    rebuilt = await sync_workspace_live_setup_requirements(
        db_session,
        workspace=workspace,
        payload=payload,
    )
    assert any(item["kind"] == "missing_skill" for item in rebuilt)
    resynced = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert resynced is not None and resynced.blocks_work is False


async def test_exact_marketplace_skill_binding_wins_over_embedded_slug_collision(
    db_session: AsyncSession,
    entity_id: str,
):
    shared_slug = f"shared-skill-{entity_id}"
    agent_slug = f"exact-skill-agent-{entity_id}"
    marketplace_skill = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Skill",
        slug=shared_slug,
        system_prompt="Use the Marketplace implementation.",
        tools=[],
        is_public=True,
        status="active",
    )
    db_session.add(marketplace_skill)
    await db_session.commit()

    payload = _base_payload(
        **{
            "contract.requires.skills": [
                {
                    "slug": shared_slug,
                    "marketplace_source": "platform",
                    "marketplace_id": marketplace_skill.id,
                }
            ],
            "embedded.skills": [
                {
                    "slug": shared_slug,
                    "name": "Embedded Collision",
                    "system_prompt": "Do not bind this implementation.",
                    "tools": [],
                }
            ],
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Exact Skill Agent",
                    "system_prompt": "Use the exact referenced Skill.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [shared_slug],
                    "skill_binding_refs": [
                        {
                            "slug": shared_slug,
                            "marketplace_source": "platform",
                            "marketplace_id": marketplace_skill.id,
                        }
                    ],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )
    await db_session.commit()

    agent = (
        await db_session.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.workspace_id == result.workspace_id,
                Agent.slug == agent_slug,
            )
        )
    ).scalar_one()
    [binding] = list(
        (await db_session.execute(select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent.id)))
        .scalars()
        .all()
    )
    bound_skill = (await db_session.execute(select(Skill).where(Skill.id == binding.skill_id))).scalar_one()

    assert bound_skill.workspace_id is None
    assert bound_skill.config["source_skill_id"] == marketplace_skill.id


async def test_manor_skill_identity_conflict_becomes_blocking_todo(
    db_session: AsyncSession,
    entity_id: str,
):
    marketplace_id = "stickman-video-creator"
    db_session.add_all(
        [
            Skill(
                id=generate_ulid(),
                entity_id=entity_id,
                workspace_id=generate_ulid(),
                name=f"Historical Manor Skill {index}",
                slug=f"historical-manor-skill-{index}-{entity_id}",
                system_prompt="Historical behavior.",
                tools=[],
                is_public=False,
                version="1.0.0",
                config={"marketplace_id": marketplace_id},
                status="active",
            )
            for index in range(2)
        ]
    )
    await db_session.flush()
    skill_slug = f"manor-conflict-{entity_id}"
    agent_slug = f"manor-conflict-agent-{entity_id}"
    payload = _base_payload(
        **{
            "contract.requires.skills": [
                {
                    "slug": skill_slug,
                    "marketplace_source": "manor",
                    "marketplace_id": marketplace_id,
                }
            ],
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Manor Conflict Agent",
                    "system_prompt": "Require the exact Manor Skill.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [skill_slug],
                    "skill_binding_refs": [
                        {
                            "slug": skill_slug,
                            "marketplace_source": "manor",
                            "marketplace_id": marketplace_id,
                        }
                    ],
                    "starter_memory": [],
                }
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )

    conflicts = [todo for todo in result.todos if todo.payload.get("identity_conflict")]
    assert len(conflicts) == 1
    assert conflicts[0].kind == "missing_skill"
    assert conflicts[0].blocking is True
    assert "multiple historical local imports" in conflicts[0].detail


async def test_legacy_marketplace_skill_slug_installs_local_copy(
    db_session: AsyncSession,
    entity_id: str,
):
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Legacy Marketplace Skill",
        slug=f"legacy-marketplace-skill-{entity_id}",
        system_prompt="Marketplace behavior.",
        tools=[],
        is_public=True,
        status="active",
    )
    db_session.add(source)
    await db_session.flush()
    agent_slug = f"legacy-marketplace-agent-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Legacy Marketplace Agent",
                    "system_prompt": "Use the legacy referenced Skill.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [source.slug],
                    "starter_memory": [],
                }
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    agent = (
        await db_session.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.workspace_id == result.workspace_id,
                Agent.slug == agent_slug,
            )
        )
    ).scalar_one()
    binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == agent.id,
            )
        )
    ).scalar_one()
    installed = await db_session.get(Skill, binding.skill_id)

    assert installed is not None
    assert installed.id != source.id
    assert installed.entity_id == entity_id
    assert installed.workspace_id is None
    assert installed.config["source_skill_id"] == source.id
    assert not any(todo.kind == "missing_skill" for todo in result.todos)


async def test_legacy_skill_slug_resolves_accessible_home_workspace_skill(
    db_session: AsyncSession,
    entity_id: str,
):
    user_id = generate_ulid()
    foreign_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user_id,
        workspace_id=generate_ulid(),
        visibility="private",
        name="Workspace-home Skill",
        slug=f"workspace-private-skill-{entity_id}",
        system_prompt="Reuse this Skill when the caller can read it.",
        tools=[],
        is_public=False,
        status="active",
    )
    db_session.add(foreign_skill)
    await db_session.flush()
    agent_slug = f"workspace-isolation-agent-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Workspace Isolation Agent",
                    "system_prompt": "Do not cross Workspace boundaries.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [foreign_skill.slug],
                    "starter_memory": [],
                }
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=user_id,
    )
    agent = (
        await db_session.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.workspace_id == result.workspace_id,
                Agent.slug == agent_slug,
            )
        )
    ).scalar_one()
    bindings = list(
        (
            await db_session.execute(
                select(AgentSkillBinding).where(
                    AgentSkillBinding.agent_id == agent.id,
                )
            )
        )
        .scalars()
        .all()
    )

    assert [binding.skill_id for binding in bindings] == [foreign_skill.id]
    assert not any(todo.kind == "missing_skill" for todo in result.todos)


async def test_legacy_skill_slug_rejects_inaccessible_home_workspace_skill(
    db_session: AsyncSession,
    entity_id: str,
):
    foreign_skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=generate_ulid(),
        workspace_id=generate_ulid(),
        visibility="private",
        name="Private Workspace-home Skill",
        slug=f"inaccessible-workspace-skill-{entity_id}",
        system_prompt="Do not reuse this Skill for another caller.",
        tools=[],
        is_public=False,
        status="active",
    )
    db_session.add(foreign_skill)
    await db_session.flush()
    agent_slug = f"workspace-isolation-agent-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Workspace Isolation Agent",
                    "system_prompt": "Do not cross Workspace boundaries.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [foreign_skill.slug],
                    "starter_memory": [],
                }
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=generate_ulid(),
    )
    agent = (
        await db_session.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.workspace_id == result.workspace_id,
                Agent.slug == agent_slug,
            )
        )
    ).scalar_one()
    bindings = list(
        (
            await db_session.execute(
                select(AgentSkillBinding).where(
                    AgentSkillBinding.agent_id == agent.id,
                )
            )
        )
        .scalars()
        .all()
    )

    assert bindings == []
    assert any(todo.kind == "missing_skill" for todo in result.todos)


async def test_legacy_skill_slug_backfills_exact_source_link(
    db_session: AsyncSession,
    entity_id: str,
):
    shared_slug = f"historical-marketplace-skill-{entity_id}"
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Skill",
        slug=shared_slug,
        system_prompt="Marketplace behavior.",
        tools=[],
        is_public=True,
        status="active",
    )
    historical = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=None,
        name="Historical Local Skill",
        slug=shared_slug,
        system_prompt="Historical installed behavior.",
        tools=[],
        is_public=False,
        config={"source_skill_id": source.id},
        status="active",
    )
    db_session.add_all([source, historical])
    await db_session.flush()
    agent_slug = f"historical-skill-agent-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Historical Skill Agent",
                    "system_prompt": "Use the historically installed Skill.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [shared_slug],
                    "starter_memory": [],
                }
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    agent = (
        await db_session.execute(
            select(Agent).where(
                Agent.entity_id == entity_id,
                Agent.workspace_id == result.workspace_id,
                Agent.slug == agent_slug,
            )
        )
    ).scalar_one()
    binding = (
        await db_session.execute(
            select(AgentSkillBinding).where(
                AgentSkillBinding.agent_id == agent.id,
            )
        )
    ).scalar_one()
    link = (
        await db_session.execute(
            select(MarketplaceResourceLink).where(
                MarketplaceResourceLink.entity_id == entity_id,
                MarketplaceResourceLink.marketplace_resource_type == "skill",
                MarketplaceResourceLink.marketplace_resource_id == source.id,
                MarketplaceResourceLink.local_resource_id == historical.id,
            )
        )
    ).scalar_one()

    assert binding.skill_id == historical.id
    assert link.relationship == "installed_from"


async def test_missing_skill_binding_becomes_todo(
    db_session: AsyncSession,
    entity_id: str,
):
    missing_slug = f"nonexistent-skill-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": f"calvin-{entity_id}",
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [missing_slug],
                    "starter_memory": [],
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    missing = [t for t in result.todos if t.kind == "missing_skill"]
    assert len(missing) == 1
    assert missing_slug in missing[0].detail
    assert missing[0].payload["installed_agent_id"]


async def test_missing_marketplace_skill_requires_exact_identity_not_slug(
    db_session: AsyncSession,
    entity_id: str,
):
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    marketplace_skill_id = generate_ulid()
    skill_slug = f"same-label-{entity_id}"
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Exact Skill Gate",
        status="active",
        settings={
            "_blueprint": {
                "blueprint_id": "builtin:exact-skill",
                "live_setup_requirements": [{
                    "kind": "missing_skill",
                    "detail": "Install the reviewed Marketplace Skill.",
                    "payload": {
                        "skill_slug": skill_slug,
                        "marketplace_skill_id": marketplace_skill_id,
                        "agent_slug": "exact-agent",
                        "installed_agent_id": agent_id,
                    },
                    "blocking": True,
                }],
            },
        },
    )
    agent = Agent(
        id=agent_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        name="Exact Agent",
        slug="exact-agent",
        status="active",
    )
    unrelated = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Unrelated same-slug Skill",
        slug=skill_slug,
        system_prompt="Wrong implementation.",
        is_public=False,
        status="active",
    )
    db_session.add_all([workspace, agent, unrelated])
    await db_session.flush()
    db_session.add(AgentSkillBinding(
        id=generate_ulid(),
        agent_id=agent.id,
        skill_id=unrelated.id,
        status="active",
    ))
    await db_session.flush()

    blocked = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert blocked is not None and blocked.blocks_work

    installed = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Reviewed Marketplace Skill",
        slug=skill_slug,
        system_prompt="Reviewed implementation.",
        is_public=False,
        config={"source_skill_id": marketplace_skill_id},
        status="active",
    )
    db_session.add(installed)
    await db_session.flush()
    db_session.add(AgentSkillBinding(
        id=generate_ulid(),
        agent_id=agent.id,
        skill_id=installed.id,
        status="active",
    ))
    await db_session.flush()

    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None and ready.blocks_work is False


async def test_exact_marketplace_skill_ref_ignores_same_slug_local_skill(
    db_session: AsyncSession,
    entity_id: str,
):
    shared_slug = f"shared-marketplace-label-{entity_id}"
    unrelated = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Unrelated Local Skill",
        slug=shared_slug,
        system_prompt="Local behavior that must not be claimed.",
        is_public=False,
        status="active",
    )
    source = Skill(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Skill",
        slug=shared_slug,
        system_prompt="Marketplace behavior.",
        is_public=True,
        version="3.0.0",
        status="active",
    )
    db_session.add_all([unrelated, source])
    await db_session.flush()
    agent_slug = f"exact-skill-agent-{entity_id}"
    payload = _base_payload(
        **{
            "contract.requires.skills": [
                {
                    "slug": shared_slug,
                    "marketplace_id": source.id,
                    "marketplace_source": "platform",
                    "min_version": source.version,
                }
            ],
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Exact Skill Agent",
                    "system_prompt": "Use the exact bound Skill.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [shared_slug],
                    "skill_binding_refs": [
                        {
                            "slug": shared_slug,
                            "marketplace_id": source.id,
                            "marketplace_source": "platform",
                        }
                    ],
                    "starter_memory": [],
                }
            ],
        }
    )

    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    agent = (
        await db_session.execute(select(Agent).where(Agent.entity_id == entity_id, Agent.slug == agent_slug))
    ).scalar_one()
    binding = (
        await db_session.execute(select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent.id))
    ).scalar_one()
    assert binding.skill_id not in {unrelated.id, source.id}
    installed = await db_session.get(Skill, binding.skill_id)
    assert installed is not None
    assert installed.config["source_skill_id"] == source.id
    link = (
        await db_session.execute(
            select(MarketplaceResourceLink).where(
                MarketplaceResourceLink.entity_id == entity_id,
                MarketplaceResourceLink.marketplace_source == "platform",
                MarketplaceResourceLink.marketplace_resource_id == source.id,
                MarketplaceResourceLink.local_resource_id == installed.id,
            )
        )
    ).scalar_one()
    assert link.relationship == "installed_from"


async def test_exact_marketplace_agent_subscription_wins_over_embedded_slug(
    db_session: AsyncSession,
    entity_id: str,
):
    shared_slug = f"shared-agent-{entity_id}"
    marketplace_agent = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Agent",
        slug=shared_slug,
        system_prompt="Use this Marketplace implementation.",
        config={},
        is_template=True,
        is_public=True,
        status="active",
    )
    db_session.add(marketplace_agent)
    await db_session.commit()

    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": shared_slug,
                    "name": "Embedded Collision",
                    "system_prompt": "Do not subscribe this Agent.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "recipe.subscriptions": [
                {
                    "service_key": "research",
                    "agent_slug": shared_slug,
                    "marketplace_agent_id": marketplace_agent.id,
                    "config": {},
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )
    await db_session.commit()

    subscription = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == result.workspace_id,
                AgentSubscription.service_key == "research",
            )
        )
    ).scalar_one()
    subscribed_agent = await db_session.get(Agent, subscription.agent_id)
    assert subscribed_agent is not None
    assert subscribed_agent.workspace_id is None
    assert subscribed_agent.config["source_agent_id"] == marketplace_agent.id


# ── Starter memory ────────────────────────────────────────────────────


async def test_starter_memory_creates_agent_level_row(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"calvin-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [
                        {
                            "memory_type": "instruction",
                            "scope": "guidance",
                            "content": "Reply within 1h.",
                            "importance": 8,
                            "confidence": 0.9,
                        }
                    ],
                }
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    await db_session.commit()

    agent = (await db_session.execute(select(Agent).where(Agent.slug == agent_slug))).scalar_one()
    mems = list((await db_session.execute(select(AgentMemory).where(AgentMemory.agent_id == agent.id))).scalars().all())
    assert len(mems) == 1
    m = mems[0]
    assert m.content == "Reply within 1h."
    assert m.user_id is None
    assert m.workspace_id == result.workspace_id
    assert m.source == "blueprint"
    assert m.importance == 8


# ── Knowledge packs ───────────────────────────────────────────────────


async def test_knowledge_pack_creates_document_group(
    db_session: AsyncSession,
    entity_id: str,
):
    pack_slug = f"competitor-intel-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.knowledge_packs": [
                {
                    "slug": pack_slug,
                    "title": f"Competitor Intelligence {entity_id}",
                    "purpose": "background on top competitors",
                    "mode": "skeleton",
                    "folder_structure": [{"path": "competitors/", "description": "..."}],
                    "starter_documents": [],
                    "external_source": None,
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    groups = list(
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.entity_id == entity_id))).scalars().all()
    )
    assert len(groups) == 1
    g = groups[0]
    assert g.name == f"Competitor Intelligence {entity_id}"
    assert (g.settings or {}).get("mode") == "skeleton"
    assert not any(t.kind == "knowledge_pack_document" for t in result.todos)
    reexported = await export_workspace(db_session, result.workspace_id, title="Copy")
    [reexported_pack] = reexported["embedded"]["knowledge_packs"]
    assert reexported_pack["slug"] == pack_slug
    assert reexported_pack["folder_structure"] == [
        {"path": "competitors/", "description": "..."},
    ]


async def test_knowledge_pack_inline_text_materializes_documents(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "embedded.knowledge_packs": [
                {
                    "slug": f"voice-{entity_id}",
                    "title": f"Voice Guide {entity_id}",
                    "purpose": "...",
                    "mode": "inline_text",
                    "folder_structure": [],
                    "starter_documents": [
                        {
                            "path": "voice.md",
                            "body_md": "# Voice\n\nFounder-led.",
                            "template": {
                                "id": "test-live-voice",
                                "mode": "live_projection",
                                "renderer": "test_live_voice",
                                "version": 1,
                            },
                        },
                        {"path": "examples.md", "body_md": "# Examples"},
                    ],
                    "external_source": None,
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    assert not any(t.kind == "knowledge_pack_document" for t in result.todos)
    group = (await db_session.execute(select(DocumentGroup).where(DocumentGroup.entity_id == entity_id))).scalar_one()
    documents = list(
        (
            await db_session.execute(
                select(Document)
                .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
                .where(DocumentGroupMember.group_id == group.id)
                .order_by(Document.name)
            )
        )
        .scalars()
        .all()
    )
    assert [document.name for document in documents] == ["examples.md", "voice.md"]
    assert documents[1].metadata_["content_text"] == "# Voice\n\nFounder-led."
    assert documents[1].metadata_["origin"]["workspace_id"] == result.workspace_id
    assert documents[1].metadata_["blueprint_template"] == {
        "id": "test-live-voice",
        "mode": "live_projection",
        "renderer": "test_live_voice",
        "version": 1,
    }
    assert documents[1].source == "blueprint"
    assert documents[1].visibility == "workspace"
    assert documents[1].classification == "public"

    reexported = await export_workspace(
        db_session,
        result.workspace_id,
        title="Copy",
        context=ExportContext(include_memory_files=True),
    )
    [reexported_pack] = reexported["embedded"]["knowledge_packs"]
    assert reexported_pack["slug"] == f"voice-{entity_id}"
    reexported_by_path = {
        document["path"]: document
        for document in reexported_pack["starter_documents"]
    }
    assert reexported_by_path["examples.md"]["body_md"] == "# Examples"
    assert reexported_by_path["examples.md"]["key"]
    assert reexported_by_path["voice.md"]["body_md"] == "# Voice\n\nFounder-led."
    assert reexported_by_path["voice.md"]["key"]
    assert reexported_by_path["voice.md"]["template"] == {
        "id": "test-live-voice",
        "mode": "live_projection",
        "renderer": "test_live_voice",
        "version": 1,
    }

    # Re-installing the same payload reuses both the pack and its documents;
    # it must not create duplicates or overwrite an operator's later edits.
    documents[1].metadata_ = {**documents[1].metadata_, "content_text": "Edited"}
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    reloaded = list(
        (
            await db_session.execute(
                select(Document)
                .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
                .where(DocumentGroupMember.group_id == group.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(reloaded) == 2
    assert next(row for row in reloaded if row.name == "voice.md").metadata_["content_text"] == "Edited"
    assert next(row for row in reloaded if row.name == "voice.md").metadata_["blueprint_template"]["id"] == (
        "test-live-voice"
    )


async def test_shared_knowledge_document_installs_once_with_two_memberships(
    db_session: AsyncSession,
    entity_id: str,
):
    shared_document = {
        "key": "shared-playbook",
        "path": "shared.md",
        "body_md": "# Shared playbook",
    }
    payload = _base_payload(
        **{
            "embedded.knowledge_packs": [
                {
                    "slug": "operations",
                    "title": f"Operations {entity_id}",
                    "mode": "inline_text",
                    "folder_structure": [],
                    "starter_documents": [dict(shared_document)],
                },
                {
                    "slug": "support",
                    "title": f"Support {entity_id}",
                    "mode": "inline_text",
                    "folder_structure": [],
                    "starter_documents": [dict(shared_document)],
                },
            ],
        }
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    await db_session.commit()

    groups = list((await db_session.execute(
        select(DocumentGroup).where(
            DocumentGroup.workspace_id == result.workspace_id,
        )
    )).scalars().all())
    assert len(groups) == 2
    rows = list((await db_session.execute(
        select(Document)
        .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
        .join(DocumentGroup, DocumentGroup.id == DocumentGroupMember.group_id)
        .where(DocumentGroup.workspace_id == result.workspace_id)
    )).scalars().all())
    assert len(rows) == 2
    assert rows[0].id == rows[1].id
    assert rows[0].metadata_["blueprint_document_key"] == "shared-playbook"


# ── End-to-end roundtrip ──────────────────────────────────────────────


async def test_subscription_after_embedded_agent_resolves_locally(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"calvin-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "C",
                    "system_prompt": "x",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "recipe.subscriptions": [
                {
                    "service_key": "social.x.reply",
                    "agent_slug": agent_slug,
                    "custom_prompt": None,
                    "config": {},
                }
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    assert len(result.subscription_ids) == 1
    missing = [t for t in result.todos if t.kind == "missing_agent"]
    assert len(missing) == 0

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    ready = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert ready is not None and ready.blocks_work is False

    subscription = await db_session.get(AgentSubscription, result.subscription_ids[0])
    assert subscription is not None
    subscription.status = "inactive"
    await db_session.flush()
    disabled = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert disabled is not None and disabled.blocks_work
