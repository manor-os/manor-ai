import pytest

from packages.core.models.workflow import WorkflowActionGrant, WorkflowProject
from packages.core.services.workflow_project_service import (
    WorkflowProjectConflict,
    WorkflowProjectNotFound,
    create_workflow_project,
    get_workflow_project,
    patch_workflow_project,
)


def test_workflow_project_defaults():
    project = WorkflowProject(
        id="proj1",
        entity_id="ent1",
        workspace_id="ws1",
        project_type="product_video",
        state={"request": {}},
        created_by="user1",
    )

    assert WorkflowProject.__table__.c.schema_version.default.arg == 1
    assert WorkflowProject.__table__.c.current_stage.default.arg == "draft"
    assert WorkflowProject.__table__.c.revision.default.arg == 0
    assert project.last_run_id is None


def test_workflow_action_grant_fields():
    grant = WorkflowActionGrant(
        id="grant1",
        entity_id="ent1",
        workspace_id="ws1",
        workflow_run_id="run1",
        project_id="proj1",
        grant_type="browser_capture",
        scope={"scene_ids": ["scene-1"]},
        granted_by="user1",
    )

    assert grant.scope == {"scene_ids": ["scene-1"]}
    assert grant.revoked_at is None


@pytest.mark.asyncio
async def test_create_and_get_workflow_project(db_session):
    project = await create_workflow_project(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        project_type="product_video",
        state={"request": {}, "scenes": []},
        created_by="user1",
        last_run_id="run1",
    )
    await db_session.commit()

    loaded = await get_workflow_project(
        db_session,
        project_id=project.id,
        entity_id="ent1",
    )

    assert loaded.id == project.id
    assert loaded.current_stage == "draft"
    assert loaded.revision == 0
    assert loaded.state == {"request": {}, "scenes": []}
    assert loaded.last_run_id == "run1"


@pytest.mark.asyncio
async def test_get_workflow_project_is_entity_scoped(db_session):
    project = await create_workflow_project(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        project_type="product_video",
        state={},
        created_by="user1",
    )
    await db_session.commit()

    with pytest.raises(WorkflowProjectNotFound):
        await get_workflow_project(
            db_session,
            project_id=project.id,
            entity_id="ent2",
        )

    with pytest.raises(WorkflowProjectNotFound):
        await get_workflow_project(
            db_session,
            project_id=project.id,
            entity_id="ent1",
            workspace_id="ws2",
        )


@pytest.mark.asyncio
async def test_patch_workflow_project_rejects_stale_revision(db_session):
    project = await create_workflow_project(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        project_type="product_video",
        state={"request": {}, "scenes": []},
        created_by="user1",
    )
    await db_session.commit()

    updated = await patch_workflow_project(
        db_session,
        project_id=project.id,
        entity_id="ent1",
        expected_revision=0,
        state={"request": {}, "scenes": [], "plan": {}},
        current_stage="discovering",
        last_run_id="run2",
    )
    await db_session.commit()

    assert updated.revision == 1
    assert updated.current_stage == "discovering"
    assert updated.last_run_id == "run2"
    assert updated.state["plan"] == {}

    with pytest.raises(WorkflowProjectConflict):
        await patch_workflow_project(
            db_session,
            project_id=project.id,
            entity_id="ent1",
            expected_revision=0,
            state={"request": {"stale": True}},
        )


@pytest.mark.asyncio
async def test_patch_workflow_project_hides_other_entities(db_session):
    project = await create_workflow_project(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        project_type="product_video",
        state={},
        created_by="user1",
    )
    await db_session.commit()

    with pytest.raises(WorkflowProjectNotFound):
        await patch_workflow_project(
            db_session,
            project_id=project.id,
            entity_id="ent2",
            expected_revision=0,
            state={"hidden": False},
        )

    with pytest.raises(WorkflowProjectNotFound):
        await patch_workflow_project(
            db_session,
            project_id=project.id,
            entity_id="ent1",
            workspace_id="ws2",
            expected_revision=0,
            state={"hidden": False},
        )
