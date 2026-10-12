"""Repair migration: declare the services installed Workspaces already deploy Agents to."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from packages.core.migrations.workspace_service_declarations import AUDIT_ACTION, apply
from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.user import Entity
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceOperationDraft
from packages.core.services import workspace_operation_service as operations

pytestmark = pytest.mark.asyncio

VIDEO_SERVICES = [
    {"key": "video_studio.direction", "name": "Video Director", "description": "Plans."},
    {"key": "video_studio.production", "name": "Video Producer", "description": "Produces."},
    {"key": "video_studio.quality", "name": "Video Quality Reviewer", "description": "Checks."},
]
PRODUCT_ROLES = {
    "product_video.exploration": "Product Explorer",
    "product_video.planning": "Product Video Planner",
    "product_video.capture": "Product Scene Collector",
    "product_video.production": "Product Video Producer",
    "product_video.quality": "Product Quality Reviewer",
}


async def _workspace(db, *, services, roles, blueprint_id=None):
    entity_id = generate_ulid()
    db.add(Entity(id=entity_id, name="Repair", plan_id="plan_enterprise"))
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Video Studio", status="active",
        operating_model={"services": copy.deepcopy(services), "context": "kept"},
        settings={"_blueprint": {"blueprint_id": blueprint_id}} if blueprint_id else {},
        operation_revision=2,
    )
    db.add(workspace)
    await db.flush()
    for key, name in roles:
        agent = Agent(id=generate_ulid(), entity_id=entity_id, name=f"Agent {name}" if name else "Planner Agent", status="active")
        db.add(agent)
        await db.flush()
        db.add(AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id, service_key=key,
            agent_id=agent.id, status="active", config={"service_name": name} if name else {},
        ))
    await db.flush()
    return workspace


async def _run(db):
    return await db.run_sync(lambda session: apply(session.connection()))


async def _reload(db, workspace_id):
    return (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id).execution_options(populate_existing=True)
    )).scalar_one()


async def _issues(db, workspace):
    current = await operations._snapshot_workspace_state(db, workspace)
    draft = WorkspaceOperationDraft(
        id=generate_ulid(), workspace_id=workspace.id, entity_id=workspace.entity_id,
        status="open", base_revision=int(workspace.operation_revision or 0),
        current_state=copy.deepcopy(current), patches=[],
    )
    return (await operations.validate_operation_draft(db, draft, current_state=current))["pre_existing_issues"]


async def test_repairs_the_r2_4_product_video_shape_and_is_idempotent(db_session):
    roles = [(service["key"], service["name"]) for service in VIDEO_SERVICES] + list(PRODUCT_ROLES.items())
    workspace = await _workspace(db_session, services=VIDEO_SERVICES, roles=roles)
    assert len(await _issues(db_session, workspace)) == 5

    result = await _run(db_session)
    mine = [op for op in result["operations"] if op["workspace_id"] == workspace.id]
    assert len(mine) == 1
    workspace = await _reload(db_session, workspace.id)
    services = workspace.operating_model["services"]
    # Existing declarations are kept exactly; only the missing ones are added.
    assert services[:3] == VIDEO_SERVICES
    assert {row["key"]: row["name"] for row in services[3:]} == PRODUCT_ROLES
    assert workspace.operating_model["context"] == "kept"
    # Open drafts built on the old state are now stale and cannot write it back.
    assert workspace.operation_revision == 3
    assert await _issues(db_session, workspace) == []
    receipt = (await db_session.execute(select(AuditLog).where(
        AuditLog.action == AUDIT_ACTION, AuditLog.resource_id == workspace.id,
    ))).scalar_one()
    assert sorted(row["key"] for row in receipt.details["added_services"]) == sorted(PRODUCT_ROLES)

    again = await _run(db_session)
    assert not [op for op in again["operations"] if op["workspace_id"] == workspace.id]
    workspace = await _reload(db_session, workspace.id)
    assert len(workspace.operating_model["services"]) == 8 and workspace.operation_revision == 3


async def test_prefers_the_installed_blueprint_declaration(db_session):
    blueprint = WorkspaceBlueprint(
        id=f"builtin:repair-{generate_ulid()}", slug="video-studio-v1", title="Video Studio",
        payload={"recipe": {"operating_model": {"services": [
            *VIDEO_SERVICES,
            {"key": "product_video.exploration", "name": "Product Explorer", "description": "Explores."},
        ]}}},
    )
    db_session.add(blueprint)
    await db_session.flush()
    workspace = await _workspace(
        db_session, services=VIDEO_SERVICES[:1], blueprint_id=blueprint.id,
        roles=[("video_studio.direction", "Video Director"),
               ("product_video.exploration", "Explorer (local label)"),
               ("product_video.planning", None)],
    )
    await _run(db_session)
    workspace = await _reload(db_session, workspace.id)
    added = workspace.operating_model["services"][1:]
    assert added == [
        {"key": "product_video.exploration", "name": "Product Explorer", "description": "Explores."},
        # Without a Blueprint entry or service_name, the deployed Agent's name is used.
        {"key": "product_video.planning", "name": "Planner Agent"},
    ]


async def test_skips_ambiguous_names_and_leaves_other_shapes_alone(db_session, caplog):
    ambiguous = await _workspace(
        db_session, services=VIDEO_SERVICES,
        roles=[("product_video.capture", "Scene Collector"), ("product_video.capture", "Capture Bot"),
               ("product_video.quality", "Product Quality Reviewer")],
    )
    undeclared = await _workspace(db_session, services=[], roles=[("product_video.capture", "Collector")])
    inactive = await _workspace(db_session, services=VIDEO_SERVICES, roles=[("product_video.capture", "Collector")])
    sub = (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == inactive.id))).scalar_one()
    sub.status = "inactive"
    await db_session.flush()

    result = await _run(db_session)
    [skip] = [item for item in result["skipped"] if item["workspace_id"] == ambiguous.id]
    assert skip["service_key"] == "product_video.capture"
    assert skip["names"] == ["Capture Bot", "Scene Collector"]
    assert "product_video.capture" in caplog.text
    ambiguous = await _reload(db_session, ambiguous.id)
    assert [row["key"] for row in ambiguous.operating_model["services"]][3:] == ["product_video.quality"]
    # A Workspace that declares no services validates no mapping against them.
    assert (await _reload(db_session, undeclared.id)).operating_model["services"] == []
    assert (await _reload(db_session, inactive.id)).operating_model["services"] == VIDEO_SERVICES


async def test_ignores_a_subscription_from_another_entity(db_session):
    workspace = await _workspace(db_session, services=VIDEO_SERVICES, roles=[("product_video.capture", "Collector")])
    sub = (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace.id))).scalar_one()
    foreign = generate_ulid()
    db_session.add(Entity(id=foreign, name="Other tenant", plan_id="plan_enterprise"))
    await db_session.flush()
    sub.entity_id = foreign
    await db_session.flush()
    await _run(db_session)
    assert (await _reload(db_session, workspace.id)).operating_model["services"] == VIDEO_SERVICES


async def test_shipped_blueprint_rows_are_inserted_in_blueprint_order(db_session):
    shipped = {"studio-v1": [
        {"key": "x.first", "name": "First", "description": "1"},
        {"key": "x.middle", "name": "Middle", "description": "2"},
        {"key": "x.last", "name": "Last", "description": "3"},
    ]}
    workspace = await _workspace(
        db_session, services=[shipped["studio-v1"][0], shipped["studio-v1"][2], {"key": "local", "name": "Mine"}],
        roles=[("x.middle", "Local label")], blueprint_id="builtin:studio-v1",
    )
    await db_session.run_sync(lambda session: apply(session.connection(), shipped=shipped))
    workspace = await _reload(db_session, workspace.id)
    assert workspace.operating_model["services"] == [
        shipped["studio-v1"][0], shipped["studio-v1"][1], shipped["studio-v1"][2], {"key": "local", "name": "Mine"},
    ]


VIDEO_STUDIO = (
    Path(__file__).resolve().parents[1] / "packages/core/blueprints/configs/video_studio/video-studio-v1.json"
)


async def test_a_repaired_video_studio_has_no_upgrade_conflict_on_its_services(db_session):
    """The repaired list is exactly what the corrected Blueprint declares."""
    if not VIDEO_STUDIO.exists():
        pytest.skip("Video Studio is not part of this distribution")
    from packages.core.blueprints.configuration_upgrade import capture_configuration, plan_configuration
    from packages.core.governance.approval_policy_service import account_defaults, initialize_workspace_approval

    new_payload = json.loads(VIDEO_STUDIO.read_text(encoding="utf-8"))
    old_payload = copy.deepcopy(new_payload)
    om = old_payload["recipe"]["operating_model"]
    om["services"] = [row for row in om["services"] if not row["key"].startswith("product_video.")]
    # The pre-fix install: three declared services, eight deployed roles.
    workspace = await _workspace(
        db_session, services=om["services"],
        roles=[(sub["service_key"], (sub.get("config") or {}).get("service_name"))
               for sub in old_payload["recipe"]["subscriptions"]],
        blueprint_id="builtin:video-studio-v1",
    )
    entity = await db_session.get(Entity, workspace.entity_id)
    entity.settings = {"approval_defaults": account_defaults()}
    await db_session.flush()
    await initialize_workspace_approval(db_session, workspace)
    baseline = await capture_configuration(db_session, workspace=workspace, payload=old_payload)

    await _run(db_session)
    workspace = await _reload(db_session, workspace.id)
    assert workspace.operating_model["services"] == new_payload["recipe"]["operating_model"]["services"]

    plan = await plan_configuration(db_session, workspace=workspace, payload=new_payload, baseline=baseline)
    [services] = [item for item in plan["items"] if item["key"] == "workspace_configuration:operating_model/services"]
    assert services["action"] == "unchanged" and not services["conflict"]
