import pytest
import json
from datetime import UTC, datetime, timedelta
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.workflow import WorkflowActionGrant, WorkflowProject
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_project_service import (
    WorkflowProjectConflict,
    WorkflowProjectNotFound,
    claim_workflow_project,
    create_workflow_project,
    get_workflow_project,
    list_workflow_projects,
    patch_workflow_project,
    lease_due_workflow_project,
    complete_workflow_project_lease,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "changed_owner", "verification_required", "verification_required_concurrent_update", "concurrent_update", "pre_handoff_concurrent_update", "legacy_handoff", "legacy_handoff_state_changed"])
async def test_native_retry_transfers_only_the_inherited_outstanding_lease(client, db_session, fault):
    from tests.test_workflows import _auth, _create_workspace
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "lease_retry")
    workspace_id = await _create_workspace(client, headers, "Maintenance queue")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    project = await create_workflow_project(
        db_session, entity_id=me["entity_id"], workspace_id=workspace_id,
        project_type="maintenance", state={"due_at": datetime.now(UTC).isoformat()},
        current_stage="pending", created_by=me["id"],
    )
    await db_session.commit()
    steps = [
        {"id": "start", "type": "trigger", "config": {}, "next": ["lease"]},
        {"id": "lease", "type": "workflow_project", "config": {
            "operation": "lease_due", "project_type": "maintenance", "output_var": "item",
            "lease_path": "work_lease", "recovery": "verification_required",
            "schema_version": 1, "state_schema": {"type": "object"},
            "due_path": "due_at", "pending_stages": ["pending"],
        }, "next": ["save"]},
        {"id": "save", "type": "workflow_project", "config": {
            "operation": "patch", "project_type": "maintenance", "output_var": "saved",
            "project_id": "{{item.project.project_id}}", "expected_revision": "{{item.project.revision}}",
            "patch": {"lease_owner_checkpoint": True}, "schema_version": 1,
            "state_schema": {"type": "object"},
        }, "next": ["snapshot"]},
        {"id": "snapshot", "type": "workflow_project", "config": {
            "operation": "get", "project_type": "maintenance", "output_var": "snapshot",
            "project_id": "{{saved.project_id}}", "schema_version": 1,
            "state_schema": {"type": "object"},
        }, "next": ["check"]},
        {"id": "check", "type": "condition", "config": {"expression": "proceed == true"},
         "true_next": ["checkpoint"], "false_next": ["blocked"]},
        {"id": "blocked", "type": "stop", "config": {"message": "offline", "retry_from_step_id": "check"}},
        {"id": "checkpoint", "type": "workflow_project", "config": {
            "operation": "patch", "project_type": "maintenance", "output_var": "checkpointed",
            "project_id": "{{snapshot.project_id}}", "expected_revision": "{{snapshot.revision}}",
            "patch": {"retry_checkpoint": True}, "schema_version": 1,
            "state_schema": {"type": "object"},
        }, "next": ["finish"]},
        {"id": "finish", "type": "workflow_project", "config": {
            "operation": "complete_lease", "project_type": "maintenance", "lease_path": "work_lease",
            "project_id": "{{item.project.project_id}}", "current_stage": "completed",
            "schema_version": 1, "state_schema": {"type": "object"},
        }, "next": ["end"]},
        {"id": "end", "type": "end", "config": {}},
    ]
    response = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Maintenance retry", "workspace_id": workspace_id,
        "steps": steps, "variables": {"proceed": False},
    })
    assert response.status_code == 201, response.text
    response = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": response.json()["id"], "workspace_id": workspace_id,
    })
    assert response.status_code == 201, response.text
    response = await client.post(f"/api/v1/workflows/bindings/{response.json()['id']}/run", headers=headers,
                                 json={"execute": False})
    assert response.status_code == 201, response.text
    first_id = response.json()["id"]
    await WorkflowRunner().run(first_id)
    await db_session.refresh(project)
    assert project.current_stage == "leased"
    assert project.state["lease_owner_checkpoint"] is True
    if fault == "changed_owner":
        project.state = {**project.state, "work_lease": {**project.state["work_lease"], "run_id": "another-run"}}
    elif fault == "verification_required":
        project.current_stage = "verification_required"
        project.revision += 1
    elif fault == "verification_required_concurrent_update":
        project.current_stage = "verification_required"
        project.revision += 1
        project.state = {**project.state, "unproven_verification_change": True}
    elif fault == "pre_handoff_concurrent_update":
        await patch_workflow_project(
            db_session,
            project_id=project.id,
            entity_id=me["entity_id"],
            workspace_id=workspace_id,
            expected_revision=project.revision,
            state={**project.state, "pre_handoff_concurrent_update": True},
        )
    await db_session.commit()
    first = await db_session.get(WorkflowRun, first_id)
    await db_session.refresh(first)
    stale_saved_projection = first.variables["saved"]
    response = await client.post(f"/api/v1/workflows/runs/{first_id}/retry", headers=headers,
                                 json={"execute": False, "variables": {"proceed": fault not in {"legacy_handoff", "legacy_handoff_state_changed"}}})
    if fault in {"changed_owner", "verification_required_concurrent_update"}:
        assert response.status_code == 400, response.text
        assert "lease changed" in response.text
        return
    assert response.status_code == 201, response.text
    retry_id = response.json()["id"]
    await db_session.refresh(project)
    assert project.state["work_lease"]["run_id"] == retry_id
    if fault == "pre_handoff_concurrent_update":
        assert response.json()["variables"]["item"]["project"]["revision"] < project.revision
    else:
        assert response.json()["variables"]["item"]["project"]["revision"] == project.revision
    if fault == "pre_handoff_concurrent_update":
        assert response.json()["variables"]["snapshot"]["revision"] < project.revision
    else:
        assert response.json()["variables"]["snapshot"]["revision"] == project.revision
    with pytest.raises(PermissionError):
        await complete_workflow_project_lease(
            db_session, project_id=project.id, entity_id=me["entity_id"], workspace_id=workspace_id,
            run_id=first_id, stage="completed", lease_path="work_lease",
        )
    await db_session.rollback()
    if fault in {"legacy_handoff", "legacy_handoff_state_changed"}:
        retry = await db_session.get(WorkflowRun, retry_id)
        if fault == "legacy_handoff_state_changed":
            stale_saved_projection = {
                **stale_saved_projection,
                "state": {**stale_saved_projection["state"], "unproven_change": True},
            }
        retry.variables = {**retry.variables, "legacy_saved": stale_saved_projection}
        await db_session.commit()
        await WorkflowRunner().run(retry_id)
        await db_session.refresh(retry)
        assert retry.status == "failed"
        assert retry.current_step_id == "blocked"
        response = await client.post(f"/api/v1/workflows/runs/{retry_id}/retry", headers=headers,
                                     json={"execute": False, "variables": {"proceed": True}})
        assert response.status_code == 201, response.text
        retry_id = response.json()["id"]
        await db_session.refresh(project)
        legacy_saved = response.json()["variables"]["legacy_saved"]
        if fault == "legacy_handoff_state_changed":
            assert legacy_saved["revision"] < project.revision
        else:
            assert legacy_saved["revision"] == project.revision, json.dumps({
                key: legacy_saved.get(key)
                for key in ("project_id", "revision", "last_run_id", "project_type", "schema_version", "current_stage")
            })
    if fault in {"concurrent_update", "pre_handoff_concurrent_update"}:
        if fault == "concurrent_update":
            await db_session.refresh(project)
            await patch_workflow_project(
                db_session,
                project_id=project.id,
                entity_id=me["entity_id"],
                workspace_id=workspace_id,
                expected_revision=project.revision,
                state={**project.state, "concurrent_update": True},
            )
            await db_session.commit()
        await WorkflowRunner().run(retry_id)
        retry = await db_session.get(WorkflowRun, retry_id)
        assert retry.status == "failed"
        assert retry.current_step_id == "checkpoint"
        assert retry.step_results["checkpoint"]["code"] == "workflow_project_conflict"
        return
    await WorkflowRunner().run(retry_id)
    await db_session.refresh(project)
    assert project.current_stage == "completed"
    assert project.state["work_lease"] == {}
    assert project.state["retry_checkpoint"] is True
    retry = await db_session.get(WorkflowRun, retry_id)
    assert retry.status == "completed"
    retry.status, retry.current_step_id = "failed", "end"
    await db_session.commit()
    # A later failure must not try to reacquire a lease already completed by
    # an inherited node, or repeat the completed work.
    response = await client.post(f"/api/v1/workflows/runs/{retry_id}/retry", headers=headers,
                                 json={"execute": False, "from_step_id": "end"})
    assert response.status_code == 201, response.text


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
    assert project.project_key is None


@pytest.mark.asyncio
async def test_applied_review_reclaims_released_report_lease_with_current_version(client, db_session, monkeypatch):
    from tests.test_workflows import _auth, _create_workspace
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "released_report")
    workspace_id = await _create_workspace(client, headers, "Client reports")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    project = await create_workflow_project(db_session, entity_id=me["entity_id"], workspace_id=workspace_id,
        project_type="client_report", state={"due_at": datetime.now(UTC).isoformat(), "recipient": "client-A"},
        current_stage="pending", created_by=me["id"])
    await db_session.commit()
    config = {"project_type": "client_report", "schema_version": 1, "state_schema": {"type": "object"}}

    async def create_run(name, steps):
        response = await client.post("/api/v1/workflows", headers=headers,
            json={"name": name, "workspace_id": workspace_id, "steps": steps})
        assert response.status_code == 201, response.text
        binding = await client.post("/api/v1/workflows/bindings", headers=headers,
            json={"workflow_id": response.json()["id"], "workspace_id": workspace_id})
        assert binding.status_code == 201, binding.text
        response = await client.post(f"/api/v1/workflows/bindings/{binding.json()['id']}/run", headers=headers,
            json={"execute": False})
        assert response.status_code == 201, response.text
        await WorkflowRunner().run(response.json()["id"])
        return await db_session.get(WorkflowRun, response.json()["id"])

    async def unavailable_after_delivery(*args, **kwargs):
        return {"status": "failed", "error": "Provider response lost", "effect_state": "unconfirmed", "do_not_replay": True}

    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", unavailable_after_delivery)
    prior = await create_run("Deliver report", [
        {"id": "start", "type": "trigger", "next": ["lease"]},
        {"id": "lease", "type": "workflow_project", "config": {**config, "operation": "lease_due",
            "due_path": "due_at", "pending_stages": ["pending"],
            "lease_path": "delivery_lease", "output_var": "item", "recovery": "verification_required"}, "next": ["deliver"]},
        {"id": "deliver", "type": "agent", "config": {"prompt": "Deliver report", "output_var": "delivery",
            "output_schema": {"type": "object", "required": ["receipt_id"],
                "properties": {"receipt_id": {"type": "string", "minLength": 1}}}}, "next": ["save"]},
        {"id": "save", "type": "workflow_project", "config": {**config, "operation": "patch",
            "project_id": "{{item.project.project_id}}", "expected_revision": "{{item.project.revision}}",
            "patch": {"delivery": "{{delivery}}"}}, "next": ["finish"]},
        {"id": "finish", "type": "workflow_project", "config": {**config, "operation": "complete_lease",
            "project_id": "{{item.project.project_id}}", "lease_path": "delivery_lease", "current_stage": "delivered"}, "next": ["end"]},
        {"id": "end", "type": "end"},
    ])
    assert prior.status == "failed" and prior.current_step_id == "deliver"
    original_results = json.loads(json.dumps(prior.step_results))
    maintenance = await create_run("Record recovery check", [
        {"id": "start", "type": "trigger", "next": ["load"]},
        {"id": "load", "type": "workflow_project", "config": {**config, "operation": "get",
            "project_id": project.id, "output_var": "current"}, "next": ["save"]},
        {"id": "save", "type": "workflow_project", "config": {**config, "operation": "patch",
            "project_id": project.id, "expected_revision": "{{current.revision}}", "current_stage": "verification_required",
            "patch": {"check_note": "Needs operator verification"}}, "next": ["claim"]},
        {"id": "claim", "type": "workflow_project", "config": {**config, "operation": "lease_due",
            "lease_path": "delivery_lease", "due_path": "due_at", "pending_stages": ["verification_required"],
            "recovery": "verification_required"}, "next": ["release"]},
        {"id": "release", "type": "workflow_project", "config": {**config, "operation": "complete_lease",
            "project_id": project.id, "lease_path": "delivery_lease", "current_stage": "verification_required"}, "next": ["end"]},
        {"id": "end", "type": "end"},
    ])
    assert maintenance.status == "completed"
    path = f"/api/v1/workflows/runs/{prior.id}"
    action = (await client.get(path + "?detail=false", headers=headers)).json()["intervention"]
    await db_session.refresh(project)
    assert action["project_revisions"] == {project.id: project.revision}
    review = {"step_id": "deliver", "decision": "applied", "effect_fingerprint": action["effect_fingerprint"],
        "confirmed": True, "note": "Verified the existing report delivery receipt for client-A.",
        "result": {"receipt_id": "report-42"}, "project_revisions": action["project_revisions"]}
    for invalid in ({}, {project.id: project.revision - 1}):
        response = await client.post(path + "/retry", headers=headers,
            json={"execute": False, "effect_review": {**review, "project_revisions": invalid}})
        assert response.status_code == 409 and "changed since review" in response.text
    original_state = dict(project.state)
    # Changed business values and a new lease owner cannot be accepted even
    # with a previously valid review. No transfer/new attempt may be committed.
    for patch in ({"recipient": "client-B"}, {"delivery_lease": {"run_id": "other-run"}}):
        project.state = {**original_state, **patch}
        await db_session.commit()
        response = await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review})
        assert response.status_code == 409 and "changed since review" in response.text
    project.state = original_state
    await db_session.commit()
    response = await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review})
    assert response.status_code == 201, response.text
    retry_id = response.json()["id"]
    await db_session.refresh(project)
    assert project.state["delivery_lease"]["run_id"] == retry_id

    async def no_second_delivery(*args, **kwargs):
        pytest.fail("A reconciled report must not be sent twice")

    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", no_second_delivery)
    await WorkflowRunner().run(retry_id)
    retried = await db_session.get(WorkflowRun, retry_id)
    assert retried.status == "completed", retried.error
    await db_session.refresh(project)
    assert project.current_stage == "delivered" and project.state["delivery_lease"] == {}
    assert project.state["delivery"] == review["result"]
    assert project.state["check_note"] == "Needs operator verification"
    await db_session.refresh(prior)
    assert prior.status == "failed" and prior.step_results == original_results


@pytest.mark.asyncio
async def test_claim_workflow_project_dedupes_one_workspace_business_key(db_session):
    first, first_claimed = await claim_workflow_project(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        project_type="daily_content_batch",
        project_key="2026-08-03",
        state={"status": "planning"},
        created_by="user1",
        last_run_id="run1",
    )
    await db_session.commit()

    duplicate, duplicate_claimed = await claim_workflow_project(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        project_type="daily_content_batch",
        project_key="2026-08-03",
        state={"status": "duplicate"},
        created_by="user1",
        last_run_id="run2",
    )

    assert first_claimed is True
    assert duplicate_claimed is False
    assert duplicate.id == first.id
    assert duplicate.state == {"status": "planning"}
    assert duplicate.last_run_id == "run1"


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
            db_session, project_id=project.id, entity_id="ent1", workspace_id="ws2",
            expected_revision=0, state={"hidden": False},
        )


@pytest.mark.asyncio
async def test_due_lease_is_reusable_and_run_scoped(db_session):
    now = datetime.now(UTC)
    for project_type, key in (("content_slot", "content"), ("maintenance_slot", "maintenance")):
        await create_workflow_project(
            db_session, entity_id="ent1", workspace_id="ws1", project_type=project_type,
            project_key=key, state={"parent_project_id": "parent", "due": (now - timedelta(minutes=1)).isoformat(),
                "execute_at": (now + timedelta(hours=1)).isoformat()},
            created_by="user1", current_stage="pending",
        )
    await db_session.commit()
    for project_type in ("content_slot", "maintenance_slot"):
        leased = await lease_due_workflow_project(
            db_session, entity_id="ent1", workspace_id="ws1", project_type=project_type,
            run_id="run1", parent_id="parent", due_path="due", pending_stages={"pending"},
        )
        assert leased is not None and leased.current_stage == "leased"
        async with AsyncSession(bind=db_session.bind) as competitor:
            assert await lease_due_workflow_project(
                competitor, entity_id="ent1", workspace_id="ws1", project_type=project_type,
                run_id="competing-run", parent_id="parent", due_path="due", pending_stages={"pending"},
            ) is None
        # A second phase cannot recover a lease owned by the first phase.
        assert await lease_due_workflow_project(
            db_session, entity_id="ent1", workspace_id="ws1", project_type=project_type,
            run_id="publish-run", parent_id="parent", due_path="execute_at",
            pending_stages={"ready"}, now=now + timedelta(hours=2),
        ) is None
        with pytest.raises(PermissionError):
            await complete_workflow_project_lease(
                db_session, project_id=leased.id, entity_id="ent1", workspace_id="ws1",
                run_id="other-run", stage="completed",
            )
        completed = await complete_workflow_project_lease(
            db_session, project_id=leased.id, entity_id="ent1", workspace_id="ws1",
            run_id="run1", stage="ready", patch={"artifact": "prepared"},
        )
        assert completed.current_stage == "ready"
        assert completed.state["artifact"] == "prepared"
        assert await lease_due_workflow_project(
            db_session, entity_id="ent1", workspace_id="ws1", project_type=project_type,
            run_id="publish-run", parent_id="parent", due_path="execute_at",
            pending_stages={"ready"}, now=now,
        ) is None
        executing = await lease_due_workflow_project(
            db_session, entity_id="ent1", workspace_id="ws1", project_type=project_type,
            run_id="publish-run", parent_id="parent", due_path="execute_at",
            pending_stages={"ready"}, now=now + timedelta(hours=1),
        )
        assert executing.id == leased.id
        with pytest.raises(PermissionError):
            await complete_workflow_project_lease(
                db_session, project_id=leased.id, entity_id="ent1", workspace_id="ws1",
                run_id="run1", stage="completed",
            )


@pytest.mark.asyncio
async def test_due_lease_filters_parent_and_preserves_active_external_effect(db_session):
    now = datetime.now(UTC)
    for index in range(100):
        await create_workflow_project(
            db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
            project_key=f"other-{index}", state={"parent_project_id": "other", "due": (now - timedelta(minutes=1)).isoformat()},
            created_by="user1", current_stage="pending",
        )
        await create_workflow_project(
            db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
            project_key=f"future-{index}", state={"parent_project_id": "wanted", "due": (now + timedelta(days=1)).isoformat()},
            created_by="user1", current_stage="pending",
        )
    target = await create_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        project_key="target", state={"parent_project_id": "wanted", "due": (now - timedelta(minutes=1)).isoformat()},
        created_by="user1", current_stage="pending",
    )
    active = WorkflowRun(id="active-run", workflow_id="wf", entity_id="ent1", workspace_id="ws1", status="paused")
    db_session.add(active)
    await db_session.flush()
    page, cursor = await list_workflow_projects(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        parent_id="wanted", limit=100,
    )
    assert len(page) == 100 and cursor
    tail, last_cursor = await list_workflow_projects(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        parent_id="wanted", limit=100, after_id=cursor,
    )
    assert len(tail) == 1 and last_cursor is None
    # ULIDs minted in one millisecond need not sort in creation order, so the
    # target is not necessarily last: check both pages as one ordered listing.
    listed = [row.id for row in [*page, *tail]]
    assert listed == sorted(listed) and len(set(listed)) == 101 and target.id in listed
    leased = await lease_due_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        run_id="active-run", parent_id="wanted", due_path="due", pending_stages={"pending"}, limit=1,
    )
    assert leased is not None and leased.id == target.id
    leased.state = {**leased.state, "lease": {**leased.state["lease"], "expires_at": (now - timedelta(minutes=1)).isoformat()}}
    await db_session.flush()
    assert await lease_due_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        run_id="new-run", parent_id="wanted", due_path="due", pending_stages={"pending"}, recovery="verification_required",
    ) is None
    await db_session.refresh(leased)
    assert leased.current_stage == "leased"
    assert await lease_due_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        run_id="recovery-run", parent_id="wanted", due_path="due",
        pending_stages={"pending", "verification_required"}, recovery="verification_required",
    ) is None
    await db_session.refresh(leased)
    assert leased.current_stage == "leased" and leased.state["lease"]["run_id"] == active.id
    await db_session.execute(update(WorkflowRun).where(WorkflowRun.id == active.id).values(status="failed"))
    await db_session.flush()
    assert await lease_due_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        run_id="new-run", parent_id="wanted", due_path="due", pending_stages={"pending"}, recovery="verification_required",
    ) is None
    await db_session.refresh(leased)
    assert leased.current_stage == "verification_required"
    assert leased.state["lease"]["run_id"] == active.id
    assert leased.state["lease"]["pending_stage"] == "pending"
    recovered = await lease_due_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="maintenance_slot",
        run_id="recovery-run", parent_id="wanted", due_path="due",
        pending_stages={"pending", "verification_required"}, recovery="verification_required",
    )
    assert recovered is not None and recovered.id == leased.id
    assert recovered.current_stage == "leased" and recovered.last_run_id == "recovery-run"
    assert recovered.state["lease"]["run_id"] == "recovery-run"
    assert recovered.state["lease"]["pending_stage"] == "verification_required"
    with pytest.raises(PermissionError):
        await complete_workflow_project_lease(
            db_session, project_id=leased.id, entity_id="ent1", workspace_id="ws1",
            run_id="active-run", stage="completed",
        )


async def test_project_query_tool_paginates_only_authorized_workspace(db_session):
    from packages.core.ai.tools.workflow_tools import _query_workflow_projects
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace, WorkspaceStaff

    entity_id, user_id = generate_ulid(), generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="Maintenance projects")
    foreign = Workspace(entity_id=entity_id, name="Private projects")
    db_session.add_all([
        Entity(id=entity_id, name="Project query"), workspace, foreign,
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.com", password_hash="unused", role="member", status="active"),
    ])
    await db_session.flush()
    db_session.add(WorkspaceStaff(workspace_id=workspace.id, user_id=user_id, role="viewer", status="active"))
    for owner in (workspace, workspace, foreign):
        await create_workflow_project(
            db_session, entity_id=entity_id, workspace_id=owner.id, project_type="maintenance",
            state={"parent_project_id": "plan"}, created_by=user_id,
        )
    await db_session.commit()
    args = dict(entity_id=entity_id, workspace_id=workspace.id, project_type="maintenance", parent_id="plan", limit=1)
    assert json.loads(await _query_workflow_projects(**args)).get("error")
    args["_user_id_from_context"] = user_id
    first = json.loads(await _query_workflow_projects(**args))
    second = json.loads(await _query_workflow_projects(**args, after_id=first["next_cursor"]))
    assert first["has_more"] and not second["has_more"]
    assert len(first["projects"]) == len(second["projects"]) == 1
    assert first["projects"][0]["project_id"] != second["projects"][0]["project_id"]
    assert json.loads(await _query_workflow_projects(**{**args, "workspace_id": foreign.id})).get("error")
    workspace.status = "paused"
    await db_session.commit()
    assert json.loads(await _query_workflow_projects(**args))["projects"] == first["projects"]
    workspace.status = "needs_setup"
    await db_session.commit()
    assert json.loads(await _query_workflow_projects(**args)).get("error")


@pytest.mark.asyncio
@pytest.mark.parametrize("project_type", ["course_delivery", "invoice"])
async def test_project_query_tool_projects_requested_nested_state_paths(db_session, project_type):
    from packages.core.ai.tools.workflow_tools import _query_workflow_projects
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace, WorkspaceStaff

    entity_id, user_id = generate_ulid(), generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="Projected project state")
    foreign = Workspace(entity_id=entity_id, name="Foreign project state")
    db_session.add_all([
        Entity(id=entity_id, name="Projected query"), workspace, foreign,
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.com", password_hash="unused", role="member", status="active"),
    ])
    await db_session.flush()
    db_session.add(WorkspaceStaff(workspace_id=workspace.id, user_id=user_id, role="viewer", status="active"))
    state = {
        "channel": {"platform": "youtube", "channel_id": None, "private_evidence": {"large": "x" * 2048}},
        "production": {"publication": {"title": "Release title", "description": "Long description"}},
        "work": {"title": "Lesson", "version_label": "v2", "creative_evidence": {"large": "x" * 2048}},
        "publication": {"video_id": "video-1"},
        "due_at": None,
        "parent_project_id": "parent-1",
        "publication_blocker": None,
    }
    project = await create_workflow_project(
        db_session, entity_id=entity_id, workspace_id=workspace.id, project_type=project_type,
        state=state, created_by=user_id,
    )
    await create_workflow_project(
        db_session, entity_id=entity_id, workspace_id=foreign.id, project_type=project_type,
        state={"publication": {"video_id": "foreign"}}, created_by=user_id,
    )
    run = WorkflowRun(entity_id=entity_id, workspace_id=workspace.id, workflow_id="definition",
                      status="failed", current_step_id="publish", error="failure: " + "x" * 1200)
    db_session.add(run)
    await db_session.flush()
    project.last_run_id = run.id
    await db_session.commit()
    args = {
        "entity_id": entity_id, "workspace_id": workspace.id, "_user_id_from_context": user_id,
        "project_type": project_type,
        "state_paths": [
            "channel.platform", "channel.channel_id", "production.publication.title",
            "work.title", "work.version_label", "publication", "due_at", "parent_project_id",
            "publication_blocker", "missing.path",
        ],
    }
    result = json.loads(await _query_workflow_projects(**args))
    assert result["projects"][0]["state"] == {
        "channel": {"platform": "youtube", "channel_id": None},
        "production": {"publication": {"title": "Release title"}},
        "work": {"title": "Lesson", "version_label": "v2"},
        "publication": {"video_id": "video-1"},
        "due_at": None,
        "parent_project_id": "parent-1",
        "publication_blocker": None,
    }
    full_state = json.loads(await _query_workflow_projects(**{key: value for key, value in args.items() if key != "state_paths"}))
    assert full_state["projects"][0]["state"] == state
    execution = json.loads(await _query_workflow_projects(**{**args, "include_execution": True}))
    assert execution["projects"][0]["execution"] == {
        "run_id": run.id, "status": "failed", "current_step_id": "publish", "error": "failure: " + "x" * 991,
    }
    assert json.loads(await _query_workflow_projects(**{**args, "workspace_id": foreign.id})).get("error")
    for state_paths in ([], ["publication..video_id"], ["publication[0]"], [".publication"], ["publication."]):
        assert json.loads(await _query_workflow_projects(**{**args, "state_paths": state_paths})).get("error")
