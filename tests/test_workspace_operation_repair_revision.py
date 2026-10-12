"""Runtime repair must invalidate reviews whose deployment snapshot changed."""
import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.workspace_operation_service import (
    OperationConflictError,
    apply_operation_draft,
    create_operation_draft,
    repair_workspace_operation_runtime,
)


async def test_repair_invalidates_an_older_model_replacement_without_repeating_revision(db_session):
    db = db_session
    entity_id, workspace_id, agent_id = [generate_ulid() for _ in range(3)]
    workspace = Workspace(
        id=workspace_id, entity_id=entity_id, name="Legacy deployment", status="paused",
        operating_model={
            "services": [{"key": "example"}],
            "agent_mappings": [{"service_key": "example", "agent_id": agent_id}],
        },
    )
    db.add_all([workspace, Agent(id=agent_id, entity_id=entity_id, name="Example")])
    await db.flush()
    draft = await create_operation_draft(
        db, workspace_id, entity_id,
        initial_patches=[{
            "op": "operating_model.replace",
            "payload": {"operating_model": {
                "services": [{"key": "example"}], "agent_mappings": [],
                "integration_scope": {"disabled_providers": ["slack"]},
            }},
        }],
    )
    assert draft.validation["valid"] is True
    assert draft.base_revision == 0

    repaired = await repair_workspace_operation_runtime(db, workspace_id, entity_id)
    assert repaired["agent_mappings"]["created"] == 1
    assert repaired["workspace_revision"] == 1
    with pytest.raises(OperationConflictError, match="stale"):
        await apply_operation_draft(db, draft.id, entity_id, workspace_id, user_confirmation=True)
    subscription = (await db.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id,
    ))).scalar_one()
    assert subscription.status == "active"

    fresh = await create_operation_draft(
        db, workspace_id, entity_id,
        initial_patches=[{"op": "integration.remove", "payload": {"provider": "slack"}}],
    )
    repeated = await repair_workspace_operation_runtime(db, workspace_id, entity_id)
    assert repeated["agent_mappings"]["created"] == 0
    assert repeated["workspace_revision"] == 1
    result = await apply_operation_draft(
        db, fresh.id, entity_id, workspace_id, user_confirmation=True,
    )
    assert result["workspace_revision"] == 2
    assert subscription.status == "active"


async def test_repair_of_unchanged_configuration_keeps_open_drafts_valid(db_session):
    workspace = Workspace(
        entity_id=generate_ulid(), name="Already repaired", status="paused",
        operating_model={"rules": [], "agent_mappings": []}, operation_revision=4,
    )
    db_session.add(workspace)
    await db_session.flush()
    draft = await create_operation_draft(
        db_session, workspace.id, workspace.entity_id,
        initial_patches=[{"op": "integration.remove", "payload": {"provider": "slack"}}],
    )

    result = await repair_workspace_operation_runtime(db_session, workspace.id, workspace.entity_id)

    assert result["workspace_revision"] == draft.base_revision == 4
    await apply_operation_draft(
        db_session, draft.id, workspace.entity_id, workspace.id, user_confirmation=True,
    )
    assert workspace.operation_revision == 5
