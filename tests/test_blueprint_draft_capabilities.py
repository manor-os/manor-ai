from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import runtime_lint_workspace_draft
from packages.core.blueprints.freshness import (
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY,
    UPGRADE_UNSUPPORTED_FINGERPRINT_KEY,
    blueprint_content_fingerprint,
    blueprint_section_fingerprints,
    blueprint_upgrade_unsupported_fingerprint,
)
from packages.core.constants.workspace_drafts import (
    CREATION_PREFERENCES_FIELD,
    CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION,
    WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD,
)
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.worker import SubscriptionWorker
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services.workspace_draft_service import (
    _refresh_missing_from_lint,
    apply_blueprint,
    finalize_draft,
)
from packages.core.services.workspace_setup_service import DEFAULT_FIELDS


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("blueprint_mode", [False, True])
@pytest.mark.parametrize("user_mode", [None, False, True])
async def test_blueprint_mode_respects_explicit_creation_switch(
    db_session, blueprint_mode, user_mode,
) -> None:
    from packages.core.services.workspace_draft_service import (
        apply_public_field_updates,
        create_draft_shell,
    )

    entity_id, user_id = generate_ulid(), generate_ulid()
    payload = _payload(agent_slug="mode-agent", skill_slug="mode-skill", server_slug="mode-server")
    payload["recipe"]["operating_model"]["heartbeat_enabled"] = blueprint_mode
    blueprint = WorkspaceBlueprint(
        id=generate_ulid(), entity_id=entity_id, slug=f"mode-{entity_id}",
        title="Runtime mode Blueprint", payload=payload,
        payload_version="1.1", status="published",
    )
    db_session.add(blueprint)
    draft = await create_draft_shell(db_session, entity_id=entity_id, user_id=user_id)
    if user_mode is not None:
        apply_public_field_updates(draft, {"heartbeat_enabled": user_mode})
    await db_session.commit()
    applied = await apply_blueprint(
        db_session, draft_id=draft.id, entity_id=entity_id,
        blueprint_id=blueprint.id, user_id=user_id,
    )
    assert applied.fields["heartbeat_enabled"] is (
        blueprint_mode if user_mode is None else user_mode
    )
    assert applied.fields[CREATION_PREFERENCES_FIELD]["autonomy_confirmed"] is (user_mode is not None)


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
                "heartbeat_enabled": True,
                "heartbeat_cadence": "weekly",
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
                "stat_key": "workspace.tasks.completed",
                "target_value": 10,
                "measurement_cadence": None,
                "priority": 3,
            }],
            "stats": [{"library_key": "workspace.tasks.completed", "collection_cadence": "daily"}],
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


def _with_exact_platform_skill(payload: dict, *, marketplace_id: str) -> dict:
    payload["contract"]["requires"]["skills"] = [{
        "slug": "exact-platform-skill",
        "marketplace_source": "platform",
        "marketplace_id": marketplace_id,
    }]
    payload["embedded"]["agents"][0]["skill_binding_refs"] = [{
        "slug": payload["embedded"]["skills"][0]["slug"],
        "marketplace_source": "platform",
        "marketplace_id": marketplace_id,
    }]
    return payload


async def test_draft_finalize_persists_only_materialized_personalization(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.services import workspace_draft_service

    entity_id = generate_ulid()
    creator_id = generate_ulid()
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=creator_id,
        fields={
            **dict(DEFAULT_FIELDS),
            "_blueprint_variable_declarations": [{
                "key": "persistent_name",
                "materialize": True,
            }, {
                "key": "one_shot_instruction",
                "materialize": False,
            }],
            "blueprint_personalization": {
                "persistent_name": "Acme",
                "one_shot_instruction": "Prepare the launch",
            },
            "_blueprint_settings": {
                "persistent_name": "{{persistent_name}}",
                "one_shot_instruction": "{{one_shot_instruction}}",
            },
        },
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add(draft)
    await db_session.flush()
    captured: dict[str, object] = {}

    async def verified(*_args, **_kwargs):
        return True

    async def fake_finalize(session, _db, *, progress=None):
        captured["fields"] = session.fields
        return generate_ulid()

    monkeypatch.setattr(
        workspace_draft_service,
        "_refresh_missing_from_lint",
        verified,
    )
    monkeypatch.setattr(
        workspace_draft_service,
        "finalize_setup",
        fake_finalize,
    )

    await finalize_draft(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
        user_id=creator_id,
    )

    fields = captured["fields"]
    assert isinstance(fields, dict)
    settings = fields["_blueprint_settings"]
    assert settings["persistent_name"] == "Acme"
    assert settings["one_shot_instruction"] == "Prepare the launch"
    assert settings["blueprint_personalization"] == {
        "persistent_name": "Acme",
    }


async def test_apply_blueprint_materializes_agent_capabilities(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    entity_id = generate_ulid()
    creator_id = generate_ulid()
    agent_slug = f"blueprint-agent-{entity_id}"
    skill_slug = f"blueprint-skill-{entity_id}"
    server_slug = f"blueprint-mcp-{entity_id}"
    payload = _payload(
        agent_slug=agent_slug,
        skill_slug=skill_slug,
        server_slug=server_slug,
    )
    payload["recipe"]["operating_model"]["settings"] = {
        "ledger_contracts": [{
            "contract_id": "manor.recruiting_ledger/v1",
            "directory": "recruiting-ledger",
        }],
    }
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
        user_id=creator_id,
        fields={
            **dict(DEFAULT_FIELDS),
            WORKSPACE_DRAFT_SCHEMA_VERSION_FIELD: (
                CURRENT_WORKSPACE_DRAFT_SCHEMA_VERSION
            ),
            CREATION_PREFERENCES_FIELD: {
                "goal_confirmed": True,
                "autonomy_confirmed": True,
            },
        },
        messages=[],
        missing=[],
        ready=True,
        status="ready",
    )
    db_session.add_all([server, blueprint, draft])
    await db_session.commit()

    applied = await apply_blueprint(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
        blueprint_id=blueprint.id,
        user_id=creator_id,
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
    assert applied.fields["goals"][0]["stat_key"] == "workspace.tasks.completed"
    assert applied.fields["stats"] == payload["recipe"]["stats"]
    assert applied.fields["heartbeat_enabled"] is True
    assert applied.fields["heartbeat_cadence"] == "weekly"
    assert applied.fields["_blueprint_operating_model"]["strategist"]["cadence"] == "daily"
    assert applied.fields["_blueprint_settings"]["ledger_contracts"][0]["contract_id"] == (
        "manor.recruiting_ledger/v1"
    )
    assert applied.fields["_blueprint_disable_business_ledger_matching"] is True
    assert applied.fields[CREATION_PREFERENCES_FIELD] == {
        "goal_confirmed": False,
        "autonomy_confirmed": True,
    }
    assert applied.missing == ["creation_preferences"]
    assert applied.status == "active"
    assert applied.ready is False

    from packages.core.ai.tools.workspace_arch_tools import (
        _confirm_creation_preferences,
    )

    confirmation = await _confirm_creation_preferences(
        db_session,
        entity_id=entity_id,
        user_id=creator_id,
        draft_id=draft.id,
        goal_choice="configured",
        autonomous_enabled=True,
        autonomy_cadence="weekly",
    )
    assert '"ok": true' in confirmation
    await _refresh_missing_from_lint(db_session, applied)
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
        user_id=creator_id,
    )
    await db_session.commit()
    assert finalized.finalized_workspace_id == workspace_id
    workspace = await db_session.get(Workspace, workspace_id)
    assert workspace is not None
    assert workspace.heartbeat_enabled is True
    assert workspace.heartbeat_cadence == "weekly"
    from packages.core.models.goal import Goal
    from packages.core.models.workspace_stat import WorkspaceStat

    goal = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace_id))).scalar_one()
    stat = (await db_session.execute(select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace_id))).scalar_one()
    assert goal.stat_id == stat.id
    assert stat.key == "workspace.tasks.completed"
    assert stat.collection_cadence == "daily"
    assert stat.current_value is None and goal.current_value is None
    assert goal.measurement_source is None and goal.measurement_cadence is None
    assert workspace.settings["ledger_contracts"][0]["contract_id"] == (
        "manor.recruiting_ledger/v1"
    )
    blueprint_record = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    assert blueprint_record[BLUEPRINT_VERSION_KEY] == blueprint.content_version
    assert blueprint_record[CONTENT_FINGERPRINT_KEY] == (
        blueprint_content_fingerprint(payload)
    )
    assert blueprint_record[SECTION_FINGERPRINTS_KEY] == (
        blueprint_section_fingerprints(payload)
    )

    subscription = (await db_session.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.service_key == "blueprint_operations",
        )
    )).scalar_one()
    agent = await db_session.get(Agent, subscription.agent_id)
    assert agent is not None
    assert agent.slug == agent_slug
    assert agent.workspace_id == workspace_id
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
    assert skill.workspace_id == workspace_id

    links = list((await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_id == blueprint.id,
            MarketplaceResourceLink.scope_id == workspace_id,
        )
    )).scalars().all())
    assert {
        (link.relationship, link.local_resource_type, link.local_resource_id)
        for link in links
    } == {
        ("installed_from", "workspace", workspace_id),
        ("installed_component", "agent", agent.id),
        ("installed_component", "skill", skill.id),
    }

    second_draft = WorkspaceDraft(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=creator_id,
        fields=dict(DEFAULT_FIELDS),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add(second_draft)
    await db_session.commit()
    await apply_blueprint(
        db_session,
        draft_id=second_draft.id,
        entity_id=entity_id,
        blueprint_id=blueprint.id,
        user_id=creator_id,
    )
    second_workspace_id, _ = await finalize_draft(
        db_session,
        draft_id=second_draft.id,
        entity_id=entity_id,
        user_id=creator_id,
    )
    await db_session.commit()
    second_subscription = (await db_session.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == second_workspace_id,
            AgentSubscription.service_key == "blueprint_operations",
        )
    )).scalar_one()
    second_agent = await db_session.get(Agent, second_subscription.agent_id)
    assert second_agent is not None
    second_skill = (await db_session.execute(
        select(Skill).join(
            AgentSkillBinding,
            AgentSkillBinding.skill_id == Skill.id,
        ).where(AgentSkillBinding.agent_id == second_agent.id)
    )).scalar_one()
    assert second_agent.id != agent.id
    assert second_skill.id != skill.id
    assert second_agent.workspace_id == second_workspace_id
    assert second_skill.workspace_id == second_workspace_id


@pytest.mark.parametrize(
    ("skill_scope", "available"),
    [("missing", False), ("marketplace", True), ("local", False)],
)
async def test_blueprint_draft_lints_exact_platform_skill_availability(
    db_session: AsyncSession,
    skill_scope: str,
    available: bool,
):
    entity_id = generate_ulid()
    creator_id = generate_ulid()
    marketplace_id = generate_ulid()
    server_slug = f"blueprint-exact-mcp-{entity_id}"
    payload = _with_exact_platform_skill(
        _payload(
            agent_slug=f"blueprint-exact-agent-{entity_id}",
            skill_slug=f"blueprint-exact-skill-{entity_id}",
            server_slug=server_slug,
        ),
        marketplace_id=marketplace_id,
    )
    rows = [
        MCPServer(
            id=generate_ulid(),
            server_key=server_slug,
            name="Blueprint exact MCP",
            transport="builtin",
            auth_type="none",
            status="active",
        ),
        WorkspaceBlueprint(
            id=generate_ulid(),
            entity_id=entity_id,
            slug=f"exact-skill-copy-{entity_id}",
            title="Exact Skill Copy",
            payload=payload,
            payload_version="1.1",
            status="published",
        ),
        WorkspaceDraft(
            id=generate_ulid(),
            entity_id=entity_id,
            user_id=creator_id,
            fields=dict(DEFAULT_FIELDS),
            messages=[],
            missing=[],
            ready=False,
            status="active",
        ),
    ]
    if skill_scope != "missing":
        rows.append(Skill(
            id=marketplace_id,
            entity_id=None if skill_scope == "marketplace" else entity_id,
            workspace_id=generate_ulid() if skill_scope == "local" else None,
            name="Exact public Marketplace Skill",
            slug=f"exact-public-skill-{entity_id}",
            system_prompt="Perform the exact public procedure.",
            is_public=True,
            status="active",
        ))
    db_session.add_all(rows)
    await db_session.commit()

    applied = await apply_blueprint(
        db_session,
        draft_id=rows[2].id,
        entity_id=entity_id,
        blueprint_id=rows[1].id,
        user_id=creator_id,
    )
    lint = await runtime_lint_workspace_draft(
        db_session,
        entity_id=entity_id,
        draft_id=applied.id,
        user_id=creator_id,
    )

    exact_issues = [
        issue for issue in (lint or {}).get("issues", [])
        if "skill_binding_refs" in str(issue.get("where") or "")
    ]
    assert applied.ready is available
    assert bool(exact_issues) is not available
    if exact_issues:
        assert exact_issues[0]["severity"] == "P0"
        assert marketplace_id in exact_issues[0]["message"]


async def test_workspace_draft_lint_rejects_local_id_as_marketplace_agent(
    db_session: AsyncSession,
):
    entity_id = generate_ulid()
    creator_id = generate_ulid()
    local_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Local Agent",
        system_prompt="Local behavior.",
        status="active",
    )
    fields = dict(DEFAULT_FIELDS)
    fields.update({
        "name": "Strict Marketplace Draft",
        "kind": "operations",
        "operating_context": "Verify Marketplace identity boundaries.",
        "primary_work": "Run one Marketplace-backed service.",
        "services": [{
            "service_key": "marketplace_service",
            "name": "Marketplace Service",
            "description": "Use an exact Marketplace Agent.",
            "autonomy_level": "supervised",
            "owner_role": "operator",
        }],
        "agent_mappings": [{
            "service_key": "marketplace_service",
            "strategy": "match",
            "agent_id": local_agent.id,
            "marketplace_agent_id": local_agent.id,
        }],
        "goals": [],
    })
    draft = WorkspaceDraft(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=creator_id,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([local_agent, draft])
    await db_session.commit()

    lint = await runtime_lint_workspace_draft(
        db_session,
        entity_id=entity_id,
        draft_id=draft.id,
        user_id=creator_id,
    )
    exact_issues = [
        issue for issue in (lint or {}).get("issues", [])
        if str(issue.get("where") or "").endswith("marketplace_agent_id")
    ]

    assert len(exact_issues) == 1
    assert exact_issues[0]["severity"] == "P0"
    assert "local Agent ID" in exact_issues[0]["message"]


@pytest.mark.parametrize("source_changed,with_variables,receipt_snapshot", [
    (False, False, False), (True, False, False), (True, True, False), (True, True, True),
])
async def test_draft_finalize_installs_exact_marketplace_agent_locally(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    source_changed: bool,
    with_variables: bool,
    receipt_snapshot: bool,
):
    entity_id = generate_ulid()
    creator_id = generate_ulid()
    agent_slug = f"external-agent-{entity_id}"
    source_agent = Agent(
        id=generate_ulid(),
        entity_id=None,
        name="Marketplace Agent",
        slug=agent_slug,
        system_prompt="Run the Marketplace capability.",
        config={},
        is_template=True,
        is_public=True,
        status="active",
    )
    payload = _payload(
        agent_slug=agent_slug,
        skill_slug=f"unused-skill-{entity_id}",
        server_slug=f"unused-mcp-{entity_id}",
    )
    payload["embedded"]["agents"] = []
    payload["embedded"]["skills"] = []
    payload["contract"]["requires"]["skills"] = []
    payload["contract"]["requires"]["mcp_servers"] = []
    payload["contract"]["requires"]["agents"] = [{
        "slug": agent_slug,
        "marketplace_id": source_agent.id,
        "min_version": "1.0",
    }]
    payload["recipe"]["subscriptions"][0]["marketplace_agent_id"] = (
        source_agent.id
    )
    if with_variables:
        payload["contract"]["variables"] = [{
            "key": "company", "default": "Acme", "required": True, "materialize": True,
        }]
        payload["recipe"]["operating_model"]["primary_work"] = "Operate {{company}}"
    blueprint = WorkspaceBlueprint(
        id=generate_ulid(),
        entity_id=entity_id,
        slug=f"external-agent-blueprint-{entity_id}",
        title="External Agent Blueprint",
        payload=payload,
        payload_version="1.1",
        content_version="1.0.0",
        status="published",
    )
    draft = WorkspaceDraft(
        id=generate_ulid(),
        entity_id=entity_id,
        user_id=creator_id,
        fields=dict(DEFAULT_FIELDS),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    if receipt_snapshot:
        from unittest.mock import AsyncMock
        from packages.core.models.blueprint_purchase import BlueprintPurchase

        blueprint.entity_id = generate_ulid()
        blueprint.status = "archived"
        blueprint.payload = copy.deepcopy(payload)
        blueprint.payload["recipe"]["operating_model"]["primary_work"] = "Unpublished work"
        db_session.add(BlueprintPurchase(
            blueprint_id=blueprint.id, buyer_entity_id=entity_id, buyer_user_id=creator_id,
            seller_entity_id=blueprint.entity_id, amount_cents=1000, status="completed",
            seller_amount_cents=1000,
            blueprint_title=blueprint.title,
            payload_snapshot=copy.deepcopy(payload), blueprint_content_version="0.9.0",
        ))
        monkeypatch.setattr(
            "packages.core.services.marketplace_billing.require_paid_marketplace_plan",
            AsyncMock(),
        )
    db_session.add_all([source_agent, blueprint, draft])
    await db_session.commit()

    await apply_blueprint(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
        blueprint_id=blueprint.id,
        user_id=creator_id,
    )
    if with_variables:
        from packages.core.services.workspace_draft_service import apply_public_field_updates

        apply_public_field_updates(draft, {"blueprint_personalization": {"company": "NewCo"}})
    if source_changed:
        blueprint.payload = copy.deepcopy(payload)
        blueprint.payload["recipe"]["operating_model"]["primary_work"] = "New source work"
        blueprint.content_version = "2.0.0"
        await db_session.flush()
    from apps.api.routers.workspace_drafts import _hydrate_response

    await db_session.refresh(draft)
    response = await _hydrate_response(db_session, draft)
    assert "_blueprint_source_payload" not in response.fields
    workspace_id, _ = await finalize_draft(
        db_session,
        draft_id=draft.id,
        entity_id=entity_id,
        user_id=creator_id,
    )
    await db_session.commit()

    from packages.core.blueprints.installer import resolve_install_variables
    from packages.core.blueprints.payload import migrate_payload

    workspace = await db_session.get(Workspace, workspace_id)
    record = workspace.settings[BLUEPRINT_SETTINGS_KEY]
    resolved, _ = resolve_install_variables(
        migrate_payload(payload), {"company": "NewCo"} if with_variables else {},
    )
    assert record[BLUEPRINT_VERSION_KEY] == ("0.9.0" if receipt_snapshot else "1.0.0")
    assert workspace.primary_work == resolved["recipe"]["operating_model"]["primary_work"]
    assert record[CONTENT_FINGERPRINT_KEY] == blueprint_content_fingerprint(payload)
    assert record[UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] == blueprint_upgrade_unsupported_fingerprint(payload)
    assert record[MATERIALIZED_UPGRADE_UNSUPPORTED_FINGERPRINT_KEY] == blueprint_upgrade_unsupported_fingerprint(resolved)
    assert "_blueprint_source_payload" not in workspace.settings

    from packages.core.blueprints.upgrade import plan

    same_source = await plan(db_session, workspace=workspace, payload=resolved, source_payload=payload)
    assert same_source["unsupported_changes"] is False
    if source_changed:
        latest, _ = resolve_install_variables(
            migrate_payload(blueprint.payload), {"company": "NewCo"} if with_variables else {},
        )
        changed_source = await plan(db_session, workspace=workspace, payload=latest, source_payload=blueprint.payload)
        assert changed_source["unsupported_changes"] is True

    subscription = (await db_session.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == workspace_id,
        )
    )).scalar_one()
    installed = await db_session.get(Agent, subscription.agent_id)
    assert installed is not None
    assert installed.id != source_agent.id
    assert installed.entity_id == entity_id
    assert installed.config["source_agent_id"] == source_agent.id
    link = (await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_type == "agent",
            MarketplaceResourceLink.marketplace_resource_id == source_agent.id,
            MarketplaceResourceLink.local_resource_id == installed.id,
        )
    )).scalar_one()
    assert link.relationship == "installed_from"


async def test_draft_channel_selection_survives_public_patch_and_binds_on_finalize(
    db_session: AsyncSession,
):
    from apps.api.routers.workspace_drafts import update_draft_fields
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel

    entity_id, user_id = generate_ulid(), generate_ulid()
    accounts = [ChannelConfig(
        entity_id=entity_id, owner_user_id=owner, channel_type="telegram",
        provider="telegram_bot", name=name, config={}, credentials={}, status="active",
    ) for owner, name in [(user_id, "First"), (user_id, "Second"), (generate_ulid(), "Other user")]]
    payload = _payload(agent_slug=f"channel-agent-{entity_id}", skill_slug="unused", server_slug="unused")
    payload["embedded"]["skills"] = []
    payload["embedded"]["agents"][0]["skill_bindings"] = []
    payload["embedded"]["agents"][0]["mcp_bindings"] = []
    payload["contract"]["requires"]["mcp_servers"] = []
    payload["contract"]["channels"] = [{"channel_type": "telegram", "required": True}]
    blueprint = WorkspaceBlueprint(
        entity_id=entity_id, slug=f"channels-{entity_id}", title="Channel selection",
        payload=payload, payload_version="1.1", status="published",
    )
    draft = WorkspaceDraft(
        entity_id=entity_id, user_id=user_id, fields=dict(DEFAULT_FIELDS),
        messages=[], missing=[], ready=False, status="active",
    )
    db_session.add_all([*accounts, blueprint, draft])
    await db_session.flush()
    await apply_blueprint(db_session, draft_id=draft.id, entity_id=entity_id,
                          blueprint_id=blueprint.id, user_id=user_id)
    assert not draft.ready
    [requirement] = draft.fields["_blueprint_channel_requirements"]
    assert {option["id"] for option in requirement["resource_options"]} == {accounts[0].id, accounts[1].id}
    key = requirement["requirement_key"]
    actor = SimpleNamespace(id=user_id, entity_id=entity_id)
    rejected = await update_draft_fields(
        draft.id, {"blueprint_channel_config_ids": {key: accounts[2].id}}, user=actor, db=db_session,
    )
    assert not rejected.ready
    selected = await update_draft_fields(
        draft.id, {"blueprint_channel_config_ids": {key: accounts[1].id}}, user=actor, db=db_session,
    )
    assert selected.ready
    await _refresh_missing_from_lint(db_session, draft)
    assert draft.fields["_blueprint_channel_config_ids"] == {key: accounts[1].id}
    accounts[1].status = "disabled"
    await db_session.flush()
    await _refresh_missing_from_lint(db_session, draft)
    assert not draft.ready
    accounts[1].status = "active"
    await db_session.flush()
    workspace_id, _ = await finalize_draft(
        db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id,
    )
    binding = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace_id, Channel.type == "telegram",
    ))).scalar_one()
    assert binding.config["channel_config_id"] == accounts[1].id
