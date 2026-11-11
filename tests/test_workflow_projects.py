import pytest
import json
from datetime import UTC, datetime, timedelta
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.workflow import WorkflowActionGrant, WorkflowProject
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_project_service import (
    WorkflowProjectConflict,
    WorkflowProjectInUse,
    WorkflowProjectNotFound,
    claim_workflow_project,
    create_workflow_project,
    delete_workflow_project,
    get_workflow_project,
    list_workflow_projects,
    patch_workflow_project,
    lease_due_workflow_project,
    complete_workflow_project_lease,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "changed_owner", "verification_required", "verification_required_concurrent_update", "concurrent_update", "pre_handoff_concurrent_update", "legacy_handoff", "legacy_handoff_state_changed"])
async def test_native_retry_transfers_only_the_inherited_outstanding_lease(client, db_session, fault, monkeypatch):
    from tests.test_workflows import _auth, _create_workspace
    from packages.core.ai.workflow_runner import WorkflowRunner

    async def keep_failed_run_leases(run, db) -> None:
        """A run that failed before failures released their leases still holds it; the retry takes it over."""

    monkeypatch.setattr(WorkflowRunner, "_release_failed_run_leases", staticmethod(keep_failed_run_leases))
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


async def _scheduled_period_run(client, db_session, *, label: str):
    """A run that leases one due period, then stops (fails) unless its variables say proceed."""
    from tests.test_workflows import _auth, _create_workspace
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, label)
    workspace_id = await _create_workspace(client, headers, f"{label} periods")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    project = await create_workflow_project(
        db_session, entity_id=me["entity_id"], workspace_id=workspace_id, project_type="period",
        state={"due_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
        current_stage="scheduled", created_by=me["id"],
    )
    await db_session.commit()
    steps = [
        {"id": "start", "type": "trigger", "config": {}, "next": ["lease"]},
        {"id": "lease", "type": "workflow_project", "config": {
            "operation": "lease_due", "project_type": "period", "output_var": "period",
            "lease_path": "production", "recovery": "reclaim", "schema_version": 1,
            "state_schema": {"type": "object"}, "due_path": "due_at", "pending_stages": ["scheduled"],
        }, "next": ["check"]},
        {"id": "check", "type": "condition", "config": {"expression": "proceed == true"},
         "true_next": ["finish"], "false_next": ["blocked"]},
        {"id": "blocked", "type": "stop", "config": {"message": "offline", "retry_from_step_id": "check"}},
        {"id": "finish", "type": "workflow_project", "config": {
            "operation": "complete_lease", "project_type": "period", "lease_path": "production",
            "project_id": "{{period.project.project_id}}", "current_stage": "completed",
            "schema_version": 1, "state_schema": {"type": "object"},
        }, "next": ["end"]},
        {"id": "end", "type": "end", "config": {}},
    ]
    response = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Period lease", "workspace_id": workspace_id,
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
    run_id = response.json()["id"]
    await WorkflowRunner().run(run_id)
    run = await db_session.get(WorkflowRun, run_id)
    await db_session.refresh(run)
    assert run.status == "failed"
    await db_session.refresh(project)
    return headers, me, workspace_id, project, run_id


@pytest.mark.asyncio
async def test_failed_run_releases_its_period_lease_to_the_stage_it_waited_in(client, db_session):
    from packages.core.services.workflow_project_service import release_failed_run_project_leases

    _, me, workspace_id, project, run_id = await _scheduled_period_run(client, db_session, label="lease_release")
    assert project.current_stage == "scheduled"
    assert project.state["production"] == {}
    assert project.last_run_id == run_id
    assert project.revision == 2
    # Releasing again, for a run that no longer holds the lease, changes nothing.
    assert await release_failed_run_project_leases(
        db_session, run_id=run_id, entity_id=me["entity_id"], workspace_id=workspace_id, lease_paths={"production"},
    ) == []
    await db_session.refresh(project)
    assert project.revision == 2


@pytest.mark.asyncio
async def test_a_second_run_can_lease_a_period_whose_failed_lease_was_released(client, db_session):
    _, me, workspace_id, project, _ = await _scheduled_period_run(client, db_session, label="lease_relet")
    second = await lease_due_workflow_project(
        db_session, entity_id=me["entity_id"], workspace_id=workspace_id, project_type="period",
        run_id="second-run", due_path="due_at", lease_path="production", pending_stages={"scheduled"},
        recovery="reclaim",
    )
    await db_session.commit()
    assert second is not None and second.id == project.id
    assert second.current_stage == "leased"
    assert second.state["production"]["run_id"] == "second-run"
    assert second.state["production"]["pending_stage"] == "scheduled"


@pytest.mark.asyncio
async def test_retry_of_a_failed_period_run_leases_the_released_project_again(client, db_session):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers, _, _, project, run_id = await _scheduled_period_run(client, db_session, label="lease_retry_after")
    response = await client.post(f"/api/v1/workflows/runs/{run_id}/retry", headers=headers,
                                 json={"execute": False, "variables": {"proceed": True}})
    assert response.status_code == 201, response.text
    retry_id = response.json()["id"]
    await db_session.refresh(project)
    assert project.current_stage == "leased"
    assert project.state["production"]["run_id"] == retry_id
    assert project.state["production"]["pending_stage"] == "scheduled"
    await WorkflowRunner().run(retry_id)
    await db_session.refresh(project)
    assert project.current_stage == "completed"
    assert project.state["production"] == {}
    retry = await db_session.get(WorkflowRun, retry_id)
    await db_session.refresh(retry)
    assert retry.status == "completed"


@pytest.mark.asyncio
async def test_retry_refuses_a_released_period_that_another_run_has_leased(client, db_session):
    from packages.core.services.workflow_project_service import lease_due_workflow_project as lease_due

    headers, me, workspace_id, _, run_id = await _scheduled_period_run(client, db_session, label="lease_retry_refused")
    await lease_due(
        db_session, entity_id=me["entity_id"], workspace_id=workspace_id, project_type="period",
        run_id="second-run", due_path="due_at", lease_path="production", pending_stages={"scheduled"},
        recovery="reclaim",
    )
    await db_session.commit()
    response = await client.post(f"/api/v1/workflows/runs/{run_id}/retry", headers=headers,
                                 json={"execute": False, "variables": {"proceed": True}})
    assert response.status_code == 400, response.text
    assert "lease changed" in response.text


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
async def test_a_key_longer_than_the_column_still_names_one_project(db_session):
    """Local eval 2026-11-03: a reply flow keyed its project by the whole customer email and the run failed on the column."""
    email = ("Hi, my candle arrived cracked. " * 20).strip()
    project = await create_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="customer_reply",
        project_key=email, state={}, created_by="user1",
    )
    await db_session.commit()
    assert len(project.project_key) == 200 and project.project_key.startswith("Hi, my candle arrived cracked.")
    # The same email names the same project; a different one past the cut does not.
    again, claimed = await claim_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="customer_reply",
        project_key=email, state={}, created_by="user1",
    )
    assert (again.id, claimed) == (project.id, False)
    other, claimed = await claim_workflow_project(
        db_session, entity_id="ent1", workspace_id="ws1", project_type="customer_reply",
        project_key=email + "PS: order 1182.", state={}, created_by="user1",
    )
    assert claimed is True and other.id != project.id


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


async def _keyed_project_runner(client, db_session, user: str, project: dict, outputs: list[str],
                                then: dict | None = None, between: tuple[dict, ...] = ()):
    """Bind a manual Workflow whose first node writes a project keyed by run inputs.

    ``between`` are steps run after it, then ``then``, the config of a second
    project node, ``save``.
    """
    from tests.test_workflows import _auth, _create_workspace
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, user)
    workspace_id = await _create_workspace(client, headers, user)
    chain = [
        {"id": "project", "type": "workflow_project", "config": {
            "schema_version": 1, "state_schema": {"type": "object"}, **project}},
        *between,
        *([{"id": "save", "type": "workflow_project", "config": {
            "schema_version": 1, "state_schema": {"type": "object"}, **then}}] if then else []),
        {"id": "done", "type": "end"},
    ]
    response = await client.post("/api/v1/workflows", headers=headers, json={
        "name": user, "workspace_id": workspace_id, "steps": [
            {"id": "start", "type": "trigger", "next": ["project"], "config": {"outputs": [
                {"key": key, "type": "text", "value": f"{{{{start.{key}}}}}"} for key in outputs]}},
            *({**step, "next": [after["id"]]} for step, after in zip(chain, chain[1:])),
            chain[-1],
        ]})
    assert response.status_code == 201, response.text
    binding = await client.post("/api/v1/workflows/bindings", headers=headers,
        json={"workflow_id": response.json()["id"], "workspace_id": workspace_id})
    assert binding.status_code == 201, binding.text

    async def run(**inputs):
        started = await client.post(f"/api/v1/workflows/bindings/{binding.json()['id']}/run", headers=headers,
            json={"execute": False, "trigger_data": inputs})
        assert started.status_code == 201, started.text
        await WorkflowRunner().run(started.json()["id"])
        return await db_session.get(WorkflowRun, started.json()["id"], populate_existing=True)

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("project,outputs,taken,fresh,label", [
    ({"project_type": "article", "project_key": "{{topic}}", "state": {"title": "{{topic}}"},
      "folder": {"name": "{{topic}}"}}, ["topic"],
     {"topic": "Zero trust for midsize banks"}, {"topic": "Passkeys for retail apps"},
     "Zero trust for midsize banks"),
    ({"project_type": "daily_content_batch", "project_key": "{{day}}/{{channel}}",
      "state": {"channel": "{{channel}}"}}, ["day", "channel"],
     {"day": "2026-10-27", "channel": "linkedin"}, {"day": "2026-10-27", "channel": "x"},
     "2026-10-27/linkedin"),
])
async def test_create_with_a_taken_project_key_fails_the_run_readably(
    client, db_session, project, outputs, taken, fresh, label,
):
    run = await _keyed_project_runner(
        client, db_session, f"key_taken_{project['project_type']}",
        {**project, "operation": "create"}, outputs,
    )
    first = await run(**taken)
    assert first.status == "completed", first.error
    second = await run(**taken)
    # Used to stay "running" forever: the duplicate insert broke the run's
    # session, so the runner could not record the failure.
    assert second.status == "failed"
    assert second.current_step_id == "project"
    assert label in second.error and "already exists" in second.error
    result = second.step_results["project"]
    assert result["code"] == "workflow_project_key_taken"
    owned = (await db_session.execute(select(WorkflowProject).where(
        WorkflowProject.project_type == project["project_type"]))).scalars().all()
    # The first run's project is untouched, not adopted by the second run.
    assert [(row.id, row.last_run_id) for row in owned] == [(result["existing_project_id"], first.id)]
    assert (await run(**fresh)).status == "completed"


@pytest.mark.asyncio
async def test_claim_reuses_the_project_a_taken_key_names(client, db_session):
    run = await _keyed_project_runner(client, db_session, "key_claim", {
        "operation": "claim", "project_type": "article", "project_key": "{{topic}}",
        "state": {"title": "{{topic}}"},
    }, ["topic"])
    first = await run(topic="Zero trust")
    second = await run(topic="Zero trust")
    assert (first.status, second.status) == ("completed", "completed")
    assert first.step_results["project"]["output"]["claimed"] is True
    assert second.step_results["project"]["output"]["claimed"] is False
    assert (second.step_results["project"]["output"]["project_id"]
            == first.step_results["project"]["output"]["project_id"])


@pytest.mark.asyncio
async def test_patch_naming_a_field_no_step_produced_fails_instead_of_storing_the_placeholder(
    client, db_session,
):
    # 11-01 baseline: a patch bound {{edit.deliverable_document_id}}, but the
    # edit Agent step's result is its text, and the project stored the
    # placeholder as the document id.
    run = await _keyed_project_runner(
        client, db_session, "unresolved_patch",
        {"operation": "create", "project_type": "article", "project_key": "{{topic}}",
         "state": {"title": "{{topic}}"}},
        ["topic", "edit"],
        then={"operation": "patch", "project_type": "article", "project_id": "{{project.project_id}}",
              "expected_revision": "{{project.revision}}",
              "patch": {"deliverable_document_id": "{{edit.deliverable_document_id}}"}},
    )
    result = await run(topic="Passkeys", edit="Editorial review is complete; Word export is blocked.")
    assert (result.status, result.current_step_id) == ("failed", "save")
    assert result.step_results["save"]["code"] == "workflow_reference_unresolved"
    assert "'edit' is text, with no field 'deliverable_document_id'" in result.error
    project = (await db_session.execute(select(WorkflowProject).where(
        WorkflowProject.id == result.step_results["project"]["output"]["project_id"]))).scalar_one()
    assert (project.revision, project.state) == (0, {"title": "Passkeys"})


@pytest.mark.asyncio
async def test_optional_input_left_empty_is_left_out_of_the_project(client, db_session):
    # 11-08 staging run 01M6X5RMAT398VZH167KZ42T7M: add_dish omitted the
    # optional photo, the start node's output binding {{start.photo}} kept its
    # own text, and the dish stored "photo": "{{start.photo}}".
    typed = {"type": "object", "properties": {
        "name": {"type": "string"}, "photo": {"type": "string"}, "cover": {"type": "string"},
        "caption": {"type": "string"}}}
    run = await _keyed_project_runner(
        client, db_session, "optional_input_left_empty",
        {"operation": "create", "project_type": "dish", "state_schema": typed,
         "state": {"name": "{{name}}", "photo": "{{photo}}", "cover": "house.jpg",
                   "caption": "{{name}} {{photo}}"}},
        ["name", "photo"],
        then={"operation": "patch", "project_type": "dish", "state_schema": typed,
              "project_id": "{{project.project_id}}", "expected_revision": "{{project.revision}}",
              "patch": {"name": "{{name}} (signature)", "cover": "{{photo}}"}},
    )
    result = await run(name="水煮鱼")
    assert result.status == "completed", result.error
    assert "photo" not in result.variables
    created = result.step_results["project"]["output"]
    assert created["state"] == {"name": "水煮鱼", "cover": "house.jpg", "caption": "水煮鱼 "}
    project = await db_session.get(WorkflowProject, created["project_id"], populate_existing=True)
    # A patch leaves out what the person did not give, so it never clears the stored cover.
    assert project.state == {"name": "水煮鱼 (signature)", "cover": "house.jpg", "caption": "水煮鱼 "}


_WORD_FILE_ID = "01M6BH2SFJ6ZVJ0A7QW55CRQN0"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["article_edit_to_project_patch", "lead_triage_to_tool_call"])
async def test_agent_step_that_first_replies_in_prose_still_gives_later_steps_its_fields(
    client, db_session, monkeypatch, scenario,
):
    # 11-01 staging run 01M6BGGR2XCBVKRXJ53VGB14YZ: the edit Agent saved the
    # Word file but replied in prose, so review_status could not read
    # {{edit.deliverable_document_id}} and the run failed. The step is now
    # asked for the fields later steps read, and asked again when it replies
    # in prose; the real agent loop runs, only the model's replies are fixed.
    from types import SimpleNamespace
    from unittest.mock import patch
    from packages.core.ai import workflow_runner as runner_module

    async def appendix(*_args, **_kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[], allowed_tool_names=set(), envelope=None)

    monkeypatch.setattr(runner_module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    if scenario == "article_edit_to_project_patch":
        agent = {"id": "edit", "type": "agent", "config": {"prompt": "Edit the draft and save the Word file."}}
        then = {"operation": "patch", "project_type": "article", "project_id": "{{project.project_id}}",
                "expected_revision": "{{project.revision}}",
                "patch": {"status": "review", "deliverable_document_id": "{{edit.deliverable_document_id}}"}}
        between = (agent,)
        prose = f"Revised the article and saved the [final Word document](/viewer/{_WORD_FILE_ID})."
        answer = {"summary": "Saved the final Word document.", "deliverable_document_id": _WORD_FILE_ID}
    else:
        sent = []

        async def send(**kwargs):
            sent.append(kwargs["arguments"])
            return SimpleNamespace(output="updated", envelope=None)

        monkeypatch.setattr(runner_module, "runtime_execute_workflow_tool_step", send)
        agent = {"id": "qualify", "type": "agent", "config": {"prompt": "Qualify the lead {{topic}}."}}
        then = None
        between = (agent, {"id": "update_crm", "type": "tool", "config": {"tool": "update_record", "args": {
            "score": "{{qualify.score}}", "owner": "{{qualify.owner_email}}"}}})
        prose = "This lead is a strong fit: score 82, owned by ana@example.com."
        answer = {"summary": "Strong fit.", "score": 82, "owner_email": "ana@example.com"}

    replies = [prose, json.dumps(answer)]
    prompts = []

    async def model(messages, *_args, **_kwargs):
        prompts.append(messages[-1]["content"])
        return replies.pop(0), {"prompt": 10, "completion": 5, "total": 15}

    run = await _keyed_project_runner(
        client, db_session, f"prose_{scenario}",
        {"operation": "create", "project_type": "article", "project_key": "{{topic}}", "state": {"title": "{{topic}}"}},
        ["topic"], then=then, between=between,
    )
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", side_effect=model), \
            patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
                  side_effect=lambda *a, **k: _with_no_tool_calls(model(*a, **k))):
        result = await run(topic="Passkeys")

    assert (result.status, result.error) == ("completed", None)
    assert result.step_results[agent["id"]]["output"] == answer
    assert all(field in prompts[0] for field in answer)  # asked for the fields up front
    assert replies == []  # and once more after the prose reply
    if scenario == "article_edit_to_project_patch":
        project = (await db_session.execute(select(WorkflowProject).where(
            WorkflowProject.id == result.step_results["project"]["output"]["project_id"]))).scalar_one()
        assert project.state["deliverable_document_id"] == _WORD_FILE_ID
    else:
        assert sent == [{"score": 82, "owner": "ana@example.com"}]


async def _with_no_tool_calls(reply):
    content, usage = await reply
    return content, None, usage


@pytest.mark.asyncio
async def test_agent_step_its_round_limit_stops_before_its_fields_continues_on_the_real_loop(
    client, db_session, monkeypatch,
):
    # 11-01 staging run 01M6CDWDTVZ1RP128CVWPKSPM5: the edit Agent spent its
    # rounds and answered deliverable_document_id "NOT_SAVED". The host sees
    # the field is not a document and continues the same conversation with a
    # fresh budget from the loop's own run_state; only the model is fixed.
    from types import SimpleNamespace
    from unittest.mock import patch
    from packages.core.ai import workflow_runner as runner_module

    tool = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}

    async def appendix(*_args, **_kwargs):
        return SimpleNamespace(prompt_appendix="", tool_schemas=[tool], allowed_tool_names={"read_file"},
                               envelope=None)

    monkeypatch.setattr(runner_module, "runtime_prepare_prompt_appendix_for_turn", appendix)
    usage = {"prompt": 10, "completion": 5, "total": 15}
    rounds = [
        ("", [{"id": "call-1", "name": "read_file", "arguments": {}}], usage),
        (json.dumps({"summary": "Saved the Word file.", "deliverable_document_id": _WORD_FILE_ID}), None, usage),
    ]
    prompts = []

    async def tool_round(messages, *_args, **_kwargs):
        prompts.append(messages[-1]["content"])
        return rounds.pop(0)

    async def out_of_rounds(*_args, **_kwargs):
        return json.dumps({"summary": "Blocked by the tool limit.", "deliverable_document_id": "NOT_SAVED"}), usage

    run = await _keyed_project_runner(
        client, db_session, "round_limit_edit",
        {"operation": "create", "project_type": "article", "project_key": "{{topic}}", "state": {"title": "{{topic}}"}},
        ["topic"],
        then={"operation": "patch", "project_type": "article", "project_id": "{{project.project_id}}",
              "expected_revision": "{{project.revision}}",
              "patch": {"deliverable_document_id": "{{edit.deliverable_document_id}}"}},
        between=({"id": "edit", "type": "agent", "config": {"prompt": "Edit and save the Word file.", "max_rounds": 1}},),
    )
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", side_effect=tool_round), \
            patch("packages.core.ai.agentic_loop.runtime_execute_agentic_final_completion", side_effect=out_of_rounds):
        result = await run(topic="Passkeys")

    assert (result.status, result.error) == ("completed", None)
    assert rounds == []
    assert "Still to do: reply with the object" in prompts[-1]
    assert result.step_results["edit"]["output"]["deliverable_document_id"] == _WORD_FILE_ID
    project = (await db_session.execute(select(WorkflowProject).where(
        WorkflowProject.id == result.step_results["project"]["output"]["project_id"]))).scalar_one()
    assert project.state["deliverable_document_id"] == _WORD_FILE_ID


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
async def test_delete_takes_the_project_and_its_children_unless_another_run_works_on_one(db_session):
    from packages.core.models.base import generate_ulid

    ws = generate_ulid()
    parent = await create_workflow_project(db_session, entity_id="ent1", workspace_id=ws, project_type="campaign",
                                           state={}, created_by="user1")
    busy_run = WorkflowRun(id=generate_ulid(), workflow_id="wf", entity_id="ent1", workspace_id=ws, status="running")
    db_session.add(busy_run)
    children = [await create_workflow_project(db_session, entity_id="ent1", workspace_id=ws, project_type="post",
                                              state={"parent_project_id": parent.id}, created_by="user1",
                                              last_run_id=run_id) for run_id in (None, busy_run.id)]
    stranger = await create_workflow_project(db_session, entity_id="ent1", workspace_id=ws, project_type="post",
                                             state={"parent_project_id": "another"}, created_by="user1")
    await db_session.commit()
    delete = dict(project_id=parent.id, entity_id="ent1", workspace_id=ws, project_type="campaign",
                  children=[("post", "parent_project_id")])
    # A post another run is publishing keeps the whole delete from starting.
    with pytest.raises(WorkflowProjectInUse):
        await delete_workflow_project(db_session, **delete)
    with pytest.raises(ValueError):
        await delete_workflow_project(db_session, **{**delete, "project_type": "post"})
    # The run doing the delete does not count as another one.
    deleted = await delete_workflow_project(db_session, **delete, run_id=busy_run.id)
    await db_session.commit()
    assert set(deleted) == {parent.id, *(child.id for child in children)}
    remaining = (await db_session.execute(select(WorkflowProject.id).where(WorkflowProject.workspace_id == ws))).scalars().all()
    assert remaining == [stranger.id]


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


@pytest.mark.asyncio
async def test_delete_refuses_when_the_children_differ_from_the_listed_set(db_session):
    from packages.core.models.base import generate_ulid

    ws = generate_ulid()
    parent = await create_workflow_project(db_session, entity_id="ent1", workspace_id=ws, project_type="campaign",
                                           state={}, created_by="user1")
    listed = await create_workflow_project(db_session, entity_id="ent1", workspace_id=ws, project_type="post",
                                           state={"parent_project_id": parent.id}, created_by="user1")
    await create_workflow_project(db_session, entity_id="ent1", workspace_id=ws, project_type="post",
                                  state={"parent_project_id": parent.id}, created_by="user1")
    await db_session.commit()
    # A child added after the listing is not in the set the files were cleaned up for: refuse, delete nothing.
    with pytest.raises(WorkflowProjectInUse):
        await delete_workflow_project(db_session, project_id=parent.id, entity_id="ent1", workspace_id=ws,
                                      project_type="campaign", children=[("post", "parent_project_id")],
                                      expected_child_ids=[listed.id])
    await db_session.rollback()
    remaining = (await db_session.execute(select(WorkflowProject.id).where(WorkflowProject.workspace_id == ws))).scalars().all()
    assert len(remaining) == 3


@pytest.mark.asyncio
async def test_a_watch_calls_its_agent_only_when_something_is_new(client, db_session, monkeypatch):
    import contextlib
    import io
    from types import SimpleNamespace

    from packages.core.ai import workflow_runner as runner_module
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.blueprints.workflow_patterns import source_feed_for, source_watch_flows
    from tests.test_workflows import _auth, _create_workspace

    feed = source_feed_for("the Gmail inbox", "email")
    watch = source_watch_flows(slug="inbox", name="Look", service_key="care", source="the Gmail inbox",
                               prompt="Customers' mail.", item_word="email", item_fields=["sender"],
                               run_workflow="reply", run_input="message_id", cron_expr="0 * * * *",
                               timezone="UTC", feed=feed)["workflows"][0]
    steps = watch["steps"]
    next(step for step in steps if step["id"] == "find")["config"].pop("service_key")
    headers = await _auth(client, "watch_gate")
    workspace_id = await _create_workspace(client, headers, "watch_gate")
    created = await client.post("/api/v1/workflows", headers=headers,
                                json={"name": "watch_gate", "workspace_id": workspace_id, "steps": steps})
    assert created.status_code == 201, created.text
    binding = await client.post("/api/v1/workflows/bindings", headers=headers,
                                json={"workflow_id": created.json()["id"], "workspace_id": workspace_id})
    assert binding.status_code == 201, binding.text

    inbox = [{"messages": []}]
    prompts: list[str] = []
    fail_child_admission = False

    async def read_inbox(**kwargs):
        assert kwargs["tool_name"] == feed.tool
        return SimpleNamespace(output=json.dumps(inbox[0]), envelope=None)

    async def attach(result, _envelope):
        return result

    async def ask_agent(self, step, variables, *_args, **_kwargs):
        prompts.append(runner_module._resolve_structure(step["config"]["prompt"], variables))
        output = {"could_read": True, "items": [{"key": row["id"], "sender": row["from"]}
                                                for row in variables["read"]["items"] if row["id"] != "junk"]}
        output["items"].append({"key": "absent-from-feed"})
        return {"status": "completed", "output": output}

    async def start_children(self, step, variables, *_args, **_kwargs):
        assert step["config"]["continue_on_failure"] is False
        expected_id = "m2" if inbox[0].get("status") == "partial" else "m1"
        assert [row["key"] for row in variables["new_items"]["items"]] == [expected_id]
        if fail_child_admission:
            return {"status": "failed", "error": "child admission failed"}
        return {"status": "completed", "output": [], "output_var": "started"}

    monkeypatch.setattr(runner_module, "runtime_execute_workflow_tool_step", read_inbox)
    monkeypatch.setattr(runner_module, "runtime_attach_and_persist_workflow_runner_result", attach)
    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", ask_agent)
    monkeypatch.setattr(WorkflowRunner, "_execute_foreach_subworkflow_step", start_children)
    sandboxed: list[str] = []

    async def run_code(self, step, variables, *_args, **_kwargs):
        sandboxed.append(step["id"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(step["config"]["code"], {"inputs": variables})
        return {"status": "completed", "output": json.loads(out.getvalue()), "output_var": step["config"]["output_var"]}

    monkeypatch.setattr(WorkflowRunner, "_execute_code_step", run_code)

    async def look(expected_status="completed"):
        started = await client.post(f"/api/v1/workflows/bindings/{binding.json()['id']}/run", headers=headers,
                                    json={"execute": False, "trigger_data": {}})
        assert started.status_code == 201, started.text
        await WorkflowRunner().run(started.json()["id"])
        run = await db_session.get(WorkflowRun, started.json()["id"], populate_existing=True)
        assert run.status == expected_status, run.error
        return run

    # Nothing new: neither a model call nor a sandbox.
    assert "find" not in (await look()).step_results and prompts == [] and sandboxed == []

    inbox[0] = {"messages": [{"id": "m1", "from": "a@x.co", "subject": "Refund"},
                             {"id": "junk", "from": "ads@x.co", "subject": "Sale"}]}
    fail_child_admission = True
    assert "remember" not in (await look("failed")).step_results
    fail_child_admission = False
    first = await look()
    assert len(prompts) == 2 and all("m1" in prompt and "Refund" in prompt for prompt in prompts)
    assert [row["key"] for row in first.step_results["new_items"]["output"]["items"]] == ["m1"]

    # Seen before, whether or not the Agent kept it: no second call, also with one more account's page shape.
    assert "find" not in (await look()).step_results and len(prompts) == 2
    inbox[0] = {"integration_account_selection": "all", "status": "complete",
                "results": [{"integration_account_id": "a", "ok": True, "result": json.dumps(inbox[0])}]}
    assert "find" not in (await look()).step_results and len(prompts) == 2

    # One account that cannot be read does not hold up what another one answered.
    inbox[0] = {"integration_account_selection": "all", "status": "partial", "failed_count": 1, "results": [
        {"integration_account_id": "a", "ok": True, "result": {"messages": [{"id": "m2", "from": "b@x.co"}]}},
        {"integration_account_id": "b", "ok": False, "result": {"error": "tool_error"}}]}
    prompts_before = len(prompts)
    partial = await look()
    assert len(prompts) == prompts_before + 1 and "m2" in prompts[-1]
    assert partial.step_results["read"]["output"]["accounts_not_read"] == 1
    sandboxed.clear()

    # An account that cannot be read costs no model call either, and is not "nothing new".
    inbox[0] = {"error": "No gmail integration is connected."}
    unread = await look()
    assert "unread" in unread.step_results and "find" not in unread.step_results and len(prompts) == prompts_before + 1
    assert sandboxed == []
