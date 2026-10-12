"""Blueprint recovery and live readiness agree on exact Marketplace provenance."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from packages.core.blueprints.exporter import export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint, sync_workspace_live_setup_requirements
from packages.core.blueprints.upgrade import apply
from packages.core.governance.approval_policy_service import account_defaults, initialize_workspace_approval
from packages.core.models.base import generate_ulid
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.user import Entity
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.workspace_readiness import _blueprint_install_todo_result, evaluate_workspace_blocking_setup
from test_blueprint_builtin_canonical import catalog  # noqa: F401
from test_blueprint_installer_embedded import _base_payload

pytestmark = pytest.mark.oss_regression


def payload_for(skill_id="catalog-writer", source="manor"):
    ref = {"slug": "writer", "marketplace_id": skill_id, "marketplace_source": source}
    return _base_payload(**{
        "contract.requires.skills": [ref],
        "embedded.agents": [{"slug": "author", "name": "Author", "system_prompt": "Write.",
                             "skill_bindings": ["writer"], "skill_binding_refs": [ref]}],
    })


async def workspace_fixture(db, *, config=None):
    entity = Entity(id=generate_ulid(), name="Setup account", settings={"approval_defaults": account_defaults()})
    ws = Workspace(id=generate_ulid(), entity_id=entity.id, name="Writing", settings={})
    agent = Agent(id=generate_ulid(), entity_id=entity.id, workspace_id=ws.id,
                  slug="author", name="Author", status="active", config={"model_mode": "inherit"})
    skill = Skill(id=generate_ulid(), entity_id=entity.id, slug="writer", name="Writer",
                  status="active", system_prompt="Write clearly.", is_public=True, config=config or {})
    binding = AgentSkillBinding(id=generate_ulid(), agent_id=agent.id, skill_id=skill.id, status="active")
    db.add_all([entity, ws, agent, skill, binding, AgentSubscription(
        entity_id=entity.id, workspace_id=ws.id, agent_id=agent.id, service_key="write", status="active")])
    await db.flush()
    await initialize_workspace_approval(db, ws)
    return ws, agent, skill, binding


def link_for(ws, skill, **overrides):
    return MarketplaceResourceLink(**{
        "id": generate_ulid(), "entity_id": ws.entity_id, "marketplace_source": "manor",
        "marketplace_resource_type": "skill", "marketplace_resource_id": "catalog-writer",
        "relationship": "installed_from", "scope_type": "entity", "scope_id": ws.entity_id,
        "local_resource_type": "skill", "local_resource_id": skill.id, **overrides,
    })


async def ready(db, ws, todo):
    return (await _blueprint_install_todo_result(
        db, ws, todo, integration_states={}, configured_channels=[],
    ))["ready"]


@pytest.mark.parametrize("provenance", ["direct_link", "legacy_config", "matching_both", "platform_config", "global_public"])
async def test_recovery_uses_exact_source_identity_and_legacy_receipts_remain_ready(db_session, provenance):
    ws, agent, skill, _binding = await workspace_fixture(db_session)
    source = "manor"
    # Manor IDs are opaque and may be ULIDs; source is never inferred from syntax.
    marketplace_id = generate_ulid() if provenance == "legacy_config" else "catalog-writer"
    if provenance in {"legacy_config", "matching_both"}:
        skill.config = {"source": "manor_marketplace", "marketplace_id": marketplace_id}
    if provenance == "platform_config":
        source = "platform"
        skill.config = {"source": "marketplace", "source_skill_id": marketplace_id}
    if provenance == "global_public":
        source, marketplace_id = "platform", skill.id
        skill.entity_id = None
    if provenance in {"direct_link", "matching_both"}:
        db_session.add(link_for(ws, skill))
    await db_session.flush()
    payload = payload_for(marketplace_id, source)
    [todo] = await sync_workspace_live_setup_requirements(db_session, workspace=ws, payload=payload)
    assert todo["payload"]["installed_skill_id"] == skill.id
    assert todo["payload"]["marketplace_source"] == source
    assert await ready(db_session, ws, todo)
    legacy = deepcopy(todo)
    legacy["payload"].pop("marketplace_source")
    assert await ready(db_session, ws, legacy)
    legacy["payload"].pop("installed_skill_id")
    assert await ready(db_session, ws, legacy)
    exported = await export_workspace(db_session, ws.id, title=ws.name)
    assert exported["embedded"]["agents"][0]["skill_binding_refs"] == [{
        "slug": "writer", "marketplace_id": marketplace_id, "marketplace_source": source,
    }]


@pytest.mark.parametrize("invalid", [
    "wrong_required_source", "foreign_skill", "inactive_skill", "inactive_binding",
    "foreign_link", "wrong_link_type", "conflicting_links", "conflicting_config",
    "wrong_config_source", "same_slug_wrong_id", "duplicate_installs", "global_private",
])
async def test_invalid_or_ambiguous_provenance_never_satisfies_recovery_or_cached_readiness(db_session, invalid):
    ws, agent, skill, binding = await workspace_fixture(db_session)
    payload = payload_for()
    link = link_for(ws, skill)
    db_session.add(link)
    if invalid == "wrong_required_source":
        payload = payload_for(source="platform")
    elif invalid == "foreign_skill":
        skill.entity_id = generate_ulid()
    elif invalid == "inactive_skill":
        skill.status = "inactive"
    elif invalid == "inactive_binding":
        binding.status = "inactive"
    elif invalid == "foreign_link":
        link.entity_id = generate_ulid()
    elif invalid == "wrong_link_type":
        link.marketplace_resource_type = "agent"
    elif invalid == "conflicting_links":
        db_session.add(link_for(ws, skill, scope_type="resource", scope_id=skill.id,
                               marketplace_resource_id="other-writer"))
    elif invalid == "conflicting_config":
        skill.config = {"marketplace_id": "other-writer"}
    elif invalid == "wrong_config_source":
        skill.config = {"marketplace_id": "catalog-writer", "marketplace_source": "platform"}
    elif invalid == "same_slug_wrong_id":
        link.marketplace_resource_id = "other-writer"
    elif invalid == "duplicate_installs":
        other = Skill(id=generate_ulid(), entity_id=ws.entity_id, slug="other-writer", name="Other writer",
                      status="active", system_prompt="Write differently.", config={"marketplace_id": "catalog-writer"})
        db_session.add_all([other, AgentSkillBinding(agent_id=agent.id, skill_id=other.id, status="active")])
    elif invalid == "global_private":
        skill.entity_id, skill.is_public = None, False
    await db_session.flush()
    [todo] = await sync_workspace_live_setup_requirements(db_session, workspace=ws, payload=payload)
    assert todo["payload"]["installed_skill_id"] is None
    assert not await ready(db_session, ws, todo)
    todo["payload"]["installed_skill_id"] = skill.id
    assert not await ready(db_session, ws, todo)


@pytest.mark.usefixtures("catalog")
async def test_builtin_provenance_cannot_be_spoofed_by_source_or_cached_local_id(db_session):
    ws, _agent, skill, _binding = await workspace_fixture(db_session, config={"source_skill_id": "builtin:file_engine"})
    skill.slug = "file_engine"
    db_session.add(link_for(ws, skill, marketplace_source="platform", marketplace_resource_id="builtin:file_engine"))
    await db_session.flush()
    [todo] = await sync_workspace_live_setup_requirements(db_session, workspace=ws, payload=payload_for("builtin:file_engine", "platform"))
    assert todo["payload"]["installed_skill_id"] is None
    todo["payload"]["installed_skill_id"] = skill.id
    assert not await ready(db_session, ws, todo)
    todo["payload"].pop("marketplace_source")
    assert not await ready(db_session, ws, todo)


@pytest.mark.parametrize("remove_link", [False, True])
async def test_real_platform_copy_uses_own_source_identity_over_inherited_catalog_hint(db_session, remove_link):
    from packages.core.services.marketplace_skill_service import ensure_marketplace_skill_installed

    ws, agent, old_skill, binding = await workspace_fixture(db_session)
    source = Skill(id=generate_ulid(), entity_id=None, name="Platform source", slug="platform-writer",
                   system_prompt="Write safely.", status="active", is_public=True,
                   config={"marketplace_id": "inherited-upstream-hint", "marketplace_source": "manor"})
    db_session.add(source)
    await db_session.flush()
    installed = await ensure_marketplace_skill_installed(db_session, entity_id=ws.entity_id, skill_id=source.id)
    assert installed.config["marketplace_id"] == "inherited-upstream-hint"
    assert installed.config["marketplace_source"] == "manor"
    assert installed.config["source_skill_id"] == source.id
    binding.skill_id = installed.id
    if remove_link:
        for link in (await db_session.scalars(select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == ws.entity_id,
            MarketplaceResourceLink.local_resource_id == installed.id,
        ))).all():
            await db_session.delete(link)
    await db_session.flush()
    [todo] = await sync_workspace_live_setup_requirements(db_session, workspace=ws, payload=payload_for(source.id, "platform"))
    assert todo["payload"]["installed_skill_id"] == installed.id
    assert await ready(db_session, ws, todo)
    todo["payload"].pop("marketplace_source")
    assert await ready(db_session, ws, todo)
    exported = await export_workspace(db_session, ws.id, title=ws.name)
    assert exported["embedded"]["agents"][0]["skill_binding_refs"] == [{
        "slug": installed.slug, "marketplace_id": source.id, "marketplace_source": "platform",
    }]
    wrong_source = deepcopy(todo)
    wrong_source["payload"]["marketplace_source"] = "manor"
    assert not await ready(db_session, ws, wrong_source)
    if not remove_link:
        link = (await db_session.scalars(select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == ws.entity_id,
            MarketplaceResourceLink.local_resource_id == installed.id,
        ))).one()
        link.marketplace_resource_id = "wrong-current-platform-source"
        await db_session.flush()
        assert not await ready(db_session, ws, todo)


