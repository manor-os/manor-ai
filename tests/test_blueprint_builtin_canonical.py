"""Legacy duplicate built-ins retain runtime IDs while portable identity stays exact."""
import copy
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from packages.core.blueprints.exporter import ExportError, export_workspace
from packages.core.blueprints.installer import sync_workspace_live_setup_requirements
from packages.core.governance.approval_policy_service import initialize_workspace_approval
from packages.core.models.base import generate_ulid
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.workflow import WorkflowDefinition
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services import builtin_skill_loader as loader
from packages.core.services.marketplace_skill_service import ensure_marketplace_skill_installed

pytestmark = pytest.mark.oss_regression


@pytest.fixture
async def catalog(db_session, monkeypatch, tmp_path):
    bundle = tmp_path / "file_engine"
    bundle.mkdir()
    (bundle / "SKILL.md").write_text("---\nname: file_engine\nversion: 1.0.0\n---\nRead verified workspace files.\n")
    (bundle / "config.json").write_text(json.dumps({
        "type": "runtime_guidance", "execution_mode": "instructions_only",
        "tools": ["read_file"], "search": {"keywords": ["files"]},
    }))
    monkeypatch.setattr(loader, "_builtin_skill_dirs", lambda: [bundle])
    # Reproduce an already-drifted legacy catalog inside this isolated test DB.
    # Transaction rollback restores the index after every test.
    await db_session.execute(text("DROP INDEX IF EXISTS uq_skills_builtin_slug"))
    canonical = (await loader.seed_builtin_skills(db_session))[0]
    return canonical


def duplicate(canonical, **changes):
    fields = {column.name: copy.deepcopy(getattr(canonical, column.name))
              for column in Skill.__table__.columns
              if column.name not in {"id", "created_at", "updated_at"}}
    return Skill(id=generate_ulid(), **{**fields, **changes})


def snapshot(rows):
    return [{column.name: copy.deepcopy(getattr(row, column.name))
             for column in row.__table__.columns} for row in rows]


async def source_workspace(db, skill, *, kind="agent"):
    ws = Workspace(id=generate_ulid(), entity_id=generate_ulid(), name="Canonical files",
                   kind="general", operating_model={}, settings={"access_mode": "members_only"})
    agent = Agent(id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
                  slug="researcher", name="Researcher", status="active", is_public=False,
                  config={"model_mode": "inherit"})
    db.add_all([ws, agent])
    await db.flush()
    await initialize_workspace_approval(db, ws)
    db.add(AgentSubscription(id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
                             agent_id=agent.id, service_key="research", status="active"))
    if kind == "agent":
        db.add(AgentSkillBinding(id=generate_ulid(), agent_id=agent.id, skill_id=skill.id, status="active"))
    elif kind == "workflow":
        db.add(WorkflowDefinition(id=generate_ulid(), entity_id=ws.entity_id, workspace_id=ws.id,
            name="Read", status="active", is_active=True, steps=[
                {"id": "start", "type": "trigger", "next": ["read"]},
                {"id": "read", "type": "tool", "config": {"tool": "manor", "args": {"skill_id": skill.id}}, "next": []},
            ]))
    elif kind == "automation":
        db.add(ScheduledJob(entity_id=ws.entity_id, workspace_id=ws.id, job_id=f"files-{ws.id}",
                           execution_type="skill", execution_target={"skill_id": skill.id}, enabled=False))
    await db.flush()
    return ws, agent


async def test_unique_packaged_match_ignores_stale_order_and_preserves_rows_and_bindings(db_session, catalog):
    canonical = catalog
    stale = duplicate(canonical, system_prompt="Old instructions", tools=["old_tool"],
                      config={**canonical.config, "source_sha256": "stale"},
                      created_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
    db_session.add(stale)
    agents = [generate_ulid(), generate_ulid()]
    bindings = [AgentSkillBinding(id=generate_ulid(), agent_id=agent, skill_id=skill.id,
                                 status="active", config={"selection": "explicit"})
                for agent in agents for skill in (stale, canonical)]
    db_session.add_all(bindings)
    await db_session.flush()
    before = snapshot([stale, canonical, *bindings])
    resolved = await ensure_marketplace_skill_installed(db_session, entity_id=generate_ulid(), skill_id="builtin:file_engine")
    assert resolved.id == canonical.id
    # Explicit legacy runtime IDs and every authorization row remain available.
    assert await db_session.get(Skill, stale.id) is stale
    assert stale.status == "active" and stale.is_public
    assert snapshot([stale, canonical, *bindings]) == before


@pytest.mark.parametrize("drift", ["system_prompt", "tools", "input_schema", "output_format", "config", "version", "digest", "source", "skill_dir"])
async def test_claimed_builtin_digest_cannot_hide_behavior_or_provenance_drift(db_session, catalog, drift):
    canonical = catalog
    if drift == "config":
        canonical.config = {**canonical.config, "allowed_surfaces": ["unreviewed"]}
    elif drift in {"digest", "source", "skill_dir"}:
        key = {"digest": "source_sha256", "source": "source", "skill_dir": "skill_dir"}[drift]
        canonical.config = {**canonical.config, key: "untrusted"}
    else:
        setattr(canonical, drift, {"system_prompt": "Changed", "tools": ["write_file"],
            "input_schema": {"type": "array"}, "output_format": "html", "version": "2.0.0"}[drift])
    await db_session.flush()
    with pytest.raises(ValueError, match="missing or ambiguous"):
        await ensure_marketplace_skill_installed(db_session, entity_id=generate_ulid(), skill_id="builtin:file_engine")


async def test_identical_packaged_duplicates_fail_closed_and_same_slug_local_row_is_ignored(db_session, catalog):
    local = duplicate(catalog, entity_id=generate_ulid())
    db_session.add(local)
    await db_session.flush()
    assert (await loader.resolve_packaged_builtin_skill(db_session, "file_engine")).id == catalog.id
    db_session.add(duplicate(catalog))
    await db_session.flush()
    with pytest.raises(ValueError, match="missing or ambiguous"):
        await loader.resolve_packaged_builtin_skill(db_session, "file_engine")


@pytest.mark.parametrize("kind", ["agent", "workflow", "automation"])
async def test_export_rejects_noncanonical_behavior_in_every_portable_skill_surface(db_session, catalog, kind):
    stale = duplicate(catalog, system_prompt="Old behavior", config={**catalog.config, "source_sha256": "stale"})
    db_session.add(stale)
    await db_session.flush()
    ws, _agent = await source_workspace(db_session, stale, kind=kind)
    with pytest.raises(ExportError, match="does not match its packaged identity"):
        await export_workspace(db_session, ws.id, title=ws.name)
    assert stale.status == "active"


@pytest.mark.parametrize("bound", ["canonical_and_stale", "stale_only", "spoofed_source"])
async def test_live_setup_resolves_only_the_bound_packaged_identity(db_session, catalog, bound):
    stale = duplicate(catalog, system_prompt="Old behavior", config={**catalog.config, "source_sha256": "stale"})
    if bound == "spoofed_source":
        stale.entity_id = generate_ulid()
        stale.config = {"source_skill_id": "builtin:file_engine"}
    db_session.add(stale)
    await db_session.flush()
    ws, agent = await source_workspace(db_session, stale)
    if bound == "spoofed_source":
        stale.entity_id = ws.entity_id
        # Even an unresolved packaged identity cannot fall back to a forged source ID.
        catalog.config = {**catalog.config, "source_sha256": "stale"}
    if bound == "canonical_and_stale":
        db_session.add(AgentSkillBinding(id=generate_ulid(), agent_id=agent.id, skill_id=catalog.id, status="active"))
    await db_session.flush()
    bindings = list((await db_session.scalars(select(AgentSkillBinding).where(AgentSkillBinding.agent_id == agent.id))).all())
    for row in [stale, catalog, *bindings]:
        await db_session.refresh(row)
    before = snapshot([stale, catalog, *bindings])
    payload = {"contract": {"requires": {"skills": [{"slug": "file_engine", "marketplace_id": "builtin:file_engine", "marketplace_source": "platform"}]}},
               "embedded": {"agents": [{"slug": "researcher", "skill_bindings": ["file_engine"]}]}, "recipe": {}}
    requirements = await sync_workspace_live_setup_requirements(db_session, workspace=ws, payload=payload)
    requirement = next(item for item in requirements if item["kind"] == "missing_skill")
    assert requirement["payload"]["installed_skill_id"] == (catalog.id if bound == "canonical_and_stale" else None)
    assert requirement["blocking"] is True
    assert snapshot([stale, catalog, *bindings]) == before
