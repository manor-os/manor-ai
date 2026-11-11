"""E2E tests: workflow definitions, runs, and step execution."""
import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from types import SimpleNamespace

from tests.async_waits import LIVENESS_TIMEOUT_S, wait_for_signal
from tests.auth_helpers import register_user_and_get_token


#: A paused run's intervention recommends resuming (shared default reason).
_RESUME_RECOMMENDED = {
    "recommendation": {
        "option": "resume", "reason": "Trying again continues from where it stopped.", "reason_code": "retry",
    },
    "allow_other": True,
}


async def _auth(client: AsyncClient, username: str = "wfuser") -> dict:
    resp = await register_user_and_get_token(client, json={
        "username": username, "email": f"{username}@test.com",
        "password": "WorkflowApprovalTest123!", "entity_name": "Workflow Corp",
    })
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


async def _create_workspace(
    client: AsyncClient,
    headers: dict,
    name: str,
) -> str:
    response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_flow_list_excludes_deleted_workspace_and_internal_blueprint_flows(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowDefinition, WorkflowTemplateInstallation
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, "flow_list_lifecycle")
    active_id = await _create_workspace(client, headers, "Video Studio")
    deleted_id = await _create_workspace(client, headers, "Old Video Studio")
    owner = (await db_session.execute(
        select(User).where(User.email == "flow_list_lifecycle@test.com")
    )).scalar_one()
    definitions = [
        WorkflowDefinition(entity_id=owner.entity_id, created_by=owner.id, name="Standalone"),
        WorkflowDefinition(entity_id=owner.entity_id, created_by=owner.id,
                           workspace_id=active_id, name="Create Video"),
        WorkflowDefinition(entity_id=owner.entity_id, created_by=owner.id,
                           workspace_id=active_id, name="Internal Executor"),
        WorkflowDefinition(entity_id=owner.entity_id, created_by=owner.id,
                           workspace_id=deleted_id, name="Create Video"),
    ]
    db_session.add_all(definitions)
    await db_session.flush()
    db_session.add(WorkflowTemplateInstallation(
        entity_id=owner.entity_id,
        template_id=f"blueprint:{active_id}",
        component_key="internal-executor",
        workflow_id=definitions[2].id,
        source_type="workspace_blueprint",
        installation_metadata={"internal": True},
    ))
    await db_session.commit()
    assert await soft_delete_workspace(db_session, deleted_id, owner.entity_id)
    await db_session.commit()

    response = await client.get("/api/v1/workflows", headers=headers)
    assert response.status_code == 200, response.text
    assert {flow["id"] for flow in response.json()} == {definitions[0].id, definitions[1].id}
    scoped = next(flow for flow in response.json() if flow["name"] == "Create Video")
    assert scoped["workspace_id"] == active_id
    assert scoped["workspace_name"] == "Video Studio"


@pytest.mark.asyncio
async def test_flow_list_summary_leaves_out_graphs(client: AsyncClient):
    headers = await _auth(client, "wfsummary")
    created = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Weekly report",
        "steps": [{"id": "start", "type": "trigger", "next": []}],
        "variables": {"ready": False},
    })).json()

    full = (await client.get("/api/v1/workflows", headers=headers)).json()
    summary = (await client.get("/api/v1/workflows?summary=true", headers=headers)).json()

    assert [w["id"] for w in full] == [w["id"] for w in summary] == [created["id"]]
    assert full[0]["steps"] and full[0]["variables"]
    assert summary[0]["name"] == "Weekly report"
    assert summary[0]["steps"] == [] and summary[0]["variables"] == {}
    assert summary[0]["step_count"] == 1
    assert full[0]["step_count"] is None


def _simple_steps(step_ids: list[str]) -> list[dict]:
    """Build an explicit trigger → transform chain → end graph for testing."""
    first = step_ids[0] if step_ids else "end"
    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": [first]},
    ]
    for i, sid in enumerate(step_ids):
        step: dict = {
            "id": sid,
            "type": "transform",
            "name": f"Step {sid}",
            "config": {"set": {sid: "done"}},
        }
        if i + 1 < len(step_ids):
            step["next"] = [step_ids[i + 1]]
        else:
            step["next"] = ["end"]
        steps.append(step)
    steps.append({"id": "end", "type": "end", "name": "Done", "config": {}, "next": []})
    return steps


async def _execute_created_retry(
    client: AsyncClient,
    headers: dict,
    response,
) -> dict:
    from packages.core.ai.workflow_runner import WorkflowRunner

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "running"
    await WorkflowRunner().run(created["id"])
    refreshed = await client.get(
        f"/api/v1/workflows/runs/{created['id']}",
        headers=headers,
    )
    assert refreshed.status_code == 200, refreshed.text
    return refreshed.json()


async def _execute_queued_binding_run(
    client: AsyncClient,
    headers: dict,
    response,
) -> dict:
    """Run what a binding run queued, as the ``run_workflow`` worker would."""
    from packages.core.ai.workflow_runner import WorkflowRunner

    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] in {"pending", "running"}, created
    assert not created["step_results"], "the request must not execute nodes"
    await WorkflowRunner().run(created["id"])
    refreshed = await client.get(
        f"/api/v1/workflows/runs/{created['id']}",
        headers=headers,
    )
    assert refreshed.status_code == 200, refreshed.text
    return refreshed.json()


@pytest.mark.asyncio
async def test_start_workflow_rejects_workspace_from_another_entity(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.services.workflow_service import start_workflow

    owner_headers = await _auth(client, "workflow_workspace_owner")
    foreign_headers = await _auth(client, "workflow_workspace_foreign")
    workflow = await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={"name": "Scoped workflow", "steps": _simple_steps([])},
    )
    assert workflow.status_code == 201, workflow.text
    foreign_workspace = await client.post(
        "/api/v1/workspaces",
        headers=foreign_headers,
        json={"name": "Foreign workflow workspace"},
    )
    assert foreign_workspace.status_code == 201, foreign_workspace.text
    owner = (await db_session.execute(
        select(User).where(User.email == "workflow_workspace_owner@test.com")
    )).scalar_one()

    with pytest.raises(ValueError, match="Workspace not found"):
        await start_workflow(
            db_session,
            str(owner.entity_id),
            workflow.json()["id"],
            workspace_id=foreign_workspace.json()["id"],
        )


@pytest.mark.asyncio
async def test_start_workflow_inherits_definition_workspace_and_rejects_conflicts(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.services.workflow_service import start_workflow

    headers = await _auth(client, "workflow_definition_workspace")
    workflow_response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Workspace-owned workflow", "steps": _simple_steps([])},
    )
    first_workspace = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "First workspace"},
    )
    second_workspace = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Second workspace"},
    )
    assert workflow_response.status_code == first_workspace.status_code == second_workspace.status_code == 201

    workflow = await db_session.get(WorkflowDefinition, workflow_response.json()["id"])
    workflow.workspace_id = first_workspace.json()["id"]
    owner = (await db_session.execute(
        select(User).where(User.email == "workflow_definition_workspace@test.com")
    )).scalar_one()
    await db_session.flush()

    run = await start_workflow(
        db_session, str(owner.entity_id), workflow.id, started_by=owner.id,
    )
    assert run.workspace_id == first_workspace.json()["id"]

    with pytest.raises(ValueError, match="belongs to another workspace"):
        await start_workflow(
            db_session,
            str(owner.entity_id),
            workflow.id,
            started_by=owner.id,
            workspace_id=second_workspace.json()["id"],
        )


@pytest.mark.asyncio
async def test_workspace_owned_workflow_binding_cannot_cross_workspace(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.services.workflow_service import (
        create_workflow_binding,
        start_workflow_from_binding,
    )

    headers = await _auth(client, "workflow_binding_workspace_scope")
    owned_response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Owned workflow", "steps": _simple_steps([])},
    )
    shared_response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Shared workflow", "steps": _simple_steps([])},
    )
    second_owned_response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Second owned workflow", "steps": _simple_steps([])},
    )
    first_workspace = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "First workspace"},
    )
    second_workspace = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Second workspace"},
    )
    assert all(response.status_code == 201 for response in (
        owned_response, shared_response, second_owned_response, first_workspace, second_workspace,
    ))
    owner = (await db_session.execute(
        select(User).where(User.email == "workflow_binding_workspace_scope@test.com")
    )).scalar_one()
    first_workspace_id = first_workspace.json()["id"]
    second_workspace_id = second_workspace.json()["id"]
    owned = await db_session.get(WorkflowDefinition, owned_response.json()["id"])
    owned.workspace_id = first_workspace_id
    second_owned = await db_session.get(WorkflowDefinition, second_owned_response.json()["id"])
    second_owned.workspace_id = second_workspace_id
    await db_session.commit()

    inherited_binding = await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={"workflow_id": owned.id},
    )
    assert inherited_binding.status_code == 201, inherited_binding.text
    owned_binding = await db_session.get(WorkflowBinding, inherited_binding.json()["id"])
    assert owned_binding.workspace_id == first_workspace_id

    create_conflict = await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={"workflow_id": owned.id, "workspace_id": second_workspace_id},
    )
    assert create_conflict.status_code == 409, create_conflict.text
    workspace_update_conflict = await client.put(
        f"/api/v1/workflows/bindings/{owned_binding.id}",
        headers=headers,
        json={"workspace_id": second_workspace_id},
    )
    assert workspace_update_conflict.status_code == 409, workspace_update_conflict.text
    workflow_update_conflict = await client.put(
        f"/api/v1/workflows/bindings/{owned_binding.id}",
        headers=headers,
        json={"workflow_id": second_owned.id},
    )
    assert workflow_update_conflict.status_code == 409, workflow_update_conflict.text

    # Historical rows may predate the creation/update guard. The runtime must
    # retain the same definition boundary before resolving Workspace resources.
    owned_binding.workspace_id = second_workspace_id
    await db_session.flush()
    with pytest.raises(ValueError, match="belongs to another workspace"):
        await start_workflow_from_binding(db_session, owned_binding, started_by=owner.id)

    shared = await db_session.get(WorkflowDefinition, shared_response.json()["id"])
    first_shared_binding = await create_workflow_binding(
        db_session, str(owner.entity_id), shared.id, workspace_id=first_workspace_id,
    )
    second_shared_binding = await create_workflow_binding(
        db_session, str(owner.entity_id), shared.id, workspace_id=second_workspace_id,
    )
    assert {first_shared_binding.workspace_id, second_shared_binding.workspace_id} == {
        first_workspace_id, second_workspace_id,
    }


@pytest.mark.asyncio
async def test_start_workflow_allows_manual_work_but_not_automatic_work_when_paused(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.services.workflow_service import start_workflow

    headers = await _auth(client, "workflow_workspace_lifecycle")
    workflow = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Lifecycle workflow", "steps": _simple_steps([])},
    )
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Lifecycle workspace"},
    )
    assert workflow.status_code == 201, workflow.text
    assert workspace.status_code == 201, workspace.text
    paused = await client.post(
        f"/api/v1/workspaces/{workspace.json()['id']}/pause",
        headers=headers,
    )
    assert paused.status_code == 200, paused.text
    owner = (await db_session.execute(
        select(User).where(User.email == "workflow_workspace_lifecycle@test.com")
    )).scalar_one()
    entity_id = str(owner.entity_id)
    owner_id = owner.id

    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun

    scheduled_job = ScheduledJob(
        job_id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.json()["id"],
        execution_type="workflow",
        execution_target={"workflow_id": workflow.json()["id"]},
        enabled=True,
    )
    scheduled_job_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=scheduled_job.job_id,
        status="running",
        trigger_type="cron",
    )
    scheduled_job_id = scheduled_job.job_id
    scheduled_job_run_id = scheduled_job_run.id
    db_session.add_all([scheduled_job, scheduled_job_run])
    await db_session.flush()

    scheduled = await start_workflow(
        db_session,
        entity_id,
        workflow.json()["id"],
        workspace_id=workspace.json()["id"],
        started_by=owner_id,
        trigger_source="schedule",
        trigger_data={
            "scheduled_job_id": scheduled_job_id,
            "scheduled_run_id": scheduled_job_run_id,
        },
    )
    assert scheduled.trigger_source == "schedule"

    run = await start_workflow(
        db_session,
        entity_id,
        workflow.json()["id"],
        workspace_id=workspace.json()["id"],
        started_by=owner_id,
    )
    assert run.workspace_id == workspace.json()["id"]
    assert run.trigger_source == "manual"
    await db_session.commit()

    with pytest.raises(ValueError, match="enabled configured Workflow"):
        await start_workflow(
            db_session,
            entity_id,
            workflow.json()["id"],
            workspace_id=workspace.json()["id"],
            trigger_source="event",
        )
    await db_session.rollback()

    with pytest.raises(ValueError, match="enabled configured Workflow"):
        await start_workflow(
            db_session,
            entity_id,
            workflow.json()["id"],
            workspace_id=workspace.json()["id"],
            started_by=owner_id,
            trigger_source="schedule",
            trigger_data={
                "scheduled_job_id": scheduled_job_id,
                "scheduled_run_id": "forged-scheduled-run",
            },
        )
    await db_session.rollback()

    with pytest.raises(ValueError, match="Workspace not found"):
        await start_workflow(
            db_session,
            entity_id,
            workflow.json()["id"],
            workspace_id="legacy-opaque-workspace",
        )


def test_workflow_definition_snapshot_keeps_ordered_display_graph_without_config_secrets():
    from packages.core.services.workflow_run_trace import build_definition_snapshot

    workflow = SimpleNamespace(
        id="wf-1",
        name="Example",
        version=4,
        steps=[
            {
                "id": "start",
                "name": "Start",
                "type": "trigger",
                "config": {
                    "api_key": "snapshot-secret",
                    "chat_projection": "hidden",
                },
                "next": "route",
            },
            {
                "id": "route",
                "name": "Route",
                "type": "switch",
                "chat_projection": "output",
                "true_next": ["finish"],
                "false_next": ["retry", "finish"],
                "config": {
                    "credential": "another-secret",
                    "cases": [
                        {"expression": "kind == 'retry'", "next": "retry"},
                        {"expression": "kind == 'done'", "next": ["finish"]},
                    ],
                    "default_next": ["finish"],
                },
            },
        ],
    )

    snapshot = build_definition_snapshot(workflow, fingerprint="fingerprint-1")

    assert snapshot == {
        "workflow_id": "wf-1",
        "name": "Example",
        "version": 4,
        "fingerprint": "fingerprint-1",
        "nodes": [
            {
                "id": "start",
                "name": "Start",
                "type": "trigger",
                "order": 0,
                "targets": ["route"],
                "chat_projection": "hidden",
            },
            {
                "id": "route",
                "name": "Route",
                "type": "switch",
                "order": 1,
                "targets": ["finish", "retry"],
                "chat_projection": "output",
            },
        ],
    }
    assert "snapshot-secret" not in json.dumps(snapshot)
    assert "another-secret" not in json.dumps(snapshot)


def test_run_display_metadata_preserves_every_exact_node_identity():
    from apps.api.routers.workflows import _run_to_dict
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import (
        _snapshot_display_metadata,
        _workflow_display_metadata,
    )

    long_node_id = f"long-node-{'x' * 140}"
    nodes = [
        {"id": f"node-{index}", "name": f"Node {index}"}
        for index in range(513)
    ]
    nodes.append({"id": long_node_id, "name": "Exact long-ID node"})
    workflow = SimpleNamespace(
        id="workflow-complete-display-metadata",
        name="Complete display metadata",
        steps=nodes,
    )
    snapshot = {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "nodes": nodes,
    }

    workflow_metadata = _workflow_display_metadata(workflow)
    snapshot_metadata = _snapshot_display_metadata(snapshot)
    for metadata in (workflow_metadata, snapshot_metadata):
        node_names = metadata["_workflow_node_names"]
        assert len(node_names) == 514
        assert node_names["node-512"] == "Node 512"
        assert node_names[long_node_id] == "Exact long-ID node"
        assert long_node_id[:100] not in node_names

    run = WorkflowRun(
        id="run-complete-display-metadata",
        workflow_id=workflow.id,
        entity_id="entity-complete-display-metadata",
        status="running",
        current_step_id=long_node_id,
        variables={},
        step_results={},
        trigger_data=snapshot_metadata,
        definition_snapshot=snapshot,
        execution_trace=[],
    )
    summary = _run_to_dict(run, include_detail=False, summary=True)
    assert summary["current_step_name"] == "Exact long-ID node"


def test_run_display_metadata_preserves_exact_long_workflow_and_node_names():
    from apps.api.routers.workflows import _run_to_dict
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import (
        _snapshot_display_metadata,
        _workflow_display_metadata,
    )

    long_workflow_name = f"Workflow {'w' * 300}"
    long_node_name = f"Node {'n' * 300}"
    workflow = SimpleNamespace(
        id="workflow-long-display-names",
        name=long_workflow_name,
        steps=[{"id": "long-name-node", "name": long_node_name}],
    )
    snapshot = {
        "workflow_id": workflow.id,
        "name": long_workflow_name,
        "nodes": workflow.steps,
    }

    workflow_metadata = _workflow_display_metadata(workflow)
    snapshot_metadata = _snapshot_display_metadata(snapshot)
    for metadata in (workflow_metadata, snapshot_metadata):
        assert metadata["_workflow_name"] == long_workflow_name
        assert metadata["_workflow_node_names"] == {
            "long-name-node": long_node_name,
        }

    run = WorkflowRun(
        id="run-long-display-names",
        workflow_id=workflow.id,
        entity_id="entity-long-display-names",
        status="running",
        current_step_id="long-name-node",
        variables={},
        step_results={},
        trigger_data=snapshot_metadata,
        definition_snapshot=snapshot,
        execution_trace=[],
    )
    summary = _run_to_dict(run, include_detail=False, summary=True)
    assert summary["workflow_name"] == long_workflow_name
    assert summary["current_step_name"] == long_node_name


def test_trace_summary_recursively_redacts_secrets_and_has_strict_byte_cap():
    from packages.core.services.workflow_run_trace import (
        TRACE_SUMMARY_BYTES,
        summarize_trace_value,
    )

    summary = summarize_trace_value({
        "password": "plain-text",
        "nested": [{"access_token": "nested-secret"}],
        "body": "\u754c" * 20_000,
    })

    encoded = json.dumps(summary, ensure_ascii=False, default=str).encode("utf-8")
    assert summary["truncated"] is True
    assert "[REDACTED]" in summary["preview"]
    assert "plain-text" not in summary["preview"]
    assert "nested-secret" not in summary["preview"]
    assert len(encoded) <= TRACE_SUMMARY_BYTES


def test_trace_summary_redacts_headers_private_keys_and_free_text_tokens():
    from packages.core.services.workflow_run_trace import summarize_trace_value

    private_key_begin = "-----BEGIN " + "PRIVATE KEY-----"
    secrets = {
        "Authorization": "Bearer authorization-secret-value",
        "X-API-Key": "x-api-key-secret-value",
        "Cookie": "session=cookie-secret-value",
        "private-key": f"{private_key_begin}\nprivate-key-secret\n-----END PRIVATE KEY-----",
        "message": (
            "Authorization: Bearer free-text-authorization-secret "
            "X-API-Key: free-text-api-secret Cookie: session=free-text-cookie-secret"
        ),
        "token_count": 42,
        "secretary_note": "keep this ordinary text",
    }

    summary = summarize_trace_value(secrets)
    encoded = json.dumps(summary)

    assert summary["Authorization"] == "[REDACTED]"
    assert summary["X-API-Key"] == "[REDACTED]"
    assert summary["Cookie"] == "[REDACTED]"
    assert summary["private-key"] == "[REDACTED]"
    assert summary["token_count"] == 42
    assert summary["secretary_note"] == "keep this ordinary text"
    assert "free-text-authorization-secret" not in encoded
    assert "free-text-api-secret" not in encoded
    assert "free-text-cookie-secret" not in encoded
    assert encoded.count("[REDACTED]") >= 5


def test_history_summary_projects_stages_waiting_point_deliverables_and_publications():
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import update_workflow_history_summary

    run = WorkflowRun(
        id="run-stages", workflow_id="wf", entity_id="entity", status="paused", current_step_id="approve",
        trigger_data={},
        definition_snapshot={"nodes": [
            {"id": "start", "type": "trigger", "name": "Start"},
            {"id": "draft", "type": "agent", "name": "Draft storyboard"},
            {"id": "ready", "type": "condition", "name": "Storyboard ready?"},
            {"id": "approve", "type": "wait", "name": "Approve storyboard"},
            {"id": "render", "type": "stage", "name": "Render"},
        ]},
        step_results={
            "start": {"status": "completed"},
            "draft": {"status": "completed"},
            "ready": {"status": "completed"},
            "publish": {"status": "completed", "output": {
                "schema_version": "publication-receipt/v1", "platform": "linkedin", "title": "Launch post",
                "published_url": "https://www.linkedin.com/feed/update/1", "verification_status": "verified",
                "external_id": "1", "payload_hash": "h", "evidence": [{"kind": "published_url"}],
            }},
        },
        variables={"__result": {"files": [
            {"document_id": "doc-1", "name": "cut.mp4", "role": "final_video", "storage": "ignored"},
            {"name": "no id is not a delivered file"},
        ]}},
        execution_trace=[],
    )
    update_workflow_history_summary(run)
    summary = run.trigger_data["_workflow_history_summary"]

    assert summary["schema_version"] == 4
    # Routing nodes are not stages a person would recognise.
    assert [stage["name"] for stage in summary["stages"]] == ["Draft storyboard", "Approve storyboard", "Render"]
    assert summary["current_stage"] == {
        "id": "approve", "name": "Approve storyboard", "index": 1, "status": "paused", "waiting": True,
    }
    assert summary["deliverables"] == [{"document_id": "doc-1", "name": "cut.mp4", "role": "final_video"}]
    assert summary["publications"] == [{
        "platform": "linkedin", "title": "Launch post", "published_url": "https://www.linkedin.com/feed/update/1",
        "verification_status": "verified",
    }]


def test_a_result_that_lists_no_files_delivered_none():
    """A planning run touches Knowledge on the way; its steps' files are not its output."""
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import workflow_run_deliverables

    cited = {"status": "completed", "saved_files": [{"document_id": "knowledge-1", "name": "product-facts.md"}]}
    run = WorkflowRun(
        id="run-plan", workflow_id="wf", entity_id="entity", status="completed", current_step_id="done",
        trigger_data={}, definition_snapshot={"nodes": []}, step_results={"plan": cited}, execution_trace=[],
    )
    # Without a files list, the run's step files stand in for what it delivered.
    assert workflow_run_deliverables({"__result": {"title": "Plan"}}, entity_id="entity", run=run)
    assert workflow_run_deliverables({"__result": {"title": "Plan", "files": []}}, entity_id="entity", run=run) == []


def test_a_step_that_reports_it_was_blocked_is_not_just_completed():
    """A Video Studio run failed while its render step said Completed: the step ran,
    but its own result said blocked. Checks keep their yes/no answer."""
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import update_workflow_history_summary

    run = WorkflowRun(
        id="run-blocked", workflow_id="wf", entity_id="entity", status="failed", current_step_id="render_blocked",
        trigger_data={}, execution_trace=[], variables={},
        definition_snapshot={"nodes": [
            {"id": "render", "type": "agent", "name": "Render"},
            {"id": "render_ok", "type": "condition", "name": "Final video ready?"},
            {"id": "render_blocked", "type": "stop", "name": "Render blocked"},
        ]},
        step_results={
            "render": {"status": "completed", "output": {"status": "blocked", "summary": "Sandbox unavailable"}},
            "render_ok": {"status": "completed", "condition_result": False, "output": False},
            "render_blocked": {"status": "failed", "error": "Sandbox unavailable"},
        },
    )
    update_workflow_history_summary(run)
    steps = {step["id"]: step for step in run.trigger_data["_workflow_history_summary"]["workflow_steps"]}
    assert steps["render"]["status"] == "completed" and steps["render"]["outcome"] == "blocked"
    assert steps["render_ok"]["answer"] == "no" and "outcome" not in steps["render_ok"]


def test_steps_that_need_the_person_read_as_the_run_does():
    """An AI Podcast Studio run said Input needed while its production step and the
    end that explained what was missing both said Completed."""
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import workflow_execution_steps

    missing = {"status": "needs_input", "summary": "No source recording was attached."}
    run = WorkflowRun(
        id="run-needs-input", workflow_id="wf", entity_id="entity", status="completed", current_step_id="needs_input",
        trigger_data={}, execution_trace=[], variables={"__result": missing},
        definition_snapshot={"nodes": [
            {"id": "produce", "type": "agent", "name": "Create the actual finished files"},
            {"id": "ready", "type": "condition", "name": "Require actual deliverables"},
            {"id": "needs_input", "type": "end", "name": "Explain the missing prerequisite"},
        ]},
        step_results={
            "produce": {"status": "completed", "output": missing},
            "ready": {"status": "completed", "condition_result": False, "output": False},
            "needs_input": {"status": "completed", "output": missing},
        },
    )
    steps = {step["id"]: step for step in workflow_execution_steps(run)}
    assert steps["produce"]["outcome"] == steps["needs_input"]["outcome"] == "needs_input"
    assert steps["ready"]["answer"] == "no" and "outcome" not in steps["ready"]


def test_a_step_retried_in_a_running_run_is_running_not_its_last_failure():
    """A Video Studio retry from the render said Running while the render row said
    Failed: the row read the last attempt's result while the run ran it again."""
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import workflow_execution_steps

    def steps(status):
        run = WorkflowRun(
            id="run-retry", workflow_id="wf", entity_id="entity", status=status, current_step_id="render",
            trigger_data={}, execution_trace=[], variables={},
            definition_snapshot={"nodes": [
                {"id": "produce", "type": "agent", "name": "Produce"},
                {"id": "render", "type": "agent", "name": "Render"},
            ]},
            step_results={
                "produce": {"status": "completed", "output": {"status": "completed"}},
                "render": {"status": "failed", "error": "Sandbox unavailable"},
            },
        )
        return {step["id"]: step["status"] for step in workflow_execution_steps(run)}

    assert steps("running") == {"produce": "completed", "render": "running"}
    # Once the run has ended, its failure is the step's state again.
    assert steps("failed") == {"produce": "completed", "render": "failed"}


def test_history_summary_names_the_result_and_finds_files_the_result_nests():
    """A Handbook result nests its files in a delivery package: the App listed it as
    "Completed" with no files while Chat showed all three."""
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import update_workflow_history_summary

    def run_with(result: object) -> dict:
        run = WorkflowRun(
            id="run-nested", workflow_id="wf", entity_id="entity", status="completed",
            trigger_data={}, definition_snapshot={"nodes": []}, step_results={}, execution_trace=[],
            variables={"__result": result},
        )
        update_workflow_history_summary(run)
        return run.trigger_data["_workflow_history_summary"]

    summary = run_with({"status": "accepted", "delivery": {"status": "completed", "artifacts": [
        {"kind": "document", "name": "guide.pdf", "document_id": "01M5DHEK509JRZGST48HPR1651"},
    ]}})
    assert [item["name"] for item in summary["deliverables"]] == ["guide.pdf"]
    assert summary["result_title"] is None

    wrapped = run_with(
        {"decision": {"choice": "revise"}, "topic_brief": {"title": "Approval-first AI", "thesis": "Why it matters"}},
    )
    assert (wrapped["result_title"], wrapped["result_summary"]) == ("Approval-first AI", "Why it matters")


@pytest.mark.asyncio
async def test_history_summary_refreshes_whichever_path_changes_the_run(db_session):
    """A path that sets the final result directly still leaves a current summary."""
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun

    run = WorkflowRun(
        id=generate_ulid(), workflow_id=generate_ulid(), entity_id=generate_ulid(), status="running",
        definition_snapshot={"nodes": [{"id": "make", "type": "agent", "name": "Make files"}]},
        step_results={}, variables={}, execution_trace=[], trigger_data={},
    )
    db_session.add(run)
    await db_session.commit()

    run.status = "completed"
    run.step_results = {"make": {"status": "completed"}}
    run.variables = {"__result": {"files": [{"document_id": "doc-9", "name": "report.pdf"}]}}
    await db_session.commit()

    stored = (await db_session.execute(select(WorkflowRun.trigger_data).where(WorkflowRun.id == run.id))).scalar_one()
    summary = stored["_workflow_history_summary"]
    assert summary["deliverables"] == [{"document_id": "doc-9", "name": "report.pdf"}]
    assert summary["stages"] == [{"id": "make", "name": "Make files", "type": "agent", "status": "completed"}]
    assert summary["current_stage"] is None


def test_execution_trace_records_transition_metadata_and_enforces_caps():
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import append_execution_trace

    run = WorkflowRun(
        id="run-1",
        workflow_id="wf-1",
        entity_id="entity-1",
        trigger_data={"attempt_number": 3},
        definition_snapshot={"nodes": [
            {"id": "render", "chat_projection": "progress"},
            {"id": "persist", "chat_projection": "hidden"},
        ]},
        variables={
            "project": {
                "state": {
                    "business_outcome": "needs_input",
                    "retry_state": {"observed_problem": "Browser disconnected"},
                },
            },
        },
        step_results={"render": {"status": "completed"}},
        execution_trace=[],
    )
    node = {"id": "render", "name": "Render", "type": "tool"}
    artifacts = [
        {"artifact_id": f"artifact-{index}", "api_key": "do-not-store"}
        for index in range(70)
    ]

    append_execution_trace(run, node=node, status="running")
    append_execution_trace(
        run,
        node=node,
        status="completed",
        result={
            "duration_ms": 12.5,
            "inputs": {"token": "input-secret", "prompt": "render"},
            "output": {
                "artifacts": artifacts,
                "items": [
                    {"subrun_id": "child-1"},
                    {"subrun_id": "child-1"},
                    {"subrun_id": "child-2"},
                ],
            },
        },
    )

    assert [entry["sequence"] for entry in run.execution_trace] == [1, 2]
    running, completed = run.execution_trace
    assert running["attempt_number"] == 3
    assert running["node_id"] == "render"
    assert running["node_name"] == "Render"
    assert running["node_type"] == "tool"
    assert running["status"] == "running"
    assert running["started_at"]
    assert completed["status"] == "completed"
    assert completed["completed_at"]
    assert completed["duration_ms"] == 12.5
    assert completed["input_summary"]["token"] == "[REDACTED]"
    assert completed["child_run_ids"] == ["child-1", "child-2"]
    assert len(completed["artifact_refs"]) == 64
    assert completed["artifact_refs"][0] == {"id": "artifact-0"}
    assert "do-not-store" not in json.dumps(completed["artifact_refs"])
    assert run.trigger_data["_workflow_history_summary"] == {
        "schema_version": 4,
        "business_outcome": "needs_input",
        "processed_count": 1,
        "total_count": 1,
        "workflow_steps": [{
            "id": "render",
            "name": "Render",
            "type": "tool",
            "status": "completed",
            "child_run_ids": ["child-1", "child-2"],
        }],
        "artifact_count": 64,
        "blocker": "Browser disconnected",
        "stages": [{"id": "render", "name": "Render", "type": "tool", "status": "completed"}],
        "current_stage": None,
        "deliverables": [],
        "publications": [],
        "result_title": None,
        "result_summary": None,
    }

    run.execution_trace = [{} for _ in range(2000)]
    append_execution_trace(run, node=node, status="failed", result={"error": "late"})
    assert len(run.execution_trace) == 2000

    run.execution_trace = []
    append_execution_trace(run, node={"id": "save_report", "name": "save_report", "type": "tool"}, status="running")
    assert run.execution_trace[0]["node_name"] == "Save report"


def test_step_names_shown_to_people_go_through_the_step_label():
    """A step named after its id (``tell_owner``) is shown as "Tell owner".

    Every place that names a step for people reads ``workflow_step_label``;
    falling back from ``name`` to the raw id shows people the id.
    """
    import re
    from pathlib import Path

    raw_fallback = re.compile(
        r"""(?:step|node)\.get\(["']name["']\)\s*or\s*(?:step\.get\(["']id["']\)|node\.get\(["']id["']\)"""
        r"""|step_id\b|node_id\b|run\.current_step_id\b|sid\b)"""
    )
    root = Path(__file__).resolve().parents[1]
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for folder in ("packages/core", "apps/api")
        for path in (root / folder).rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if raw_fallback.search(line)
    ]
    assert offenders == [], offenders




def test_workflow_run_trigger_data_drops_caller_supplied_history_summary():
    from types import SimpleNamespace

    from packages.core.services.workflow_service import _attempt_trigger_data

    workflow = SimpleNamespace(
        id="workflow-summary-boundary",
        name="Summary boundary",
        version=1,
        steps=[],
    )
    trigger_data = _attempt_trigger_data(workflow, {
        "request": "Create a product video",
        "_workspace_user_request": {"user_id": "forged-user"},
        "_workflow_history_summary": {
            "business_outcome": "accepted",
            "processed_count": 999,
            "total_count": 1,
            "artifact_count": 999,
            "blocker": "forged",
        },
    })

    assert trigger_data["request"] == "Create a product video"
    assert "_workflow_history_summary" not in trigger_data
    assert "_workspace_user_request" not in trigger_data


@pytest.mark.parametrize(
    "started_by,user_request,expected",
    [
        ("owner", {"user_id": "owner"}, {"_workspace_user_request": {"user_id": "owner"}}),
        ("owner", {"user_id": "other-user"}, {}),
        ("owner", None, {}),
        (None, {"user_id": None}, {}),
    ],
)
def test_child_workflow_inherits_only_matching_durable_user_request(started_by, user_request, expected):
    from packages.core.ai.workflow_runner import _child_workflow_user_request

    run = SimpleNamespace(
        started_by=started_by,
        trigger_data={"_workspace_user_request": user_request},
    )
    assert _child_workflow_user_request(run) == expected




def test_execution_trace_compacts_and_byte_caps_artifact_and_child_refs():
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import (
        MAX_ARTIFACT_REFS,
        TRACE_SUMMARY_BYTES,
        append_execution_trace,
    )

    run = WorkflowRun(
        id="run-large-refs",
        workflow_id="wf-large-refs",
        entity_id="entity-large-refs",
        trigger_data={},
        definition_snapshot={"nodes": [{"id": "render"}]},
        execution_trace=[],
    )
    artifacts = [
        {
            "id": f"artifact-{index}",
            "document_id": f"document-{index}",
            "fs_path": f"workspace/artifacts/{index}.mp4",
            "path": f"artifacts/{index}.mp4",
            "name": f"{'large-name-' * 50}{index}.mp4",
            "mime_type": "video/mp4",
            "status": "ready",
            "content": "payload" * 20_000,
            "authorization": "Bearer artifact-secret-value",
            "url": "https://example.test/file?access_token=url-secret-value",
        }
        for index in range(100)
    ]
    child_run_ids = [f"child-{index}-{'x' * 180}" for index in range(100)]
    child_run_ids.append("oversized-" + "x" * 20_000)

    append_execution_trace(
        run,
        node={"id": "render"},
        status="completed",
        result={
            "output": {
                "artifacts": artifacts,
                "child_run_ids": child_run_ids,
            }
        },
    )

    entry = run.execution_trace[0]
    allowed_fields = {
        "id",
        "document_id",
        "fs_path",
        "path",
        "name",
        "mime_type",
        "status",
    }
    assert len(entry["artifact_refs"]) <= MAX_ARTIFACT_REFS
    assert len(entry["child_run_ids"]) <= MAX_ARTIFACT_REFS
    assert len(json.dumps(entry["artifact_refs"]).encode("utf-8")) <= TRACE_SUMMARY_BYTES
    assert len(json.dumps(entry["child_run_ids"]).encode("utf-8")) <= TRACE_SUMMARY_BYTES
    assert all(set(ref) <= allowed_fields for ref in entry["artifact_refs"])
    assert "payload" not in json.dumps(entry["artifact_refs"])
    assert "artifact-secret-value" not in json.dumps(entry["artifact_refs"])
    assert "url-secret-value" not in json.dumps(entry["artifact_refs"])


def test_workflow_execution_steps_append_not_reached_nodes_and_share_counts():
    from types import SimpleNamespace

    from packages.core.services.workflow_run_trace import (
        workflow_execution_progress,
        workflow_execution_steps,
    )

    run = SimpleNamespace(
        definition_snapshot={
            "nodes": [
                {"id": "start", "name": "Start", "type": "trigger", "order": 0},
                {
                    "id": "unused_branch",
                    "name": "Unused branch",
                    "type": "stop",
                    "order": 1,
                },
                {"id": "review", "name": "Review", "type": "wait", "order": 2},
                {
                    "id": "publish",
                    "name": "Publish",
                    "type": "subworkflow",
                    "order": 3,
                },
                {"id": "done", "name": "Done", "type": "end", "order": 4},
            ],
        },
        execution_trace=[
            {"sequence": 1, "node_id": "start", "status": "running"},
            {"sequence": 2, "node_id": "start", "status": "completed"},
            {"sequence": 3, "node_id": "review", "status": "running"},
            {"sequence": 4, "node_id": "review", "status": "completed"},
            {
                "sequence": 5,
                "node_id": "publish",
                "status": "running",
                "child_run_ids": ["child-run-1"],
            },
            {
                "sequence": 6,
                "node_id": "publish",
                "status": "failed",
                "child_run_ids": ["child-run-1"],
            },
        ],
        step_results={
            "start": {"status": "completed"},
            "review": {"status": "completed"},
            "publish": {
                "status": "failed",
                "subrun_id": "child-run-1",
            },
        },
        status="failed",
        current_step_id="publish",
        retry_of_run_id=None,
    )

    steps = workflow_execution_steps(run)

    assert [(step["id"], step["status"]) for step in steps] == [
        ("start", "completed"),
        ("review", "completed"),
        ("publish", "failed"),
        ("unused_branch", "pending"),
        ("done", "pending"),
    ]
    assert steps[2]["child_run_ids"] == ["child-run-1"]
    # Only the stages count: the wait and the subworkflow. The start, the stop and the end are routing.
    assert workflow_execution_progress(steps) == {
        "processed_count": 2,
        "total_count": 2,
    }


def test_routing_steps_never_move_the_stage_total():
    from packages.core.services.workflow_run_trace import workflow_execution_progress

    def steps(stage_status: str, routing_status: str) -> list[dict]:
        return [
            {"id": "start", "type": "trigger", "status": routing_status},
            {"id": "check", "type": "condition", "status": routing_status},
            {"id": "draft", "type": "agent", "status": stage_status},
            {"id": "publish", "type": "tool", "status": "pending"},
            {"id": "done", "type": "end", "status": "pending"},
        ]

    before = workflow_execution_progress(steps("running", "pending"))
    during = workflow_execution_progress(steps("running", "completed"))
    after = workflow_execution_progress(steps("completed", "completed"))
    assert before == {"processed_count": 0, "total_count": 2}
    assert during == {"processed_count": 0, "total_count": 2}
    assert after == {"processed_count": 1, "total_count": 2}


def test_a_check_step_carries_its_answer_not_just_that_it_ran():
    from types import SimpleNamespace

    from packages.core.services.workflow_run_trace import workflow_execution_steps

    run = SimpleNamespace(
        definition_snapshot={"nodes": [
            {"id": "start", "name": "Start", "type": "trigger", "order": 0},
            {"id": "suitable", "name": "Check material is suitable", "type": "condition", "order": 1},
            {"id": "picked", "name": "Files picked?", "type": "condition", "order": 2},
        ]},
        execution_trace=[
            {"sequence": 1, "node_id": "start", "status": "completed"},
            {"sequence": 2, "node_id": "suitable", "status": "completed"},
            {"sequence": 3, "node_id": "picked", "status": "completed"},
        ],
        step_results={
            "start": {"status": "completed"},
            "suitable": {"status": "completed", "output": False, "condition_result": False},
            "picked": {"status": "completed", "output": True, "condition_result": True},
        },
        status="completed", current_step_id="picked", retry_of_run_id=None,
    )
    steps = {step["id"]: step for step in workflow_execution_steps(run)}
    assert (steps["suitable"]["status"], steps["suitable"]["answer"]) == ("completed", "no")
    assert steps["picked"]["answer"] == "yes"
    assert "answer" not in steps["start"]


def test_workflow_execution_steps_prepend_retry_receipts_and_keep_real_skips():
    from types import SimpleNamespace

    from packages.core.services.workflow_run_trace import workflow_execution_steps

    run = SimpleNamespace(
        definition_snapshot={
            "nodes": [
                {"id": "start", "name": "Start", "type": "trigger", "order": 0},
                {"id": "prepare", "name": "Prepare", "type": "agent", "order": 1},
                {"id": "optional", "name": "Optional", "type": "tool", "order": 2},
                {"id": "publish", "name": "Publish", "type": "agent", "order": 3},
            ],
        },
        execution_trace=[
            {"sequence": 1, "node_id": "optional", "status": "running"},
            {"sequence": 2, "node_id": "optional", "status": "skipped"},
            {"sequence": 3, "node_id": "publish", "status": "running"},
        ],
        # Retry inheritance is a set in the persistence service, so JSON key
        # order cannot be treated as execution order for these receipts.
        step_results={
            "prepare": {"status": "completed"},
            "start": {"status": "completed"},
            "optional": {"status": "skipped", "skipped": True},
        },
        status="running",
        current_step_id="publish",
        retry_of_run_id="prior-run",
    )

    assert [(step["id"], step["status"]) for step in workflow_execution_steps(run)] == [
        ("start", "completed"),
        ("prepare", "completed"),
        ("optional", "skipped"),
        ("publish", "running"),
    ]


@pytest.mark.asyncio
async def test_a_queued_run_goes_to_a_worker_and_a_create_only_run_does_not(client: AsyncClient, monkeypatch):
    """Staging 2026-11-03: MCP's wait=false created runs nothing published; recovery found them five minutes later."""
    from apps.api.routers import workflows as workflows_router

    published: list[str] = []
    monkeypatch.setattr(workflows_router.WorkflowRunner, "enqueue", staticmethod(lambda run_id: published.append(run_id) or True))
    headers = await _auth(client, "wfrunqueue")
    workflow = (await client.post("/api/v1/workflows", headers=headers,
                                  json={"name": "Queued run", "steps": _simple_steps(["prepare"])})).json()
    url = f"/api/v1/workflows/{workflow['id']}/run"

    created = await client.post(url, headers=headers, json={"execute": False})
    assert created.status_code == 201, created.text
    assert published == []

    queued = await client.post(url, headers=headers, json={"execute": False, "queue": True})
    assert queued.status_code == 201, queued.text
    assert published == [queued.json()["id"]]
    assert queued.json()["execution_trace"] == []


@pytest.mark.asyncio
async def test_started_workflow_run_exposes_definition_snapshot_and_empty_trace(
    client: AsyncClient,
):
    headers = await _auth(client, "wfrunsnapshot")
    steps = _simple_steps(["prepare"])
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Snapshot run", "steps": steps},
    )).json()

    response = await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["execution_trace"] == []
    assert run["definition_snapshot"]["workflow_id"] == workflow["id"]
    assert run["definition_snapshot"]["name"] == "Snapshot run"
    assert run["definition_snapshot"]["version"] == 1
    assert run["definition_snapshot"]["fingerprint"] == run["workflow_definition_fingerprint"]
    assert run["definition_snapshot"]["nodes"] == [
        {
            "id": step["id"],
            "name": step["name"],
            "type": step["type"],
            "order": order,
            "targets": step["next"],
            "chat_projection": "progress",
        }
        for order, step in enumerate(steps)
    ]


@pytest.mark.asyncio
async def test_runner_persists_structured_step_error_as_safe_text(
    client: AsyncClient,
    monkeypatch,
):
    import packages.core.ai.workflow_runner as workflow_runner_module
    import packages.core.database as db_module
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services.workflow_run_trace import TRACE_SUMMARY_BYTES

    headers = await _auth(client, "wfstructuredfailure")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Structured failure", "steps": _simple_steps(["fail"])},
    )).json()
    started = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    structured_error = {
        "code": "provider_failed",
        "authorization": "Bearer runner-secret-value-123456",
        "password": "runner-password-value",
        "message": "Provider rejected the request",
    }
    original_execute = WorkflowRunner._execute_step_safe

    async def execute_with_structured_failure(self, step, run, db):
        if step["id"] == "fail":
            run.current_step_id = step["id"]
            return {"status": "failed", "error": structured_error}
        return await original_execute(self, step, run, db)

    monkeypatch.setattr(workflow_runner_module, "async_session", db_module.async_session)
    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", execute_with_structured_failure)

    await WorkflowRunner().run(started["id"])

    failed = (await client.get(
        f"/api/v1/workflows/runs/{started['id']}",
        headers=headers,
    )).json()
    encoded_error = failed["error"].encode("utf-8")
    assert failed["status"] == "failed"
    assert isinstance(failed["error"], str)
    assert len(encoded_error) <= TRACE_SUMMARY_BYTES
    assert b"runner-secret-value-123456" not in encoded_error
    assert b"runner-password-value" not in encoded_error
    # The run headline is the error's readable message, never its JSON.
    assert failed["error"] == "Provider rejected the request"
    assert failed["step_results"]["fail"]["error"] == structured_error


@pytest.mark.asyncio
async def test_run_executes_private_snapshot_when_live_definition_changes(
    client: AsyncClient,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfrunsnapshotdrift")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Stable definition", "steps": _simple_steps(["work"])},
    )).json()
    started = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"name": "Changed definition"},
    )
    assert updated.status_code == 200, updated.text

    await WorkflowRunner().run(started["id"])
    detail = (await client.get(
        f"/api/v1/workflows/runs/{started['id']}",
        headers=headers,
    )).json()

    assert detail["status"] == "completed"
    assert detail["definition_snapshot"] == started["definition_snapshot"]
    assert detail["step_results"]["work"]["status"] == "completed"
    assert detail["execution_trace"]


@pytest.mark.asyncio
async def test_runner_claims_one_execution_across_concurrent_deliveries(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services import workflow_run_execution_claim as claim_service

    headers = await _auth(client, "wfconcurrentclaim")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Exclusive execution", "steps": _simple_steps(["work"])},
    )).json()
    started = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    original_execute = WorkflowRunner._execute_step_safe
    work_started = asyncio.Event()
    finish_work = asyncio.Event()
    work_calls = 0

    async def delayed_work(self, step, run, db):
        nonlocal work_calls
        if step["id"] == "work":
            work_calls += 1
            work_started.set()
            await finish_work.wait()
        return await original_execute(self, step, run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", delayed_work)

    first = asyncio.create_task(WorkflowRunner().run(started["id"]))
    await asyncio.wait_for(work_started.wait(), timeout=2)
    second_outcome = await asyncio.wait_for(
        WorkflowRunner().run(started["id"]),
        timeout=2,
    )
    finish_work.set()
    first_outcome = await asyncio.wait_for(first, timeout=2)

    assert work_calls == 1
    assert first_outcome == "executed"
    assert second_outcome == claim_service.CLAIM_HELD

    detail = (await client.get(
        f"/api/v1/workflows/runs/{started['id']}",
        headers=headers,
    )).json()
    assert detail["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("broker_accepts", [True, False])
async def test_binding_run_returns_immediately_and_executes_on_a_worker(
    client: AsyncClient,
    monkeypatch,
    broker_accepts: bool,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, f"wfbindingqueued{int(broker_accepts)}")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Queued binding", "steps": _simple_steps(["work"])},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={"workflow_id": workflow["id"], "trigger_type": "manual"},
    )).json()
    queued: list[str] = []
    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue",
        staticmethod(lambda run_id, *_a, **_k: queued.append(run_id) or broker_accepts),
    )

    async def inline_execution_forbidden(*_args, **_kwargs):
        pytest.fail("the request handler executed a workflow node")

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", inline_execution_forbidden)
    # ``execute`` defaults to true; binding runs still go to the worker queue.
    response = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={"variables": {}},
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert queued == [run["id"]]
    if broker_accepts:
        assert run["status"] in {"pending", "running"}
        assert run["step_results"] == {}
    else:
        assert run["status"] == "failed"
        assert run["error"] == "Workflow could not be queued. Please start it again."


@pytest.mark.asyncio
async def test_lost_owner_hands_replay_safe_step_to_exactly_one_successor(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import (
        WORKFLOW_RUN_EXECUTION_RESUMED_ELSEWHERE,
        WorkflowRunner,
    )
    from packages.core.services import workflow_run_execution_claim as claim_service

    headers = await _auth(client, "wfclaimheartbeat")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Heartbeat guarded", "steps": _simple_steps(["work"])},
    )).json()
    started = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    released: list[tuple[str, str]] = []
    work_started = asyncio.Event()
    original_owner_lost = False
    original_extend = claim_service._extend_postgres_execution_claim
    original_release = claim_service._release_postgres_execution_claim

    async def lose_original_owner_heartbeat(*args, **kwargs):
        nonlocal original_owner_lost
        # The runner's original ownership is lost, while the independent
        # recovery claim remains healthy long enough to settle the run.
        if work_started.is_set() and not original_owner_lost:
            original_owner_lost = True
            return False
        return await original_extend(*args, **kwargs)

    async def release(key: str, token: str):
        released.append((key, token))
        return await original_release(key, token)

    monkeypatch.setattr(
        claim_service,
        "_extend_postgres_execution_claim",
        lose_original_owner_heartbeat,
    )
    monkeypatch.setattr(
        claim_service,
        "_release_postgres_execution_claim",
        release,
    )
    monkeypatch.setattr(
        claim_service,
        "_WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS",
        0.01,
    )
    never_finish = asyncio.Event()
    original_execute = WorkflowRunner._execute_step_safe

    async def blocking_work(self, step, run, db):
        if step["id"] == "work":
            work_started.set()
            await never_finish.wait()
        return await original_execute(self, step, run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", blocking_work)
    queued: list[dict] = []
    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue",
        staticmethod(lambda run_id, *_a, **kwargs: queued.append(
            {"run_id": run_id, **kwargs}
        ) or True),
    )

    assert await WorkflowRunner().run(started["id"]) == (
        WORKFLOW_RUN_EXECUTION_RESUMED_ELSEWHERE
    )

    # The interrupted transform is replay-safe: the run stays active and one
    # worker delivery receives the recovery owner's retained lease.
    detail = (await client.get(
        f"/api/v1/workflows/runs/{started['id']}",
        headers=headers,
    )).json()
    assert detail["status"] == "running"
    assert detail["error"] is None
    recovery = detail["trigger_data"]["_execution_recovery"]
    assert recovery["attempts"] == 1
    assert recovery["interrupted_step_ids"] == ["work"]
    assert len(queued) == 1 and queued[0]["run_id"] == started["id"]
    handoff_token = queued[0]["claim_handoff_token"]
    assert handoff_token not in json.dumps(detail), "lease tokens stay private"
    # Only the lost owner released; the recovery lease is retained.
    assert len(released) == 1 and released[0][1] != handoff_token

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", original_execute)
    monkeypatch.setattr(
        claim_service, "_extend_postgres_execution_claim", original_extend,
    )
    adopted = asyncio.Event()
    duplicate_outcome: list[str] = []

    async def adopt_then_redeliver(self, step, run, db):
        if step["id"] == "work" and not adopted.is_set():
            adopted.set()
            # A duplicate delivery of the same handoff cannot adopt it again.
            duplicate_outcome.append(await WorkflowRunner().run(
                started["id"], claim_handoff_token=handoff_token,
            ))
        return await original_execute(self, step, run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", adopt_then_redeliver)
    assert await WorkflowRunner().run(
        started["id"], claim_handoff_token=handoff_token,
    ) == "executed"
    assert duplicate_outcome == [claim_service.CLAIM_HELD]

    detail = (await client.get(
        f"/api/v1/workflows/runs/{started['id']}",
        headers=headers,
    )).json()
    assert detail["status"] == "completed", detail
    assert detail["step_results"]["work"]["status"] == "completed"
    assert detail["variables"]["work"]["work"] == "done"


@pytest.mark.asyncio
async def test_claim_loss_settlement_cannot_fail_a_successor_owner(
    client: AsyncClient,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services.workflow_run_execution_claim import (
        workflow_run_execution_claim,
    )

    headers = await _auth(client, "wfclaimsuccessor")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Successor guarded", "steps": _simple_steps(["work"])},
    )).json()
    started = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    async with workflow_run_execution_claim(started["id"]) as successor:
        assert successor
        assert await WorkflowRunner._recover_lost_execution_claim(started["id"]) is None

        detail = (await client.get(
            f"/api/v1/workflows/runs/{started['id']}",
            headers=headers,
        )).json()
        assert detail["status"] == "running"
        assert detail["error"] is None


@pytest.mark.asyncio
async def test_runner_reports_claim_held_when_loss_settlement_has_a_successor(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    from packages.core.ai import workflow_runner as runner_module
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services.workflow_run_execution_claim import (
        CLAIM_HELD as WORKFLOW_RUN_EXECUTION_CLAIM_HELD,
        WorkflowRunExecutionClaimLost,
    )

    class Claim:
        def raise_if_lost(self):
            return None

        def mark_terminal_committed(self):
            return None

    @asynccontextmanager
    async def granted_claim(_run_id):
        yield Claim()

    async def lose_claim(*_args, **_kwargs):
        raise WorkflowRunExecutionClaimLost("lost")

    async def successor_owns_recovery(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        runner_module,
        "workflow_run_execution_claim",
        granted_claim,
    )
    monkeypatch.setattr(WorkflowRunner, "_run_claimed", lose_claim)
    monkeypatch.setattr(
        WorkflowRunner,
        "_recover_lost_execution_claim",
        staticmethod(successor_owns_recovery),
    )

    assert await WorkflowRunner().run("run-1") == WORKFLOW_RUN_EXECUTION_CLAIM_HELD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_ttl_seconds", "outage_attempts", "lost"),
    [
        # 2026-10-13: three refused renewals within 4s of a 660s lease used to
        # cancel a 25-minute workflow step. Unreachable is not lost.
        (660, 8, False),
        # No renewal confirmed before the lease's own deadline: stop working.
        (0.2, None, True),
    ],
)
async def test_execution_claim_survives_renewal_outage_until_its_lease_deadline(
    monkeypatch,
    lease_ttl_seconds,
    outage_attempts,
    lost,
):
    from packages.core.services import workflow_run_execution_claim as claim_service

    renewal_succeeded = asyncio.Event()
    released: list[tuple[str, str]] = []
    renewal_results = iter([None] * outage_attempts + [True] * 100 if outage_attempts else [])

    async def acquire(*_args, **_kwargs):
        return True

    async def extend(*_args, **_kwargs):
        result = next(renewal_results, None)
        if result is True:
            renewal_succeeded.set()
        return result

    async def release(key: str, token: str):
        released.append((key, token))
        return True

    monkeypatch.setattr(
        claim_service,
        "_acquire_postgres_execution_claim",
        acquire,
    )
    monkeypatch.setattr(
        claim_service,
        "_extend_postgres_execution_claim",
        extend,
    )
    monkeypatch.setattr(
        claim_service,
        "_release_postgres_execution_claim",
        release,
    )
    monkeypatch.setattr(
        claim_service,
        "_WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        claim_service,
        "_WORKFLOW_RUN_EXECUTION_RENEWAL_RETRY_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        claim_service,
        "WORKFLOW_RUN_EXECUTION_LEASE_TTL_SECONDS",
        lease_ttl_seconds,
    )

    if lost:
        with pytest.raises(claim_service.WorkflowRunExecutionClaimLost):
            async with claim_service.workflow_run_execution_claim(
                "workflow-renewal-deadline"
            ):
                await asyncio.sleep(5)
    else:
        async with claim_service.workflow_run_execution_claim(
            "workflow-transient-renewal"
        ) as claim:
            await asyncio.wait_for(renewal_succeeded.wait(), timeout=5)
            assert claim.lost.is_set() is False

    assert len(released) == 1


@pytest.mark.asyncio
async def test_expired_execution_claim_is_reclaimed_without_stale_owner_release(
    db_session,
):
    import packages.core.database as db_module
    from packages.core.models.execution_claim import RuntimeExecutionClaim
    from packages.core.services import workflow_run_execution_claim as claim_service

    claim_key = "workflow-run:expired-claim"
    stale_token = "0" * 26
    successor_token = "1" * 26
    db_session.add(
        RuntimeExecutionClaim(
            claim_key=claim_key,
            claim_token=stale_token,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    await db_session.commit()

    assert await claim_service._acquire_postgres_execution_claim(
        claim_key,
        successor_token,
        lease_ttl_seconds=60,
    ) is True
    assert await claim_service._release_postgres_execution_claim(
        claim_key,
        stale_token,
    ) is False

    async with db_module.async_session() as verification_db:
        persisted = await verification_db.get(RuntimeExecutionClaim, claim_key)
        assert persisted is not None
        assert persisted.claim_token == successor_token

    assert await claim_service._release_postgres_execution_claim(
        claim_key,
        successor_token,
    ) is True


def test_retry_variables_restore_inherited_shared_checkpoint_output():
    from packages.core.services.workflow_service import _retry_variables

    workflow = SimpleNamespace(steps=[
        {
            "id": "load_project",
            "config": {"output_var": "project"},
            "next": ["recover"],
        },
        {
            "id": "recover",
            "config": {"output_var": "recovery_result"},
            "next": ["save_project"],
        },
        {
            "id": "save_project",
            "config": {"output_var": "project"},
            "next": ["end"],
        },
    ])
    prior = SimpleNamespace(
        variables={"project": {"revision": 2}, "recovery_result": {"complete": False}},
        step_results={
            "load_project": {"status": "completed", "output": {"revision": 1}},
            "recover": {"status": "completed", "output": {"complete": False}},
            "save_project": {"status": "completed", "output": {"revision": 2}},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"load_project"},
        {"retry_segment_ids": ["SEG-002"]},
    )

    assert variables["project"] == {"revision": 1}
    assert "recovery_result" not in variables
    assert variables["retry_segment_ids"] == ["SEG-002"]


def test_retry_variables_restore_structured_wait_response_without_replacing_receipt_output():
    from packages.core.services.workflow_service import _retry_variables

    materials = {"primary_audio": [{"document_id": "audio-1"}]}
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["materials"]},
        {
            "id": "materials",
            "type": "wait",
            "config": {
                "wait_type": "input",
                "response_variable": "materials_input",
                "output_var": "materials_record",
                "input_schema": {
                    "type": "object",
                    "required": ["primary_audio"],
                    "properties": {
                        "primary_audio": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["document_id"],
                                "properties": {"document_id": {"type": "string"}},
                            },
                        },
                    },
                },
            },
            "next": ["publish"],
        },
        {"id": "publish", "type": "tool", "config": {}, "next": []},
    ])
    prior = SimpleNamespace(
        variables={"materials_input": materials},
        step_results={
            "start": {"status": "completed", "output": {}},
            "materials": {
                "status": "completed",
                "output": "Choose the primary audio.",
                "workflow_response": {"choice": "submit", "payload": {"materials_input": materials}},
            },
            "publish": {"status": "failed"},
        },
    )

    variables = _retry_variables(workflow, prior, {"start", "materials"}, None, "publish")

    assert variables["materials"] == "Choose the primary audio."
    assert variables["materials_record"] == "Choose the primary audio."
    assert variables["materials_input"] == materials

    same_name_workflow = SimpleNamespace(steps=deepcopy(workflow.steps))
    same_name_config = same_name_workflow.steps[1]["config"]
    same_name_config["response_variable"] = "materials"
    same_name_config["output_var"] = "materials"
    same_name_prior = SimpleNamespace(
        variables={"materials": materials},
        step_results={
            "start": {"status": "completed", "output": {}},
            "materials": {
                "status": "completed",
                "output": "Choose the primary audio.",
                "workflow_response": {"choice": "submit", "payload": {"materials": materials}},
            },
            "publish": {"status": "failed"},
        },
    )

    same_name_variables = _retry_variables(
        same_name_workflow, same_name_prior, {"start", "materials"}, None, "publish",
    )

    assert same_name_variables["materials"] == materials


def test_retry_variables_replay_collection_wait_clears_prior_customer_answers():
    from packages.core.services.workflow_service import _retry_variables

    workflow = SimpleNamespace(steps=[
        {
            "id": "start",
            "type": "trigger",
            "config": {"run_inputs": [{"key": "workspace_id"}]},
            "next": ["collect_profile"],
        },
        {
            "id": "collect_profile",
            "type": "wait",
            "config": {
                "wait_type": "human_input",
                "response_variable": "onboarding_response",
                "output_var": "onboarding_answers",
                "collection_fields": [
                    {"key": "company_name"},
                    {"key": "team_size"},
                ],
            },
            "next": ["prepare_workspace"],
        },
        {
            "id": "prepare_workspace",
            "type": "agent",
            "config": {"output_var": "proposal"},
            "next": [],
        },
        {
            "id": "unselected_sibling",
            "type": "transform",
            "config": {"set": {"sibling_value": "unused"}},
            "next": [],
        },
    ])
    prior = SimpleNamespace(
        variables={
            "workspace_id": "workspace-onboarding",
            "company_name": "Old Company",
            "team_size": "12",
            "onboarding_response": {
                "payload": {"inputs": {"company_name": "Old Company", "team_size": "12"}},
            },
            "onboarding_answers": {"company_name": "Old Company", "team_size": "12"},
            "sibling_value": "keep-this-unselected-value",
            "proposal": {"status": "failed"},
        },
        step_results={
            "start": {"status": "completed", "output": {}},
            "collect_profile": {
                "status": "completed",
                "output": {"company_name": "Old Company", "team_size": "12"},
                "collection_values": {"company_name": "Old Company", "team_size": "12"},
            },
            "prepare_workspace": {"status": "failed"},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start"},
        None,
        "collect_profile",
    )

    assert variables["workspace_id"] == "workspace-onboarding"
    assert variables["sibling_value"] == "keep-this-unselected-value"
    assert "onboarding_response" not in variables
    assert "onboarding_answers" not in variables
    assert "company_name" not in variables
    assert "team_size" not in variables


def test_retry_variables_restore_inherited_automatic_and_named_outputs():
    from packages.core.services.workflow_service import _retry_variables

    workflow = SimpleNamespace(steps=[
        {
            "id": "upload",
            "config": {
                "outputs": [{
                    "key": "published_video_id",
                    "type": "text",
                    "value": "{{upload.video_id}}",
                }],
            },
            "next": ["metadata"],
        },
        {"id": "metadata", "config": {}, "next": []},
    ])
    upload_receipt = {
        "success": True,
        "video_id": "LC13h459jjI",
        "watch_url": "https://www.youtube.com/watch?v=LC13h459jjI",
    }
    prior = SimpleNamespace(
        variables={},
        step_results={"upload": {"status": "completed", "output": upload_receipt}},
    )

    variables = _retry_variables(workflow, prior, {"upload"}, None)

    assert variables["upload"] == upload_receipt
    assert variables["published_video_id"] == "LC13h459jjI"


def test_retry_variables_restore_shared_output_in_execution_order_not_definition_order():
    from packages.core.services.workflow_service import _retry_variables

    # ``restore`` is intentionally declared after ``plan`` although it runs
    # first. Both write the same output variable; a retry must retain the
    # result from the later, selected producer.
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "config": {}, "next": ["restore"]},
        {
            "id": "plan",
            "type": "tool",
            "config": {"output_var": "brief"},
            "next": ["publish"],
        },
        {"id": "publish", "type": "agent", "config": {}, "next": []},
        {
            "id": "restore",
            "type": "tool",
            "config": {"output_var": "brief"},
            "next": ["plan"],
        },
    ])
    prior = SimpleNamespace(
        variables={"brief": {"ready": True, "source": "plan"}},
        step_results={
            "start": {"status": "completed", "output": {}},
            "restore": {"status": "completed", "output": {"ready": False, "source": "restore"}},
            "plan": {"status": "completed", "output": {"ready": True, "source": "plan"}},
            "publish": {"status": "failed"},
        },
    )

    variables = _retry_variables(workflow, prior, {"start", "restore", "plan"}, None, "publish")

    assert variables["brief"] == {"ready": True, "source": "plan"}


def test_retry_inherits_completed_ancestors_through_unexecuted_condition():
    from packages.core.services.workflow_service import retry_inherited_step_ids

    steps = [
        {"id": "start", "type": "trigger", "next": ["producer"]},
        {"id": "producer", "type": "tool", "next": ["gate"]},
        {
            "id": "gate",
            "type": "condition",
            "next": ["blocked"],
            "true_next": ["publish"],
            "false_next": ["blocked"],
        },
        {"id": "blocked", "type": "stop", "next": []},
        {"id": "publish", "type": "tool", "next": []},
    ]
    prior_results = {
        "start": {"status": "completed"},
        "producer": {"status": "completed", "output": {"video_id": "video-1"}},
    }

    assert retry_inherited_step_ids(steps, prior_results, "publish") == {
        "start",
        "producer",
    }


def test_retry_inherits_completed_ancestors_through_true_condition():
    from packages.core.services.workflow_service import retry_inherited_step_ids

    steps = [
        {"id": "start", "type": "trigger", "next": ["producer"]},
        {"id": "producer", "type": "tool", "next": ["gate"]},
        {
            "id": "gate",
            "type": "condition",
            "next": ["blocked"],
            "true_next": ["publish"],
            "false_next": ["blocked"],
        },
        {"id": "blocked", "type": "stop", "next": []},
        {"id": "publish", "type": "tool", "next": []},
    ]
    prior_results = {
        "start": {"status": "completed"},
        "producer": {"status": "completed", "output": {"video_id": "video-1"}},
        "gate": {"status": "completed", "condition_result": True},
    }

    assert retry_inherited_step_ids(steps, prior_results, "publish") == {
        "start",
        "producer",
        "gate",
    }


def test_retry_variables_remove_stale_internal_stage_outputs():
    from packages.core.services.workflow_service import _retry_variables

    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["prepare"]},
        {
            "id": "prepare",
            "type": "stage",
            "config": {
                "operations": [{
                    "id": "load",
                    "type": "transform",
                    "config": {"set": {"project": "ready"}},
                }]
            },
            "next": ["produce"],
        },
        {
            "id": "produce",
            "type": "stage",
            "config": {
                "operations": [
                    {
                        "id": "collect",
                        "type": "transform",
                        "config": {"set": {"collected": True}},
                    },
                    {
                        "id": "render",
                        "type": "agent",
                        "config": {"output_var": "production_result"},
                    },
                ]
            },
            "next": ["review"],
        },
        {
            "id": "review",
            "type": "stage",
            "config": {
                "operations": [{
                    "id": "quality",
                    "type": "transform",
                    "config": {"set": {"quality_result": "stale"}},
                }]
            },
            "next": [],
        },
    ])
    prior = SimpleNamespace(
        variables={
            "project": "ready",
            "load": {"project": "ready"},
            "collected": True,
            "collect": {"collected": True},
            "render": {"status": "failed"},
            "production_result": {"status": "stale"},
            "quality": {"quality_result": "stale"},
            "quality_result": "stale",
            "__stage_execution": {
                "prepare": {
                    "status": "completed",
                    "operation_results": {"load": {"status": "completed"}},
                },
                "produce": {
                    "status": "failed",
                    "failed_operation_id": "render",
                    "operation_results": {
                        "collect": {"status": "completed"},
                        "render": {"status": "failed"},
                    },
                },
                "review": {
                    "status": "completed",
                    "operation_results": {"quality": {"status": "completed"}},
                },
            },
        },
        step_results={
            "start": {"status": "completed"},
            "prepare": {"status": "completed"},
            "produce": {"status": "failed"},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start", "prepare"},
        {"corrected_input": "ready"},
        "produce",
    )

    assert variables["project"] == "ready"
    assert variables["load"] == {"project": "ready"}
    assert variables["collected"] is True
    assert variables["collect"] == {"collected": True}
    assert variables["corrected_input"] == "ready"
    assert set(variables["__stage_execution"]) == {"produce"}
    for stale_key in (
        "render",
        "production_result",
        "quality",
        "quality_result",
    ):
        assert stale_key not in variables


def test_retry_variables_preserve_declared_input_when_stage_output_reuses_its_name():
    from packages.core.services.workflow_service import _retry_variables

    request = {
        "product_name": "Manor Workspace",
        "start_url": "http://localhost:3010/workspaces",
        "must_show": ["Workspace list", "Marketplace"],
    }
    workflow = SimpleNamespace(steps=[
        {
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [{
                    "key": "start_url",
                    "target": "request.start_url",
                }],
            },
            "next": ["prepare"],
        },
        {
            "id": "prepare",
            "type": "stage",
            "config": {
                "operations": [{
                    "id": "normalize_request",
                    "type": "agent",
                    "config": {"output_var": "request"},
                }],
            },
            "next": ["check_browser"],
        },
    ])
    prior = SimpleNamespace(
        variables={
            "request": request,
            "normalize_request": {"status": "failed"},
            "__stage_execution": {
                "prepare": {
                    "status": "failed",
                    "failed_operation_id": "normalize_request",
                    "operation_results": {
                        "normalize_request": {"status": "failed"},
                    },
                },
            },
        },
        step_results={
            "start": {"status": "completed"},
            "prepare": {"status": "failed"},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start"},
        None,
        "prepare",
    )

    assert variables["request"] == request
    assert "normalize_request" not in variables


def test_retry_variables_restart_a_completed_target_stage_from_its_entry():
    from packages.core.services.workflow_service import _retry_variables

    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["check_browser"]},
        {
            "id": "check_browser",
            "type": "stage",
            "config": {
                "operations": [{
                    "id": "browser_preflight",
                    "type": "agent",
                    "config": {"output_var": "browser_preflight"},
                }],
            },
            "next": ["discover"],
        },
        {"id": "discover", "type": "stage", "config": {"operations": []}, "next": []},
    ])
    prior = SimpleNamespace(
        variables={
            "browser_preflight": {"available": False},
            "__stage_execution": {
                "check_browser": {
                    "status": "completed",
                    "operation_results": {
                        "browser_preflight": {"status": "completed"},
                    },
                },
            },
        },
        step_results={
            "start": {"status": "completed"},
            "check_browser": {"status": "completed"},
            "discover": {"status": "completed", "next_override": []},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start"},
        None,
        "check_browser",
    )

    assert "__stage_execution" not in variables
    assert "browser_preflight" not in variables


def test_retry_variables_restore_outputs_from_an_inherited_stage_operation():
    from packages.core.services.workflow_service import _retry_variables

    created_project = {"project_id": "project-1", "revision": 0}
    latest_project = {"project_id": "project-1", "revision": 4}
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["prepare_project"]},
        {
            "id": "prepare_project",
            "type": "stage",
            "config": {
                "operations": [
                    {
                        "id": "create_project",
                        "type": "workflow_project",
                        "config": {"output_var": "project"},
                    },
                    {
                        "id": "prepare_shared",
                        "type": "transform",
                        "config": {
                            "set": {"shared_value": "inherited"},
                            "outputs": [{
                                "key": "named_value",
                                "value": "{{prepare_shared.shared_value}}",
                            }],
                        },
                    },
                ],
            },
            "next": ["check_browser"],
        },
        {
            "id": "check_browser",
            "type": "stage",
            "config": {"operations": []},
            "next": ["discover"],
        },
        {
            "id": "discover",
            "type": "stage",
            "config": {
                "operations": [
                    {
                        "id": "mark_discovering",
                        "type": "workflow_project",
                        "config": {"output_var": "project"},
                    },
                    {
                        "id": "overwrite_shared",
                        "type": "transform",
                        "config": {
                            "set": {"shared_value": "descendant"},
                            "outputs": [{
                                "key": "named_value",
                                "value": "descendant-named",
                            }],
                        },
                    },
                ],
            },
            "next": [],
        },
    ])
    prior = SimpleNamespace(
        variables={
            "project": latest_project,
            "create_project": created_project,
            "prepare_shared": {
                "project": created_project,
                "shared_value": "inherited",
            },
            "shared_value": "descendant",
            "named_value": "descendant-named",
            "mark_discovering": latest_project,
            "overwrite_shared": {
                "project": latest_project,
                "shared_value": "descendant",
                "named_value": "descendant-named",
            },
            "__stage_execution": {
                "prepare_project": {
                    "status": "completed",
                    "operation_results": {
                        "create_project": {"status": "completed"},
                        "prepare_shared": {"status": "completed"},
                    },
                },
                "discover": {
                    "status": "completed",
                    "operation_results": {
                        "mark_discovering": {"status": "completed"},
                        "overwrite_shared": {"status": "completed"},
                    },
                },
            },
        },
        step_results={
            "start": {"status": "completed"},
            "prepare_project": {"status": "completed"},
            "check_browser": {"status": "completed"},
            "discover": {"status": "completed", "next_override": []},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start", "prepare_project"},
        None,
        "check_browser",
    )

    assert variables["project"] == created_project
    assert variables["shared_value"] == "inherited"
    assert variables["named_value"] == "inherited"


def test_retry_variables_restore_inherited_stage_outputs_across_linked_attempts():
    from packages.core.services.workflow_service import _retry_variables

    created_project = {"project_id": "project-1", "revision": 0}
    latest_project = {"project_id": "project-1", "revision": 4}
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["prepare_project"]},
        {
            "id": "prepare_project",
            "type": "stage",
            "config": {
                "operations": [{
                    "id": "create_project",
                    "type": "workflow_project",
                    "config": {"output_var": "project"},
                }],
            },
            "next": ["check_browser"],
        },
        {
            "id": "check_browser",
            "type": "stage",
            "config": {"operations": []},
            "next": ["discover"],
        },
        {
            "id": "discover",
            "type": "stage",
            "config": {
                "operations": [{
                    "id": "mark_discovering",
                    "type": "workflow_project",
                    "config": {"output_var": "project"},
                }],
            },
            "next": [],
        },
    ])
    prior = SimpleNamespace(
        variables={
            "project": latest_project,
            "create_project": created_project,
            "mark_discovering": latest_project,
            "__stage_execution": {
                "discover": {
                    "status": "completed",
                    "operation_results": {
                        "mark_discovering": {"status": "completed"},
                    },
                },
            },
        },
        step_results={
            "start": {"status": "completed"},
            "prepare_project": {"status": "completed"},
            "check_browser": {"status": "completed"},
            "discover": {"status": "completed", "next_override": []},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start", "prepare_project"},
        None,
        "check_browser",
    )

    assert variables["project"] == created_project


def test_retry_variables_do_not_flatten_a_completed_stage_output_into_cleanup_keys():
    from packages.core.services.workflow_service import _retry_variables

    schema = {"type": "object", "required": ["request"]}
    workflow = SimpleNamespace(steps=[
        {"id": "start", "type": "trigger", "next": ["retry_here"]},
        {"id": "retry_here", "type": "stage", "config": {"operations": []}, "next": ["later"]},
        {
            "id": "later",
            "type": "stage",
            "config": {
                "operations": [{
                    "id": "build_handoff",
                    "type": "transform",
                    "config": {"set": {"input": {"business_outcome": "needs_input"}}},
                }],
            },
            "next": [],
        },
    ])
    prior = SimpleNamespace(
        variables={
            "request": {"product_name": "Manor"},
            "product_video_project_schema": schema,
            "runtime_context": {"workspace_id": "workspace-1"},
            "input": {"business_outcome": "needs_input"},
            "build_handoff": {
                "request": {"product_name": "Manor"},
                "product_video_project_schema": schema,
                "runtime_context": {"workspace_id": "workspace-1"},
                "input": {"business_outcome": "needs_input"},
            },
            "__stage_execution": {
                "later": {
                    "status": "completed",
                    "operation_results": {
                        "build_handoff": {"status": "completed"},
                    },
                },
            },
        },
        step_results={
            "start": {"status": "completed"},
            "retry_here": {"status": "completed"},
            "later": {
                "status": "completed",
                "next_override": [],
                "output": {
                    "request": {"product_name": "Manor"},
                    "product_video_project_schema": schema,
                    "runtime_context": {"workspace_id": "workspace-1"},
                    "input": {"business_outcome": "needs_input"},
                },
            },
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start"},
        None,
        "retry_here",
    )

    assert variables["request"] == {"product_name": "Manor"}
    assert variables["product_video_project_schema"] == schema
    assert variables["runtime_context"] == {"workspace_id": "workspace-1"}
    assert "input" not in variables


def test_retry_variables_do_not_flatten_subworkflow_output_into_cleanup_keys():
    from packages.core.services.workflow_service import _retry_variables

    schema = {"type": "object", "required": ["artifact"]}
    runtime_context = {"workspace_id": "workspace-1"}
    child_output = {
        "stickman_video_project_schema": schema,
        "runtime_context": runtime_context,
        "video_project": {"project_id": "project-1"},
    }
    workflow = SimpleNamespace(
        variables={"stickman_video_project_schema": schema},
        steps=[
            {"id": "start", "type": "trigger", "next": ["create_video"]},
            {
                "id": "create_video",
                "type": "subworkflow",
                "config": {"output_var": "video_generation"},
                "next": ["publish_video"],
            },
            {"id": "publish_video", "type": "subworkflow", "config": {}, "next": []},
        ],
    )
    prior = SimpleNamespace(
        variables={
            "stickman_video_project_schema": schema,
            "runtime_context": runtime_context,
            "create_video": child_output,
            "video_generation": child_output,
        },
        step_results={
            "start": {"status": "completed"},
            "create_video": {"status": "completed", "output": child_output},
            "publish_video": {"status": "failed"},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start"},
        None,
        "create_video",
    )

    assert variables["stickman_video_project_schema"] == schema
    assert variables["runtime_context"] == runtime_context
    assert "create_video" not in variables
    assert "video_generation" not in variables


def test_retry_variables_restore_missing_definition_defaults():
    from packages.core.services.workflow_service import _retry_variables

    schema = {"type": "object", "required": ["artifact"]}
    runtime_context = {"workspace_id": "workspace-1"}
    child_output = {"video_project": {"project_id": "project-1"}}
    workflow = SimpleNamespace(
        variables={
            "stickman_video_project_schema": schema,
            "runtime_context": runtime_context,
        },
        steps=[
            {"id": "start", "type": "trigger", "next": ["create_video"]},
            {
                "id": "create_video",
                "type": "subworkflow",
                "config": {"output_var": "video_generation"},
                "next": ["publish_video"],
            },
            {"id": "publish_video", "type": "subworkflow", "config": {}, "next": []},
        ],
    )
    prior = SimpleNamespace(
        variables={
            "create_video": child_output,
            "video_generation": child_output,
        },
        step_results={
            "start": {"status": "completed"},
            "create_video": {"status": "completed", "output": child_output},
            "publish_video": {"status": "failed"},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start"},
        None,
        "create_video",
    )

    assert variables["stickman_video_project_schema"] == schema
    assert variables["runtime_context"] == runtime_context
    assert "create_video" not in variables
    assert "video_generation" not in variables


def test_retry_variables_refresh_stale_definition_schema_without_replacing_inputs():
    from packages.core.services.workflow_service import _retry_variables

    current_schema = {
        "type": "object",
        "properties": {
            "artifact": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "filename": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    }
    stale_schema = deepcopy(current_schema)
    del stale_schema["properties"]["artifact"]["properties"]["document_id"]
    generated_video = {"video_source": "Workspaces/example/final/video.mp4"}
    workflow = SimpleNamespace(
        variables={
            "stickman_video_project_schema": current_schema,
            "target_duration": 120,
        },
        steps=[
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "run_inputs": [
                        {
                            "key": "target_duration",
                            "target": "target_duration",
                        },
                    ],
                },
                "next": ["generate_video"],
            },
            {
                "id": "generate_video",
                "type": "tool",
                "config": {"output_var": "video_generation"},
                "next": ["claim_video_project"],
            },
            {
                "id": "claim_video_project",
                "type": "tool",
                "config": {},
                "next": [],
            },
        ],
    )
    prior = SimpleNamespace(
        variables={
            "stickman_video_project_schema": stale_schema,
            "target_duration": 20,
            "generate_video": generated_video,
            "video_generation": generated_video,
        },
        step_results={
            "start": {"status": "completed"},
            "generate_video": {
                "status": "completed",
                "output": generated_video,
            },
            "claim_video_project": {"status": "failed"},
        },
    )

    variables = _retry_variables(
        workflow,
        prior,
        {"start", "generate_video"},
        None,
        "claim_video_project",
    )

    assert variables["stickman_video_project_schema"] == current_schema
    assert variables["target_duration"] == 20
    assert variables["generate_video"] == generated_video
    assert variables["video_generation"] == generated_video


def test_retry_variable_patch_resolves_run_inputs_from_nested_targets():
    from packages.core.services.workflow_service import _validate_retry_variable_patch

    run_inputs = [
        {
            "key": "product_name",
            "type": "string",
            "required": True,
            "target": "request.product_name",
        },
        {
            "key": "start_url",
            "type": "string",
            "required": True,
            "target": "request.start_url",
            "schema": {"type": "string", "format": "uri"},
        },
        {
            "key": "must_show",
            "type": "json",
            "required": True,
            "target": "request.must_show",
            "schema": {"type": "array", "minItems": 1},
        },
    ]
    workflow = SimpleNamespace(
        id="workflow-product-video",
        name="Create product video",
        description="",
        workspace_id="workspace-product-video",
        variables={"request": {}},
        steps=[
            {
                "id": "start",
                "type": "trigger",
                "config": {"run_inputs": run_inputs},
            },
        ],
    )
    prior = SimpleNamespace(
        workspace_id="workspace-product-video",
        variables={
            "request": {
                "product_name": "Manor Workspace",
                "start_url": "http://localhost:3010/workspaces",
                "must_show": ["Workspace list", "Marketplace"],
            },
        },
    )

    normalized = _validate_retry_variable_patch(
        workflow,
        prior,
        {"request": {"must_show": ["Workspace list", "Workspace details"]}},
    )

    assert normalized["product_name"] == "Manor Workspace"
    assert normalized["start_url"] == "http://localhost:3010/workspaces"
    assert normalized["must_show"] == ["Workspace list", "Workspace details"]
    assert normalized["request"] == {
        "product_name": "Manor Workspace",
        "start_url": "http://localhost:3010/workspaces",
        "must_show": ["Workspace list", "Workspace details"],
    }


def test_retry_variable_patch_accepts_frozen_execution_view_without_description():
    from packages.core.services.workflow_service import _validate_retry_variable_patch

    workflow = SimpleNamespace(
        id="workflow-release",
        name="Prepare release",
        variables={"change_request": ""},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {"run_inputs": [{
                "key": "change_request",
                "type": "string",
                "schema": {"type": "string"},
            }]},
        }],
    )
    prior = SimpleNamespace(workspace_id="workspace-release", variables={})

    assert _validate_retry_variable_patch(
        workflow,
        prior,
        {"change_request": "formatting feedback"},
    ) == {"change_request": "formatting feedback"}


@pytest.mark.asyncio
async def test_retry_refreshes_the_latest_durable_project_revision(db_session):
    from packages.core.services.workflow_project_service import (
        create_workflow_project,
        patch_workflow_project,
    )
    from packages.core.services.workflow_service import (
        _refresh_retry_workflow_project,
    )

    project = await create_workflow_project(
        db_session,
        entity_id="entity-retry-project",
        workspace_id="workspace-retry-project",
        project_type="product_video",
        state={"business_outcome": "in_progress", "phase": "request"},
        created_by="user-retry-project",
        last_run_id="run-1",
    )
    project = await patch_workflow_project(
        db_session,
        project_id=project.id,
        entity_id="entity-retry-project",
        workspace_id="workspace-retry-project",
        expected_revision=0,
        state={"business_outcome": "needs_input", "phase": "discovery"},
        last_run_id="run-1",
    )
    await db_session.commit()
    prior = SimpleNamespace(
        entity_id="entity-retry-project",
        workspace_id="workspace-retry-project",
    )

    variables = await _refresh_retry_workflow_project(
        db_session,
        prior,
        {
            "project": {
                "project_id": project.id,
                "revision": 0,
                "state": {"business_outcome": "in_progress", "phase": "request"},
            }
        },
    )

    assert variables["project"]["revision"] == 1
    assert variables["project"]["state"]["phase"] == "discovery"
    assert variables["project"]["last_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_stickman_publication_claim_rejects_a_stale_video_revision(
    db_session,
):
    from packages.core.services.workflow_project_service import (
        WorkflowProjectConflict,
        create_workflow_project,
        patch_workflow_project,
    )

    state = {
        "artifact": {
            "document_id": "document-stickman",
            "fs_path": "runs/stickman/final.mp4",
            "filename": "final.mp4",
            "sha256": "a" * 64,
        },
        "publication": {
            "title": "Verified Stickman video",
            "description": "Ready for operator review",
            "audience": "not_made_for_kids",
            "visibility": "public",
            "review_status": "draft",
        },
        "topic": {
            "reservation_id": "topic-stickman",
            "selected_topic": "A practical lesson",
        },
    }
    project = await create_workflow_project(
        db_session,
        entity_id="entity-stickman-publish",
        workspace_id="workspace-stickman-publish",
        project_type="stickman_video",
        project_key="topic-stickman",
        state=state,
        created_by="user-stickman-publish",
        current_stage="video_ready",
        last_run_id="generation-run",
    )
    await db_session.commit()

    approved_state = deepcopy(state)
    approved_state["publication"]["review_status"] = "approved"
    winner = await patch_workflow_project(
        db_session,
        project_id=project.id,
        entity_id="entity-stickman-publish",
        workspace_id="workspace-stickman-publish",
        expected_revision=0,
        state=approved_state,
        current_stage="publishing",
        last_run_id="publisher-a",
    )

    with pytest.raises(WorkflowProjectConflict):
        await patch_workflow_project(
            db_session,
            project_id=project.id,
            entity_id="entity-stickman-publish",
            workspace_id="workspace-stickman-publish",
            expected_revision=0,
            state=approved_state,
            current_stage="publishing",
            last_run_id="publisher-b",
        )

    assert winner.revision == 1
    assert winner.current_stage == "publishing"
    assert winner.last_run_id == "publisher-a"


@pytest.mark.asyncio
async def test_retry_preserves_legacy_product_video_asset_evidence_for_validation(
    db_session,
):
    from packages.core.services.workflow_project_service import (
        create_workflow_project,
    )
    from packages.core.services.workflow_service import (
        _refresh_retry_workflow_project,
    )

    scene = {
        "scene_id": "scene-1",
        "required_asset_types": ["recording", "screenshot"],
        "acceptance_evidence": [
            {
                "must_show": "Workspace list",
                "observable": "Workspace list is visible",
                "required_asset_type": "recording",
            }
        ],
    }
    state = {
        "business_outcome": "needs_input",
        "plan": {
            "scene_ids": ["scene-1"],
            "scenes": [deepcopy(scene)],
            "must_show_coverage": [
                {
                    "requirement": "Workspace list",
                    "scene_ids": ["scene-1"],
                    "acceptance_evidence": deepcopy(scene["acceptance_evidence"]),
                }
            ],
        },
        "scenes": [deepcopy(scene)],
    }
    project = await create_workflow_project(
        db_session,
        entity_id="entity-pv-retry",
        workspace_id="workspace-pv-retry",
        project_type="product_video",
        state=state,
        created_by="user-pv-retry",
        last_run_id="run-pv-retry",
    )
    await db_session.commit()
    prior = SimpleNamespace(
        entity_id="entity-pv-retry",
        workspace_id="workspace-pv-retry",
    )

    variables = await _refresh_retry_workflow_project(
        db_session,
        prior,
        {"project": {"project_id": project.id}},
    )

    runtime_state = variables["project"]["state"]
    assert runtime_state == state
    assert project.state == state


@pytest.mark.asyncio
async def test_parent_retry_reuses_completed_foreach_child_retry(client, db_session):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "foreachrecovery")
    child_steps = [
        {"id": "start", "type": "trigger", "next": ["check"]},
        {"id": "check", "type": "condition", "config": {"expression": "ready == true"},
         "true_next": ["done"], "false_next": ["blocked"]},
        {"id": "blocked", "type": "stop", "config": {"message": "Temporarily unavailable"}},
        {"id": "done", "type": "end", "config": {}},
    ]
    child = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Equipment check", "steps": child_steps, "variables": {"ready": False},
    })).json()
    parent = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Equipment batch", "steps": [
            {"id": "start", "type": "trigger", "next": ["batch"]},
            {"id": "batch", "type": "foreach_subworkflow", "config": {
                "workflow_id": child["id"], "over": [{"id": "equipment-a"}], "output_var": "checks",
            }, "next": ["done"]},
            {"id": "done", "type": "end", "config": {}},
        ],
    })).json()
    initial = (await client.post(f"/api/v1/workflows/{parent['id']}/run", headers=headers,
                                 json={"execute": False})).json()
    await WorkflowRunner().run(initial["id"])
    failed = (await client.get(f"/api/v1/workflows/runs/{initial['id']}", headers=headers)).json()
    assert failed["status"] == "failed"
    child_id = failed["variables"]["__foreach_subworkflow__batch"]["items"][0]["subrun_id"]
    response = await client.post(f"/api/v1/workflows/runs/{child_id}/retry", headers=headers,
                                 json={"from_step_id": "check", "variables": {"ready": True}, "execute": False})
    assert response.status_code == 201, response.text
    child_retry_id = response.json()["id"]
    # Retrying the parent while its child has not completed must retain failure.
    response = await client.post(f"/api/v1/workflows/runs/{initial['id']}/retry", headers=headers,
                                 json={"from_step_id": "batch", "execute": False})
    assert response.status_code == 201, response.text
    blocked_parent_id = response.json()["id"]
    await WorkflowRunner().run(blocked_parent_id)
    blocked = await db_session.get(WorkflowRun, blocked_parent_id)
    assert blocked.status == "failed"
    await WorkflowRunner().run(child_retry_id)
    response = await client.post(f"/api/v1/workflows/runs/{blocked_parent_id}/retry", headers=headers,
                                 json={"from_step_id": "batch", "execute": False})
    assert response.status_code == 201, response.text
    retried = response.json()
    await WorkflowRunner().run(retried["id"])
    completed = (await client.get(f"/api/v1/workflows/runs/{retried['id']}", headers=headers)).json()
    assert completed["status"] == "completed"
    assert completed["variables"]["checks"][0]["ready"] is True
    assert completed["step_results"]["batch"]["items"][0]["subrun_id"] == child_retry_id


@pytest.mark.asyncio
@pytest.mark.parametrize("local_definition", [True, False])
async def test_scheduled_deployment_resolves_names_in_target_workspace(client, db_session, monkeypatch, local_definition):
    import packages.core.database as db_module
    import packages.core.ai.tools.workflow_tools as tools
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "scopeddeployment")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _create_workspace(client, headers, "Equipment maintenance")
    other_workspace_id = await _create_workspace(client, headers, "Other equipment")
    expected = WorkflowDefinition(
        entity_id=me["entity_id"], workspace_id=workspace_id if local_definition else None,
        name="Inspect equipment", steps=_simple_steps(["inspect"]),
    )
    db_session.add(expected)
    await db_session.commit()
    foreign = WorkflowDefinition(
        entity_id=me["entity_id"], workspace_id=other_workspace_id,
        name=expected.name, steps=expected.steps,
    )
    db_session.add(foreign)
    await db_session.commit()
    monkeypatch.setattr(tools, "async_session", db_module.async_session)
    args = dict(
        entity_id=me["entity_id"], user_id=me["id"], target_workspace_id=workspace_id,
        trigger_type="schedule", cron="0 9 * * 1",
    )
    result = json.loads(await tools._deploy_workflow(**args, workflow=expected.name))
    assert result["ok"] is True, result
    job = await db_session.get(ScheduledJob, result["scheduled_job_id"])
    assert job.workspace_id == workspace_id
    assert job.execution_target["workflow_id"] == expected.id
    denied = json.loads(await tools._deploy_workflow(**args, workflow=foreign.id))
    assert denied["ok"] is False
    duplicate = WorkflowDefinition(
        entity_id=me["entity_id"], workspace_id=expected.workspace_id,
        name=expected.name, steps=expected.steps,
    )
    db_session.add(duplicate)
    await db_session.commit()
    ambiguous = json.loads(await tools._deploy_workflow(**args, workflow=expected.name))
    assert ambiguous["ok"] is False


@pytest.mark.asyncio
async def test_scheduled_deployment_resolves_a_blueprint_flow_by_slug_in_its_workspace(client, db_session, monkeypatch):
    """A Blueprint step names its own Flow by slug; renames and other installs do not change the target."""
    import packages.core.database as db_module
    import packages.core.ai.tools.workflow_tools as tools
    from packages.core.models.workflow import WorkflowDefinition, WorkflowTemplateInstallation
    from packages.core.models.scheduler import ScheduledJob

    headers = await _auth(client, "slugdeployment")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _create_workspace(client, headers, "Equipment maintenance")
    other_workspace_id = await _create_workspace(client, headers, "Other equipment")
    installed = {}
    for target_workspace_id, name in ((workspace_id, "Weekly checks (renamed)"), (other_workspace_id, "Weekly checks")):
        flow = WorkflowDefinition(
            entity_id=me["entity_id"], workspace_id=target_workspace_id,
            name=name, steps=_simple_steps(["inspect"]),
        )
        db_session.add(flow)
        await db_session.flush()
        db_session.add(WorkflowTemplateInstallation(
            entity_id=me["entity_id"], template_id=f"workspace_blueprint:equipment:{target_workspace_id}",
            component_key="equipment-weekly-checks", workflow_id=flow.id, source_type="workspace_blueprint",
        ))
        installed[target_workspace_id] = flow
    await db_session.commit()
    monkeypatch.setattr(tools, "async_session", db_module.async_session)
    args = dict(
        entity_id=me["entity_id"], user_id=me["id"], target_workspace_id=workspace_id,
        trigger_type="schedule", cron="0 9 * * 1",
    )

    result = json.loads(await tools._deploy_workflow(**args, workflow="equipment-weekly-checks"))
    assert result["ok"] is True, result
    job = await db_session.get(ScheduledJob, result["scheduled_job_id"])
    assert job.execution_target["workflow_id"] == installed[workspace_id].id
    # The other Workspace's install keeps the old display name; it is never a fallback here.
    assert json.loads(await tools._deploy_workflow(**args, workflow="Weekly checks"))["ok"] is False


@pytest.mark.asyncio
async def test_manor_agent_workflow_tools_crud_deploy_and_run(client: AsyncClient, monkeypatch):
    """Conversation tools can build, publish, execute, inspect, and remove a flow."""
    registration = (await client.post("/api/v1/auth/register", json={
        "username": "wfagenttools",
        "email": "wfagenttools@test.com",
        "password": "pass123",
        "entity_name": "Workflow Agent Tools Corp",
    })).json()
    entity_id = registration["entity_id"]
    user_id = registration["user_id"]

    import packages.core.database as db_module
    import packages.core.ai.tools.workflow_tools as tools

    # workflow_tools is a process-wide registry module; point its captured
    # session factory at the test database configured by the client fixture.
    monkeypatch.setattr(tools, "async_session", db_module.async_session)

    created = json.loads(await tools._create_workflow(
        entity_id=entity_id,
        user_id=user_id,
        name="Agent campaign flow",
        description="Created entirely through Manor Agent tools",
        steps=_simple_steps(["draft", "publish"]),
        variables={"campaign": "launch"},
    ))
    assert created["ok"] is True
    workflow_id = created["workflow"]["id"]
    assert created["validation"]["valid"] is True

    fetched = json.loads(await tools._get_workflow(
        entity_id=entity_id,
        workflow=workflow_id,
    ))
    assert fetched["workflow"]["name"] == "Agent campaign flow"
    version = fetched["workflow"]["version"]

    updated = json.loads(await tools._update_workflow(
        entity_id=entity_id,
        workflow=workflow_id,
        expected_version=version,
        description="Updated through conversation",
    ))
    assert updated["ok"] is True
    assert updated["workflow"]["version"] == version + 1

    actor_user_id = user_id

    async def fake_ai_edit(prompt, current_steps, generated_entity_id, *, user_id=None):
        assert user_id == actor_user_id
        assert prompt == "Add a final audit step"
        assert generated_entity_id == entity_id
        assert current_steps[0]["type"] == "trigger"
        return {
            "name": "AI renamed draft",
            "variables": {"campaign": "launch"},
            "steps": _simple_steps(["draft", "publish", "audit"]),
        }

    import packages.core.services.workflow_generator as workflow_generator
    monkeypatch.setattr(workflow_generator, "generate_workflow", fake_ai_edit)
    edited = json.loads(await tools._ai_edit_workflow(
        entity_id=entity_id,
        user_id=user_id,
        workflow=workflow_id,
        prompt="Add a final audit step",
        expected_version=updated["workflow"]["version"],
    ))
    assert edited["ok"] is True
    assert edited["saved"] is True
    assert edited["workflow"]["name"] == "Agent campaign flow"
    assert [step["id"] for step in edited["workflow"]["steps"]][-2:] == ["audit", "end"]

    deployed = json.loads(await tools._deploy_workflow(
        entity_id=entity_id,
        user_id=user_id,
        workflow=workflow_id,
        trigger_type="mcp",
        description="Build and publish a campaign",
    ))
    assert deployed["ok"] is True
    assert deployed["deployment"]["trigger_type"] == "mcp"

    published = json.loads(await tools._list_workflows(entity_id=entity_id))
    assert [item["id"] for item in published["workflows"]] == [workflow_id]

    executed = json.loads(await tools._run_workflow(
        entity_id=entity_id,
        user_id=user_id,
        workflow=workflow_id,
        inputs={"campaign": "summer"},
    ))
    assert executed["ok"] is True
    assert executed["run"]["status"] == "completed"
    assert executed["run"]["step_results"]["draft"]["output"]["draft"] == "done"
    run_id = executed["run"]["id"]

    history = json.loads(await tools._list_workflow_runs(
        entity_id=entity_id,
        workflow=workflow_id,
    ))
    assert history["runs"][0]["id"] == run_id

    blocked_delete = json.loads(await tools._delete_workflow(
        entity_id=entity_id,
        workflow=workflow_id,
    ))
    assert blocked_delete["ok"] is False
    assert blocked_delete["binding_ids"] == [deployed["deployment"]["id"]]

    deleted = json.loads(await tools._delete_workflow(
        entity_id=entity_id,
        workflow=workflow_id,
        delete_bindings=True,
    ))
    assert deleted == {
        "ok": True,
        "deleted": True,
        "workflow_id": workflow_id,
        "deleted_bindings": 1,
    }


@pytest.mark.asyncio
async def test_create_workflow(client: AsyncClient):
    headers = await _auth(client)
    steps = _simple_steps(["s1", "s2"])
    resp = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "My Pipeline",
        "steps": steps,
        "description": "A test workflow",
        "icon": "image",
        "category": "ops",
        "tags": ["test"],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Pipeline"
    assert data["description"] == "A test workflow"
    assert data["icon"] == "image"
    assert len(data["steps"]) == 4
    assert data["status"] == "active"
    assert data["is_active"] is True
    assert data["category"] == "ops"
    assert data["tags"] == ["test"]
    assert data["created_by"]
    assert data["created_at"]

    # Verify we can GET it back
    wf_id = data["id"]
    get_resp = await client.get(f"/api/v1/workflows/{wf_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "My Pipeline"

    # Identity fields are edited together from the workflow editor.
    update_resp = await client.put(f"/api/v1/workflows/{wf_id}", headers=headers, json={
        "name": "Campaign Pipeline",
        "description": "An updated campaign workflow",
        "icon": "notify",
    })
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Campaign Pipeline"
    assert update_resp.json()["description"] == "An updated campaign workflow"
    assert update_resp.json()["icon"] == "notify"

    # Details expose real provenance and the workspaces currently using it.
    workspace = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Campaign Operations"},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": wf_id,
            "workspace_id": workspace["id"],
            "name": "Campaign launch",
        },
    )).json()
    metadata_resp = await client.get(
        f"/api/v1/workflows/{wf_id}/metadata",
        headers=headers,
    )
    assert metadata_resp.status_code == 200
    metadata = metadata_resp.json()
    assert metadata["created_by"] == data["created_by"]
    assert metadata["creator"]["name"] == "wfuser"
    assert metadata["created_at"] == data["created_at"]
    assert metadata["binding_count"] == 1
    assert metadata["workspace_count"] == 1
    assert metadata["workspace_usage"][0]["workspace_id"] == workspace["id"]
    assert metadata["workspace_usage"][0]["workspace_name"] == "Campaign Operations"

    # The editor exposes this endpoint as a confirmed destructive action.
    assert (await client.delete(
        f"/api/v1/workflows/bindings/{binding['id']}",
        headers=headers,
    )).status_code == 204
    delete_resp = await client.delete(f"/api/v1/workflows/{wf_id}", headers=headers)
    assert delete_resp.status_code == 204
    assert (await client.get(f"/api/v1/workflows/{wf_id}", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_run_rejects_workflow_without_explicit_trigger(client: AsyncClient):
    headers = await _auth(client, "wfmissingtrigger")
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Incomplete graph",
        "steps": [{
            "id": "work",
            "type": "transform",
            "name": "Work",
            "config": {"set": {"result": "done"}},
            "next": [],
        }],
    })).json()

    response = await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )
    assert response.status_code == 409
    assert "explicit trigger" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_ai_edit_style_update_rejects_invalid_graph_without_persisting_it(
    client: AsyncClient,
):
    headers = await _auth(client, "wfaieditvalidation")
    created = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Safe AI edit", "steps": _simple_steps(["draft"])},
    )).json()
    invalid_steps = [{
        "id": "unsupported",
        "type": "unsupported",
        "config": {},
        "next": [],
    }]

    rejected = await client.put(
        f"/api/v1/workflows/{created['id']}",
        headers=headers,
        json={"steps": invalid_steps, "validate_steps": True},
    )
    assert rejected.status_code == 422
    assert "workflow is invalid" in rejected.json()["detail"].lower()

    unchanged = (await client.get(
        f"/api/v1/workflows/{created['id']}",
        headers=headers,
    )).json()
    assert unchanged["steps"] == created["steps"]


@pytest.mark.asyncio
async def test_run_rejects_spoofed_imported_unmapped_node(
    client: AsyncClient,
):
    headers = await _auth(client, "wfimportedunsupported")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Partially mapped import",
            "tags": [
                "imported:n8n",
                "manor-import-proof:n8n:attacker-controlled",
            ],
            "steps": [
                {
                    "id": "trigger",
                    "type": "trigger",
                    "config": {},
                    "next": ["legacy"],
                },
                {
                    "id": "legacy",
                    "type": "unsupported",
                    "config": {},
                    "meta": {
                        "source_tool": "n8n",
                        "original_type": "vendor.node",
                        "unmapped": True,
                    },
                    "next": ["end"],
                },
                {
                    "id": "end",
                    "type": "end",
                    "config": {},
                    "next": [],
                },
            ],
        },
    )).json()
    assert "imported:n8n" not in workflow["tags"]
    assert not any(
        tag.startswith("manor-import-proof:") for tag in workflow["tags"]
    )

    started = await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )
    assert started.status_code == 409, started.text
    assert "must be replaced" in started.json()["detail"]


@pytest.mark.asyncio
async def test_start_and_execute(client: AsyncClient):
    headers = await _auth(client, "wfuser2")
    steps = _simple_steps(["a", "b"])
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Two-step", "steps": steps,
    })).json()
    wf_id = wf["id"]

    # Start a run without inline execution (manual /step driving)
    run_resp = await client.post(f"/api/v1/workflows/{wf_id}/run", headers=headers, json={
        "variables": {"input": "hello"}, "execute": False,
    })
    assert run_resp.status_code == 201
    run = run_resp.json()
    assert run["status"] == "running"
    assert run["current_step_id"] == "trigger"
    run_id = run["id"]

    # Execute the explicit entry, then step a (transform).
    entry = (await client.post(f"/api/v1/workflows/runs/{run_id}/step", headers=headers)).json()
    assert entry["step_id"] == "trigger"
    assert entry["status"] == "completed"

    step1 = (await client.post(f"/api/v1/workflows/runs/{run_id}/step", headers=headers)).json()
    assert step1["step_id"] == "a"
    assert step1["status"] == "completed"

    # Verify run advanced to step b
    run_state = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run_state["current_step_id"] == "b"

    # Execute step b
    step2 = (await client.post(f"/api/v1/workflows/runs/{run_id}/step", headers=headers)).json()
    assert step2["step_id"] == "b"
    assert step2["status"] == "completed"

    # Explicit terminal marker completes the run after the transform chain.
    terminal = (await client.post(f"/api/v1/workflows/runs/{run_id}/step", headers=headers)).json()
    assert terminal["step_id"] == "end"
    final = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert final["status"] == "completed"
    assert final["completed_at"] is not None
    # Both steps recorded in step_results
    assert "a" in final["step_results"]
    assert "b" in final["step_results"]
    assert [
        (trace["node_id"], trace["status"])
        for trace in final["execution_trace"]
    ] == [
        ("trigger", "running"),
        ("trigger", "completed"),
        ("a", "running"),
        ("a", "completed"),
        ("b", "running"),
        ("b", "completed"),
        ("end", "running"),
        ("end", "completed"),
    ]


@pytest.mark.asyncio
async def test_manual_step_terminal_status_finalizes_scheduled_occurrence(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from datetime import datetime, timezone

    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services import workflow_chat_projection

    async def broken_chat_projection(*_args, **_kwargs):
        raise RuntimeError("chat projection unavailable")

    monkeypatch.setattr(
        workflow_chat_projection,
        "_project_workflow_run_status_chat",
        broken_chat_projection,
    )

    headers = await _auth(client, "wfmanualscheduled")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Scheduled manual step", "steps": _simple_steps([])},
    )).json()
    run_data = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    run = await db_session.get(WorkflowRun, run_data["id"])
    assert run is not None
    job = ScheduledJob(
        id=generate_ulid(),
        job_id=f"manual-step:{generate_ulid()}",
        entity_id=run.entity_id,
        workspace_id=run.workspace_id,
        name="Scheduled manual step",
        execution_type="workflow",
        enabled=True,
        last_status="running",
    )
    scheduled_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=job.job_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    run.trigger_source = "schedule"
    run.trigger_data = {
        "scheduled_job_id": job.job_id,
        "scheduled_run_id": scheduled_run.id,
    }
    db_session.add_all([job, scheduled_run])
    await db_session.commit()

    entry = await client.post(
        f"/api/v1/workflows/runs/{run.id}/step",
        headers=headers,
    )
    terminal = await client.post(
        f"/api/v1/workflows/runs/{run.id}/step",
        headers=headers,
    )

    assert entry.status_code == 200, entry.text
    assert terminal.status_code == 200, terminal.text
    assert terminal.json()["step_id"] == "end"
    await db_session.refresh(scheduled_run)
    await db_session.refresh(job)
    assert scheduled_run.status == "completed"
    assert scheduled_run.result == {
        "workflow_run_id": run.id,
        "status": "completed",
    }
    assert job.last_status == "completed"


@pytest.mark.asyncio
async def test_manual_step_rejects_a_run_held_by_the_full_runner(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfmanualclaim")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Manual step claim", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    entry = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    assert entry.status_code == 200, entry.text

    original_execute = WorkflowRunner._execute_step_safe
    work_started = asyncio.Event()
    finish_work = asyncio.Event()
    work_calls = 0

    async def delayed_work(self, step, workflow_run, db):
        nonlocal work_calls
        if step["id"] == "work":
            work_calls += 1
            if work_calls == 1:
                work_started.set()
                await finish_work.wait()
        return await original_execute(self, step, workflow_run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", delayed_work)
    full_run = asyncio.create_task(WorkflowRunner().run(run["id"]))
    try:
        await wait_for_signal(work_started, full_run)
        manual = await asyncio.wait_for(
            client.post(f"/api/v1/workflows/runs/{run['id']}/step", headers=headers),
            timeout=LIVENESS_TIMEOUT_S,
        )
    finally:
        finish_work.set()
        await asyncio.wait_for(full_run, timeout=LIVENESS_TIMEOUT_S)

    assert manual.status_code == 400, manual.text
    assert manual.json()["detail"] == "Run is already executing"
    assert work_calls == 1


@pytest.mark.asyncio
async def test_manual_step_executes_private_snapshot_after_definition_delete(
    client: AsyncClient,
):
    headers = await _auth(client, "wfmanualdeletedsnapshot")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Deleted manual definition", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    deleted = await client.delete(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text

    stepped = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )

    assert stepped.status_code == 200, stepped.text
    assert stepped.json()["step_id"] == "trigger"
    assert stepped.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_manual_step_commits_checkpoint_before_releasing_execution_claim(
    client: AsyncClient,
    monkeypatch,
):
    import packages.core.database as db_module
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services import workflow_run_execution_claim as claim_service

    headers = await _auth(client, "wfmanualdurableclaim")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Durable manual claim", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    durable_at_release: list[bool] = []

    original_release = claim_service._release_postgres_execution_claim

    async def release(claim_key, token):
        async with db_module.async_session() as verification_db:
            persisted = await verification_db.get(WorkflowRun, run["id"])
            durable_at_release.append(
                bool(persisted and "trigger" in (persisted.step_results or {}))
            )
        return await original_release(claim_key, token)

    monkeypatch.setattr(
        claim_service,
        "_release_postgres_execution_claim",
        release,
    )

    stepped = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )

    assert stepped.status_code == 200, stepped.text
    assert durable_at_release == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "should_project", "should_record_inline", "should_finalize"),
    [
        ("running", False, False, False),
        ("paused", True, True, False),
        ("completed", True, False, True),
        ("failed", True, False, True),
        ("cancelled", True, False, True),
    ],
)
async def test_manual_step_projects_every_settled_run_before_commit(
    monkeypatch,
    run_status: str,
    should_project: bool,
    should_record_inline: bool,
    should_finalize: bool,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.ledger import adapters as ledger_adapters
    from packages.core.services import workflow_chat_projection
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.services import workflow_service

    run = SimpleNamespace(status=run_status)
    projected: list[object] = []
    recorded: list[object] = []
    finalized: list[object] = []
    committed: list[bool] = []
    terminal_committed: list[bool] = []

    class FakeDb:
        async def commit(self):
            committed.append(True)

    class FakeClaim:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_if_lost(self):
            return None

        async def fence_for_commit(self, db):
            assert isinstance(db, FakeDb)

        def mark_terminal_committed(self):
            terminal_committed.append(True)

    async def execute_claimed(*_args):
        return {"status": run_status}

    async def get_run(*_args):
        return run

    async def project(_db, *, run):
        projected.append(run)

    async def record(_db, run):
        recorded.append(run)

    async def finalize(_runner, run, _db):
        finalized.append(run)

    monkeypatch.setattr(
        claim_service,
        "workflow_run_execution_claim",
        lambda _run_id: FakeClaim(),
    )
    monkeypatch.setattr(
        workflow_service,
        "_execute_claimed_workflow_step",
        execute_claimed,
    )
    monkeypatch.setattr(workflow_service, "get_run", get_run)
    monkeypatch.setattr(
        workflow_chat_projection,
        "project_workflow_run_status",
        project,
    )
    monkeypatch.setattr(ledger_adapters, "record_workflow_run_status", record)
    monkeypatch.setattr(WorkflowRunner, "_finalize_run_effects", finalize)

    result = await workflow_service.execute_workflow_step(
        FakeDb(),
        "run-1",
        "entity-1",
    )

    assert result == {"status": run_status}
    assert projected == ([run] if should_project else [])
    assert recorded == ([run] if should_record_inline else [])
    assert finalized == ([run] if should_finalize else [])
    assert terminal_committed == [True]
    assert committed == [True]


@pytest.mark.asyncio
async def test_manual_step_returns_terminal_result_when_effects_are_deferred(
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services import workflow_chat_projection
    from packages.core.services import workflow_run_execution_claim as claim_service
    from packages.core.services import workflow_service

    run = SimpleNamespace(
        id="run-terminal-step",
        status="completed",
        terminal_effects_completed_at=None,
        terminal_effects_next_attempt_at=None,
    )
    rollbacks: list[bool] = []

    class FakeDb:
        async def commit(self):
            return None

        async def rollback(self):
            rollbacks.append(True)

    class FakeClaim:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __bool__(self):
            return True

        def raise_if_lost(self):
            return None

        async def fence_for_commit(self, _db):
            return None

        def mark_terminal_committed(self):
            return None

    async def no_project(*_args, **_kwargs):
        return None

    async def execute_claimed(*_args):
        return {"status": "completed"}

    async def get_run(*_args):
        return run

    async def fail_effects(_runner, _run, _db):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(
        claim_service,
        "workflow_run_execution_claim",
        lambda _run_id: FakeClaim(),
    )
    monkeypatch.setattr(
        workflow_service,
        "_execute_claimed_workflow_step",
        execute_claimed,
    )
    monkeypatch.setattr(
        workflow_service,
        "get_run",
        get_run,
    )
    monkeypatch.setattr(
        workflow_chat_projection,
        "project_workflow_run_status",
        no_project,
    )
    monkeypatch.setattr(WorkflowRunner, "_finalize_run_effects", fail_effects)

    result = await workflow_service.execute_workflow_step(
        FakeDb(),
        run.id,
        "entity-1",
    )

    assert result == {"status": "completed"}
    assert run.status == "completed"
    assert run.terminal_effects_next_attempt_at is not None
    assert rollbacks == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["chat", "approval"])
async def test_workflow_status_settlement_survives_chat_projection_failure(
    monkeypatch, projection,
):
    from packages.core.services import proposal_workflow_runs
    from packages.core.services import scheduler_service
    from packages.core.services import workflow_chat_projection

    events = []

    class NestedTransaction:
        async def __aenter__(self):
            events.append("chat_savepoint")

        async def __aexit__(self, exc_type, *_args):
            # A savepoint that exits cleanly is released, not rolled back.
            events.append(f"chat_rollback:{exc_type.__name__}" if exc_type else "chat_release")
            return False

    class FakeDb:
        async def flush(self):
            events.append("authoritative_flush")

        def begin_nested(self):
            return NestedTransaction()

    async def finalize(_db, _run):
        events.append("scheduler_finalized")

    async def sync(_db, _run):
        events.append("proposal_synced")

    async def broken_chat(_db, *, run):
        assert run is workflow_run
        events.append("chat_projection")
        raise RuntimeError("corrupt chat projection")

    from packages.core.services import workflow_runtime_approvals

    async def project_approval(_db, *, run, step, result):
        assert run is workflow_run
        assert step == {"id": "step-1"}
        assert result == {"status": "paused"}
        events.append("approval_projection")
        if projection == "approval":
            raise RuntimeError("corrupt approval projection")

    workflow_run = SimpleNamespace(
        id="run-1", current_step_id="step-1",
        step_results={"step-1": {"status": "paused"}},
    )
    monkeypatch.setattr(
        workflow_runtime_approvals,
        "project_workflow_runtime_approval",
        project_approval,
    )
    monkeypatch.setattr(
        scheduler_service,
        "finalize_scheduled_workflow_run",
        finalize,
    )
    monkeypatch.setattr(
        proposal_workflow_runs,
        "sync_proposal_workflow_run_item",
        sync,
    )
    monkeypatch.setattr(
        workflow_chat_projection,
        "_project_workflow_run_status_chat",
        broken_chat,
    )

    await workflow_chat_projection.project_workflow_run_status(
        FakeDb(),
        run=workflow_run,
    )

    assert events == [
        "scheduler_finalized",
        "proposal_synced",
        "authoritative_flush",
        "chat_savepoint",
        "approval_projection",
        *(["chat_projection"] if projection == "chat" else []),
        "chat_rollback:RuntimeError",
        # The stale-card cleanup runs in its own savepoint after the chat
        # projection, and a run that is not ended closes nothing.
        "chat_savepoint",
        "chat_release",
    ]


@pytest.mark.asyncio
async def test_manual_step_routes_handled_failure_to_error_output(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfmanualerroroutput")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Manual error output",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["request"]},
                {
                    "id": "request",
                    "type": "transform",
                    "config": {
                        "on_error": "continue_error",
                        "error_next": ["recover"],
                    },
                    "next": ["success"],
                },
                {"id": "success", "type": "transform", "next": []},
                {"id": "recover", "type": "transform", "next": []},
            ],
        },
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    entry = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    assert entry.status_code == 200, entry.text

    original_execute = WorkflowRunner._execute_step_safe

    async def fail_request(self, step, workflow_run, db):
        if step["id"] == "request":
            workflow_run.current_step_id = step["id"]
            return {"status": "failed", "error": "upstream unavailable"}
        return await original_execute(self, step, workflow_run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", fail_request)

    failed = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    recovered = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    detail = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()

    assert failed.status_code == 200, failed.text
    assert failed.json()["continued"] is True
    assert failed.json()["next_step_id"] == "recover"
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["step_id"] == "recover"
    assert detail["status"] == "completed"
    assert detail["step_results"]["request"]["next_override"] == ["recover"]
    assert "success" not in detail["step_results"]


@pytest.mark.asyncio
async def test_manual_step_persists_structured_error_as_safe_text(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services.workflow_run_trace import TRACE_SUMMARY_BYTES

    headers = await _auth(client, "wfmanualstructurederror")
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Manual structured failure",
        "steps": _simple_steps(["fail"]),
    })).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    entry = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    assert entry.status_code == 200

    structured_error = {
        "code": "manual_provider_failed",
        "authorization": "Bearer manual-secret-value-123456",
        "password": "manual-password-value",
        "message": "Provider rejected the manual step",
    }

    async def execute_structured_failure(self, step, workflow_run, db):
        workflow_run.current_step_id = step["id"]
        return {"status": "failed", "error": structured_error}

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", execute_structured_failure)

    response = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    assert response.status_code == 200
    failed = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    encoded_error = failed["error"].encode("utf-8")
    assert failed["status"] == "failed"
    assert isinstance(failed["error"], str)
    assert len(encoded_error) <= TRACE_SUMMARY_BYTES
    assert b"manual-secret-value-123456" not in encoded_error
    assert b"manual-password-value" not in encoded_error
    assert failed["error"] == "Provider rejected the manual step"
    assert failed["step_results"]["fail"]["error"] == structured_error


@pytest.mark.asyncio
async def test_manual_step_trace_records_paused_terminal_transition(client: AsyncClient):
    headers = await _auth(client, "wfmanualpausedtrace")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Manual pause trace",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["approval"]},
                {
                    "id": "approval",
                    "type": "wait",
                    "config": {"wait_type": "approval"},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    start = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    paused = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step",
        headers=headers,
    )
    assert start.status_code == 200
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    detail = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert [
        trace["status"]
        for trace in detail["execution_trace"]
        if trace["node_id"] == "approval"
    ] == ["running", "paused"]


@pytest.mark.asyncio
async def test_condition_step(client: AsyncClient):
    headers = await _auth(client, "wfuser3")
    steps = [
        {"id": "start", "type": "trigger", "name": "Start", "next": ["check"]},
        {"id": "check", "type": "condition", "name": "Score check",
         "config": {"expression": "score > 0.7"},
         "true_next": ["pass"], "false_next": ["fail"]},
        {"id": "pass", "type": "transform", "name": "Pass",
         "config": {"set": {"result": "passed"}}, "next": []},
        {"id": "fail", "type": "transform", "name": "Fail",
         "config": {"set": {"result": "failed"}}, "next": []},
    ]
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Condition test", "steps": steps,
    })).json()

    # Run with score=0.9 -> should take true_next -> "pass"
    run1 = (await client.post(f"/api/v1/workflows/{wf['id']}/run", headers=headers, json={
        "variables": {"score": 0.9}, "execute": False,
    })).json()
    await client.post(f"/api/v1/workflows/runs/{run1['id']}/step", headers=headers)
    step_result = (await client.post(
        f"/api/v1/workflows/runs/{run1['id']}/step", headers=headers
    )).json()
    assert step_result["output"] is True
    run1_state = (await client.get(
        f"/api/v1/workflows/runs/{run1['id']}", headers=headers
    )).json()
    assert run1_state["current_step_id"] == "pass"

    # Run with score=0.3 -> should take false_next -> "fail"
    run2 = (await client.post(f"/api/v1/workflows/{wf['id']}/run", headers=headers, json={
        "variables": {"score": 0.3}, "execute": False,
    })).json()
    await client.post(f"/api/v1/workflows/runs/{run2['id']}/step", headers=headers)
    step_result2 = (await client.post(
        f"/api/v1/workflows/runs/{run2['id']}/step", headers=headers
    )).json()
    assert step_result2["output"] is False
    run2_state = (await client.get(
        f"/api/v1/workflows/runs/{run2['id']}", headers=headers
    )).json()
    assert run2_state["current_step_id"] == "fail"


@pytest.mark.asyncio
async def test_list_runs(client: AsyncClient):
    headers = await _auth(client, "wfuser4")
    steps = _simple_steps(["x"])
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Multi-run", "steps": steps,
    })).json()
    wf_id = wf["id"]

    # Start 3 runs
    for _ in range(3):
        resp = await client.post(f"/api/v1/workflows/{wf_id}/run", headers=headers, json={})
        assert resp.status_code == 201

    # List runs for this workflow
    runs_resp = await client.get(f"/api/v1/workflows/{wf_id}/runs", headers=headers)
    assert runs_resp.status_code == 200
    runs = runs_resp.json()
    assert len(runs) == 3
    assert all(r["workflow_id"] == wf_id for r in runs)
    assert all("definition_snapshot" not in run for run in runs)
    assert all("execution_trace" not in run for run in runs)

    global_runs = (await client.get(
        "/api/v1/workflows/runs",
        headers=headers,
    )).json()
    assert len(global_runs) == 3
    assert all("definition_snapshot" not in run for run in global_runs)
    assert all("execution_trace" not in run for run in global_runs)

    detail = (await client.get(
        f"/api/v1/workflows/runs/{runs[0]['id']}",
        headers=headers,
    )).json()
    assert detail["definition_snapshot"]["workflow_id"] == wf_id
    assert detail["execution_trace"]


@pytest.mark.asyncio
async def test_workflow_run_lineage_is_server_owned_for_manual_and_webhook_starts(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wflineagespoof")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Server-owned lineage", "steps": _simple_steps(["work"])},
    )).json()
    spoofed_lineage = {
        "retry_of_run_id": "attacker-parent",
        "retry_from_step_id": "attacker-step",
        "attempt_number": 99,
        "safe_input": "kept",
    }

    manual = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False, "trigger_data": spoofed_lineage},
    )).json()
    webhook_binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={"workflow_id": workflow["id"], "trigger_type": "webhook"},
    )).json()
    webhook_response = await client.post(
        f"/api/v1/workflows/webhook/{webhook_binding['trigger_config']['webhook_token']}",
        json=spoofed_lineage,
    )
    assert webhook_response.status_code == 200, webhook_response.text
    webhook = (await client.get(
        f"/api/v1/workflows/runs/{webhook_response.json()['run_ids'][0]}",
        headers=headers,
    )).json()

    for run in (manual, webhook):
        assert run["retry_of_run_id"] is None
        assert run["retry_from_step_id"] is None
        assert run["attempt_number"] == 1
        assert run["lineage_status"] == "canonical"
        assert run["trigger_data"]["safe_input"] == "kept"
        assert {
            "retry_of_run_id",
            "retry_from_step_id",
            "attempt_number",
        }.isdisjoint(run["trigger_data"])

    stored = list((await db_session.execute(
        select(WorkflowRun).where(WorkflowRun.id.in_([manual["id"], webhook["id"]]))
    )).scalars())
    assert len(stored) == 2
    assert all(run.retry_of_run_id is None for run in stored)
    assert all(run.retry_from_step_id is None for run in stored)
    assert all(run.attempt_number == 1 for run in stored)
    assert all(run.lineage_root_run_id == run.id for run in stored)
    assert all(run.lineage_is_legacy is False for run in stored)


@pytest.mark.asyncio
async def test_workspace_manual_binding_run_projects_wait_to_workspace_chat(
    client: AsyncClient,
):
    long_review = "Detailed evidence. " * 900
    headers = await _auth(client, "wfworkspacemanualwait")
    workspace_id = await _create_workspace(
        client,
        headers,
        "Workspace manual wait projection",
    )
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Review generated video",
            "variables": {
                "video_project": {
                    "title": "Verified Stickman video",
                    "document_id": "document-video-1",
                    "notes": long_review,
                },
            },
            "steps": [
                {
                    "id": "trigger",
                    "type": "trigger",
                    "name": "Start",
                    "config": {},
                    "next": ["approve_video"],
                },
                {
                    "id": "approve_video",
                    "type": "wait",
                    "name": "Approve video",
                    "config": {
                        "wait_type": "approval",
                        "message": "Review the verified video before publication.",
                        "review_title": "Verified Stickman video",
                        "review": "{{video_project}}",
                        "response_variable": "video_decision",
                        "options": ["approve", "cancel"],
                    },
                    "next": ["end"],
                },
                {
                    "id": "end",
                    "type": "end",
                    "name": "Done",
                    "config": {},
                    "next": [],
                },
            ],
        },
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace_id,
            "trigger_type": "manual",
            "config": {
                "workspace_attached": True,
                "chat_entrypoint": {
                    "enabled": True,
                    "title": "Create, review, and publish a Stickman video",
                    "projection": {"progress": True, "step_outputs": "explicit"},
                    "wait_bridge": True,
                },
            },
        },
    )).json()

    response = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={
            "execute": True,
            "trigger_data": {"workspace_manual": True},
        },
    )

    run = await _execute_queued_binding_run(client, headers, response)
    assert run["status"] == "paused"
    assert run["current_step_id"] == "approve_video"
    origin = run["trigger_data"]["_workflow_chat_origin"]
    assert origin["wait_bridge"] is True
    assert origin["route_source"] == "workspace_manual"
    assert origin["activity_message_id"]

    messages_response = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=headers,
    )
    assert messages_response.status_code == 200, messages_response.text
    messages = messages_response.json()
    activity = next(
        message
        for message in messages
        if message["id"] == origin["activity_message_id"]
    )
    assert activity["message_kind"] == "workflow_activity"
    assert activity["meta"]["workflow_run_id"] == run["id"]
    approval = next(
        message
        for message in messages
        if (message.get("pending_action") or {}).get("workflow_run_id") == run["id"]
    )
    assert approval["pending_action"]["kind"] == "workflow_approval"
    assert approval["pending_action"]["step_id"] == "approve_video"
    assert approval["pending_action"]["review"] == {
        "title": "Verified Stickman video",
        "document_id": "document-video-1",
        "notes": long_review,
    }
    detail = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}", headers=headers,
    )).json()
    action = detail["resume_action"]
    assert action["message_id"] == approval["id"]
    assert action["message_workspace_id"] == workspace_id
    assert action["step_id"] == "approve_video"
    assert action["review"] == approval["pending_action"]["review"]
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{action['message_id']}/resolve",
        headers=headers, json={"choice": "cancel"},
    )
    assert resolved.status_code == 200, resolved.text
    terminal = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}", headers=headers,
    )).json()
    assert terminal["status"] == "cancelled"
    assert terminal["resume_action"] is None


@pytest.mark.parametrize(("config", "options"), [
    ({"message": "Confirm once the file is delivered.", "options": ["confirm"]}, ["confirm"]),
    ({"message": "交付给客户后，请确认交接已完成。", "options": ["approve", "revise"]}, ["confirm"]),
    # An approval that says "confirm" still asks for a judgement.
    ({"message": "Review the draft and confirm it is ready. Approve or request revisions.",
      "options": ["approve", "revise"]}, ["approve", "revise"]),
    # Choices the author named are theirs.
    ({"message": "Confirm the refund?", "options": ["yes", "no"]}, ["yes", "no"]),
])
def test_a_wait_asking_to_confirm_offers_one_confirm(config, options):
    from packages.core.services.workflow_contracts import workflow_wait_options

    assert workflow_wait_options({"wait_type": "approval", **config}) == options


@pytest.mark.asyncio
async def test_app_run_of_a_plain_binding_reports_in_chat_and_shows_the_files_to_approve(
    client: AsyncClient,
    db_session,
):
    """A Flow the App starts, attached without a Chat entry, still reports in the
    Workspace Chat, and a wait that names nothing shows the files made for it."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.document import Document

    headers = await _auth(client, "wfappplainbinding")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_id = await _create_workspace(client, headers, "Plain binding review")
    draft = Document(
        entity_id=me["entity_id"], name="final-draft.docx", fs_path="projects/a/final-draft.docx",
        file_type="docx", source="agent", vector_status="ready", created_by=me["id"],
    )
    db_session.add(draft)
    await db_session.commit()
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Write and deliver",
        "variables": {"draft_id": draft.id},
        "steps": [
            {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["create_article"]},
            {"id": "create_article", "type": "workflow_project", "next": ["write"], "config": {
                "operation": "create", "project_type": "article",
                "schema_version": 1, "current_stage": "writing",
                "state": {"title": "Onboarding analytics", "status": "writing"},
                "state_schema": {"type": "object", "properties": {"title": {"type": "string"}, "status": {"type": "string"}}},
            }},
            {"id": "write", "type": "transform", "name": "Write", "next": ["owner_approval"],
             "config": {"markdown": "Saved the draft: [final-draft.docx](/viewer/{{draft_id}})"}},
            {"id": "owner_approval", "type": "wait", "name": "Owner approval", "next": ["confirm_delivery"],
             "config": {"wait_type": "approval", "message": "Approve the final draft.",
                        "options": ["approve", "revise"], "response_variable": "final_approval"}},
            # Written before waits named "confirm": the generic choices, a message that asks to confirm.
            {"id": "confirm_delivery", "type": "wait", "name": "Confirm delivery", "next": ["delivered_status"],
             "config": {"wait_type": "approval",
                        "message": "Deliver the approved Word file to the client, then confirm the handoff is complete.",
                        "options": ["approve", "revise"], "approval_values": ["approve"], "revise_from": "write",
                        "response_variable": "delivery"}},
            {"id": "delivered_status", "type": "workflow_project", "next": ["end"], "config": {
                "operation": "patch", "project_id": "{{create_article.project_id}}", "project_type": "article",
                "schema_version": 1, "expected_revision": "{{create_article.revision}}", "patch": {"status": "delivered"},
                "current_stage": "delivered", "state_schema": {"type": "object", "properties": {"status": {"type": "string"}}},
            }},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    # Deployed the way an Agent deploys a Flow: no chat_entrypoint config.
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"], "workspace_id": workspace_id, "trigger_type": "manual",
    })).json()
    run = await _execute_queued_binding_run(client, headers, await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run", headers=headers,
        json={"execute": True, "trigger_data": {"workspace_manual": True, "_workspace_app_action": f"workflow:{binding['id']}"}},
    ))
    assert run["status"] == "paused" and run["current_step_id"] == "owner_approval"
    origin = run["trigger_data"]["_workflow_chat_origin"]
    assert origin["route_source"] == "workspace_manual" and origin["activity_message_id"]

    reviewed = {"document_id": draft.id, "name": "final-draft.docx", "fs_path": "projects/a/final-draft.docx"}

    async def pending(step_id: str) -> dict:
        messages = (await client.get(f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers)).json()
        assert any(message["id"] == origin["activity_message_id"] for message in messages)
        return next(
            message for message in messages
            if (message.get("pending_action") or {}).get("step_id") == step_id and not message.get("resolved_at")
        )

    approval = await pending("owner_approval")
    assert approval["pending_action"]["options"] == ["approve", "revise"]
    assert approval["pending_action"]["review"] == {"files": [reviewed]}
    assert approval["pending_action"]["review_read_only"] is True
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{approval['id']}/resolve",
        headers=headers, json={"choice": "approve"},
    )
    assert resolved.status_code == 200, resolved.text
    await WorkflowRunner().run(run["id"])

    # Nothing new was made since the approval: the confirmation still sees the file.
    confirmation = await pending("confirm_delivery")
    assert confirmation["pending_action"]["review"] == {"files": [reviewed]}
    assert confirmation["pending_action"]["options"] == ["confirm"]
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{confirmation['id']}/resolve",
        headers=headers, json={"choice": "confirm"},
    )
    assert resolved.status_code == 200, resolved.text
    await WorkflowRunner().run(run["id"])
    done = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert done["status"] == "completed"
    assert done["step_results"]["confirm_delivery"]["approved"] is True
    # The run ends on a project record; what it delivered is still the file it made,
    # and the result is named by the project, not by its bookkeeping fields.
    assert done["trigger_data"]["_workflow_history_summary"]["deliverables"] == [
        {"document_id": draft.id, "name": "final-draft.docx"},
    ]
    messages = (await client.get(f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers)).json()
    completion = next(message for message in messages if (message.get("meta") or {}).get("workflow_final_output"))
    assert completion["meta"]["workflow_result"]["title"] == "Onboarding analytics"
    assert [item.get("document_id") for item in completion["attachments"]] == [draft.id]


@pytest.mark.asyncio
async def test_workspace_chat_approves_a_review_assembled_from_several_variables(
    client: AsyncClient,
):
    headers = await _auth(client, "wfworkspacecompositereview")
    workspace_id = await _create_workspace(client, headers, "Composite review")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Review generated media",
            "variables": {"image": {"title": "Feed image"}, "publish_after_approval": False},
            "steps": [
                {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["approve_media"]},
                {
                    "id": "approve_media",
                    "type": "wait",
                    "name": "Approve media",
                    "config": {
                        "wait_type": "approval",
                        "message": "Review the generated media.",
                        "review": {"image": "{{image}}", "publish_after_approval": "{{publish_after_approval}}"},
                        "response_variable": "media_decision",
                        "options": ["approve", "cancel"],
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
            ],
        },
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace_id,
            "trigger_type": "manual",
            "config": {
                "workspace_attached": True,
                "chat_entrypoint": {"enabled": True, "title": "Review media", "wait_bridge": True},
            },
        },
    )).json()
    run = await _execute_queued_binding_run(client, headers, await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={"execute": True, "trigger_data": {"workspace_manual": True}},
    ))
    assert run["current_step_id"] == "approve_media"
    messages = (await client.get(f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers)).json()
    approval = next(
        message for message in messages
        if (message.get("pending_action") or {}).get("workflow_run_id") == run["id"]
    )
    # The server cannot write edits back into several variables, so the card says so.
    assert approval["pending_action"]["review_read_only"] is True

    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{approval['id']}/resolve",
        headers=headers,
        json={"choice": "approve", "payload": {"review": approval["pending_action"]["review"]}},
    )
    assert resolved.status_code == 200, resolved.text
    after = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert after["variables"]["media_decision"]["choice"] == "approve"


@pytest.mark.asyncio
async def test_workspace_manual_binding_run_requests_missing_starter_input(
    client: AsyncClient,
):
    headers = await _auth(client, "wfworkspacemanualstarter")
    workspace_id = await _create_workspace(
        client,
        headers,
        "Workspace manual starter input",
    )
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Publish existing video",
            "variables": {"workflow_project_id": ""},
            "steps": [
                {
                    "id": "trigger",
                    "type": "trigger",
                    "name": "Start",
                    "config": {
                        "run_inputs": [
                            {
                                "key": "workflow_project_id",
                                "type": "string",
                                "label": "Verified video project",
                                "required": True,
                            },
                        ],
                    },
                    "next": ["end"],
                },
                {
                    "id": "end",
                    "type": "end",
                    "name": "Done",
                    "config": {},
                    "next": [],
                },
            ],
        },
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace_id,
            "trigger_type": "manual",
            "config": {
                "workspace_attached": True,
                "chat_entrypoint": {
                    "enabled": True,
                    "title": "Publish an existing Stickman video",
                    "wait_bridge": True,
                },
            },
        },
    )).json()

    response = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={
            "execute": False,
            "trigger_data": {"workspace_manual": True},
        },
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "paused"
    assert run["current_step_id"] == "trigger"
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=headers,
    )).json()
    starter = next(
        message
        for message in messages
        if (message.get("pending_action") or {}).get("kind")
        == "workflow_starter_input"
    )
    assert starter["pending_action"]["workflow_run_id"] == run["id"]
    assert starter["pending_action"]["values"] == {}

    # The run page answers the same starter form, not a bare Resume.
    path = f"/api/v1/workflows/runs/{run['id']}"
    action = (await client.get(path, headers=headers)).json()["resume_action"]
    assert action["kind"] == "workflow_starter_input"
    assert action["message_id"] == starter["id"]
    assert action["message_workspace_id"] == workspace_id
    assert [field["key"] for field in action["inputs"]] == ["workflow_project_id"]
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{starter['id']}/resolve",
        headers=headers,
        json={"choice": "run", "payload": {"inputs": {"workflow_project_id": "project-1"}}},
    )
    assert response.status_code == 200, response.text
    detail = (await client.get(path, headers=headers)).json()
    assert detail["variables"]["workflow_project_id"] == "project-1"
    assert (detail.get("resume_action") or {}).get("kind") != "workflow_starter_input"


@pytest.mark.asyncio
async def test_workspace_manual_run_keeps_valid_inputs_when_one_is_rejected(client: AsyncClient):
    """One rejected answer re-asks only for it; a rule across inputs never drops the others."""
    headers = await _auth(client, "wfworkspacemanualpartial")
    workspace_id = await _create_workspace(client, headers, "Workspace manual partial inputs")
    platform = {"type": "boolean", "required": False, "default": False}
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Plan a release",
        "steps": [
            {"id": "trigger", "type": "trigger", "name": "Start", "config": {
                "run_inputs": [
                    {"key": "song_title", "type": "string", "label": "Song title", "required": True},
                    {"key": "start_at", "type": "string", "label": "Start time", "required": True,
                     "schema": {"type": "string", "format": "date-time"}},
                    {"key": "publish_youtube", "label": "YouTube", **platform},
                    {"key": "publish_tiktok", "label": "TikTok", **platform},
                ],
                "input_rules": [{
                    "anyOf": [
                        {"properties": {"publish_youtube": {"const": True}}, "required": ["publish_youtube"]},
                        {"properties": {"publish_tiktok": {"const": True}}, "required": ["publish_tiktok"]},
                    ],
                    "x-message": "Choose at least one platform.",
                }],
            }, "next": ["end"]},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"], "workspace_id": workspace_id, "trigger_type": "manual",
        "config": {"workspace_attached": True,
                   "chat_entrypoint": {"enabled": True, "title": "Plan a release", "wait_bridge": True}},
    })).json()

    response = await client.post(f"/api/v1/workflows/bindings/{binding['id']}/run", headers=headers, json={
        "variables": {"song_title": "Country", "start_at": "tomorrow", "publish_youtube": True},
        "trigger_data": {"workspace_manual": True},
        "execute": False,
    })

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "paused"
    messages = (await client.get(f"/api/v1/workspaces/{workspace_id}/chat/messages", headers=headers)).json()
    starter = next(
        message["pending_action"] for message in messages
        if (message.get("pending_action") or {}).get("kind") == "workflow_starter_input"
    )
    assert starter["values"]["song_title"] == "Country"
    assert starter["values"]["publish_youtube"] is True
    assert "start_at" not in starter["values"]
    assert set(starter["input_notes"]) == {"start_at"}


@pytest.mark.asyncio
async def test_workspace_manual_binding_run_projects_enqueue_failure(
    client: AsyncClient,
    monkeypatch,
):
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *_args, **_kwargs: False),
    )
    headers = await _auth(client, "wfworkspacemanualqueuefail")
    workspace_id = await _create_workspace(
        client,
        headers,
        "Workspace manual queue failure",
    )
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Queue failure projection",
            "steps": _simple_steps([]),
        },
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace_id,
            "trigger_type": "manual",
            "config": {
                "workspace_attached": True,
                "chat_entrypoint": {
                    "enabled": True,
                    "title": "Queue failure projection",
                    "wait_bridge": True,
                },
            },
        },
    )).json()

    response = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={
            "execute": False,
            "trigger_data": {"workspace_manual": True},
        },
    )

    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "failed"
    assert "could not be queued" in run["error"]
    messages = (await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=headers,
    )).json()
    activity = next(
        message
        for message in messages
        if message["message_kind"] == "workflow_activity"
    )
    assert activity["meta"]["workflow_run_id"] == run["id"]
    assert activity["meta"]["workflow_status"] == "failed"


@pytest.mark.asyncio
async def test_run_cards_name_the_flow_and_steps_in_each_viewers_language(
    client: AsyncClient,
    monkeypatch,
):
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    headers = await _auth(client, "wfrunviewerlanguage")
    workspace_id = (await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Viewer language"},
    )).json()["id"]
    steps = _simple_steps(["prepare"])
    steps[0]["name"] = "Intake"
    steps[0]["i18n"] = {"zh": {"name": "接收需求"}}
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers, json={"name": "Make a video", "steps": steps},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings", headers=headers, json={
            "workflow_id": workflow["id"], "workspace_id": workspace_id, "trigger_type": "manual",
            "name": "Make a video", "config": {"i18n": {"zh": {"name": "做视频"}}},
        },
    )).json()
    started = (await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run", headers=headers,
        json={"execute": False, "trigger_data": {"_workflow_display_i18n": {"zh": {"name": "spoof"}}}},
    ))
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    chinese = {**headers, "X-Language": "zh"}
    history = (await client.get(
        "/api/v1/workflows/runs", headers=chinese, params={"workspace_id": workspace_id},
    )).json()
    assert (history[0]["workflow_name"], history[0]["current_step_name"]) == ("做视频", "接收需求")
    detail = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=chinese)).json()
    assert detail["current_step_name"] == "接收需求"
    assert {node["id"]: node["name"] for node in detail["definition_snapshot"]["nodes"]}["trigger"] == "接收需求"

    english = (await client.get(
        "/api/v1/workflows/runs", headers=headers, params={"workspace_id": workspace_id},
    )).json()
    assert (english[0]["workflow_name"], english[0]["current_step_name"]) == ("Make a video", "Intake")
    # A translation is display text: it never changes which runs can resume on the Flow.
    from packages.core.services.workflow_run_trace import workflow_definition_fingerprint

    def fingerprint(steps):
        return workflow_definition_fingerprint(SimpleNamespace(id="flow", version=1, steps=steps))

    assert workflow["steps"][0]["i18n"] == {"zh": {"name": "接收需求"}}
    assert fingerprint(workflow["steps"]) == fingerprint(
        [{key: value for key, value in step.items() if key != "i18n"} for step in workflow["steps"]]
    )


@pytest.mark.asyncio
async def test_run_list_names_are_immutable_after_workflow_rename_and_delete(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    headers = await _auth(client, "wfrunimmutablenames")
    workspace_id = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Immutable run names"},
    )).json()["id"]
    steps = _simple_steps(["prepare"])
    steps[0]["name"] = "Frozen intake"
    steps[1]["name"] = "Frozen preparation"
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Frozen workflow name", "steps": steps},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace_id,
            "trigger_type": "manual",
        },
    )).json()

    started_response = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={
            "execute": False,
            "trigger_data": {
                "_workflow_name": "User supplied spoof",
                "_workflow_node_names": {"trigger": "User supplied spoof"},
            },
        },
    )
    assert started_response.status_code == 201, started_response.text
    started = started_response.json()
    assert started["trigger_data"]["_workflow_name"] == "Frozen workflow name"
    assert started["trigger_data"]["_workflow_node_names"] == {
        "trigger": "Frozen intake",
        "prepare": "Frozen preparation",
        "end": "Done",
    }
    assert started["workflow_name"] == "Frozen workflow name"
    assert started["current_step_name"] == "Frozen intake"

    renamed_steps = _simple_steps(["prepare"])
    renamed_steps[0]["name"] = "Mutable intake"
    renamed_steps[1]["name"] = "Mutable preparation"
    renamed = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"name": "Mutable workflow name", "steps": renamed_steps},
    )
    assert renamed.status_code == 200, renamed.text

    history_response = await client.get(
        "/api/v1/workflows/runs",
        headers=headers,
        params={"workspace_id": workspace_id},
    )
    assert history_response.status_code == 200, history_response.text
    history = history_response.json()
    assert history[0]["workflow_name"] == "Frozen workflow name"
    assert history[0]["current_step_name"] == "Frozen intake"
    assert "trigger_data" not in history[0]

    detached = await client.delete(
        f"/api/v1/workflows/bindings/{binding['id']}",
        headers=headers,
    )
    assert detached.status_code == 204, detached.text
    deleted = await client.delete(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    history_without_definition = (await client.get(
        "/api/v1/workflows/runs",
        headers=headers,
        params={"workspace_id": workspace_id},
    )).json()
    assert history_without_definition[0]["workflow_name"] == "Frozen workflow name"
    assert history_without_definition[0]["current_step_name"] == "Frozen intake"

    await WorkflowRunner().run(started["id"])
    executed_without_definition = (await client.get(
        f"/api/v1/workflows/runs/{started['id']}",
        headers=headers,
    )).json()
    assert executed_without_definition["status"] == "completed"
    assert executed_without_definition["step_results"]["prepare"]["status"] == "completed"


@pytest.mark.asyncio
async def test_run_family_marks_bounded_legacy_lineage_untrusted_and_incomplete(
    client: AsyncClient,
    db_session,
):
    from datetime import UTC, datetime, timedelta

    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfrunfamily")
    user = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Retry family", "steps": _simple_steps(["work"])},
    )).json()
    workspace_id = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Retry family workspace"},
    )).json()["id"]
    old_time = datetime.now(UTC) - timedelta(days=2)
    root_id = generate_ulid()
    selected_id = generate_ulid()
    child_id = generate_ulid()

    def run_row(
        run_id: str,
        *,
        created_at: datetime,
        attempt_number: int = 1,
        retry_of_run_id: str | None = None,
    ) -> WorkflowRun:
        trigger_data = {
            "attempt_number": attempt_number,
            "_workflow_name": "Retry family",
            "_workflow_node_names": {"work": "Step work"},
        }
        if retry_of_run_id:
            trigger_data["retry_of_run_id"] = retry_of_run_id
        return WorkflowRun(
            id=run_id,
            workflow_id=workflow["id"],
            entity_id=user["entity_id"],
            workspace_id=workspace_id,
            status="failed",
            current_step_id="work",
            variables={},
            step_results={},
            trigger_data=trigger_data,
            definition_snapshot={},
            execution_trace=[],
            started_by=user["id"],
            started_at=created_at,
            created_at=created_at,
        )

    family = [
        run_row(root_id, created_at=old_time),
        run_row(
            selected_id,
            created_at=old_time + timedelta(minutes=1),
            attempt_number=2,
            retry_of_run_id=root_id,
        ),
        run_row(
            child_id,
            created_at=old_time + timedelta(minutes=2),
            attempt_number=3,
            retry_of_run_id=selected_id,
        ),
    ]
    unrelated = [
        run_row(
            generate_ulid(),
            created_at=old_time + timedelta(days=1, minutes=index),
        )
        for index in range(105)
    ]
    db_session.add_all([*family, *unrelated])
    await db_session.commit()

    first_page = (await client.get(
        "/api/v1/workflows/runs",
        headers=headers,
        params={"workspace_id": workspace_id, "limit": 100},
    )).json()
    assert {root_id, selected_id, child_id}.isdisjoint({run["id"] for run in first_page})

    response = await client.get(
        f"/api/v1/workflows/runs/{selected_id}/family",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    related = response.json()
    assert [run["id"] for run in related] == [root_id, selected_id]
    assert all(
        run["lineage_status"] == "legacy_untrusted_incomplete"
        for run in related
    )
    assert all("definition_snapshot" not in run for run in related)
    assert all("execution_trace" not in run for run in related)

    bounded = await client.get(
        f"/api/v1/workflows/runs/{selected_id}/family",
        headers=headers,
        params={"limit": 2},
    )
    assert bounded.status_code == 200
    assert len(bounded.json()) == 2
    over_limit = await client.get(
        f"/api/v1/workflows/runs/{selected_id}/family",
        headers=headers,
        params={"limit": 201},
    )
    assert over_limit.status_code == 422

    other_headers = await _auth(client, "wfrunfamilyother")
    denied = await client.get(
        f"/api/v1/workflows/runs/{selected_id}/family",
        headers=other_headers,
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_run_family_over_200_attempts_includes_selected_and_latest_with_bounded_queries(
    db_session,
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import event

    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import list_run_family

    created_at = datetime.now(UTC) - timedelta(days=1)
    lineage_root_run_id = generate_ulid()
    parent_id = None
    run_ids: list[str] = []
    for index in range(220):
        run_id = lineage_root_run_id if index == 0 else generate_ulid()
        run_ids.append(run_id)
        db_session.add(WorkflowRun(
            id=run_id,
            workflow_id="workflow-deep-family",
            entity_id="entity-deep-family",
            lineage_root_run_id=lineage_root_run_id,
            lineage_is_legacy=False,
            retry_of_run_id=parent_id,
            retry_from_step_id="work" if parent_id else None,
            attempt_number=index + 1,
            status="failed",
            current_step_id="work",
            variables={},
            step_results={},
            trigger_data={},
            definition_snapshot={},
            execution_trace=[],
            created_at=created_at + timedelta(seconds=index),
        ))
        parent_id = run_id
    await db_session.commit()
    db_session.expunge_all()

    statements: list[str] = []
    sync_engine = db_session.bind.sync_engine

    def count_statement(_conn, _cursor, statement, _parameters, _context, _many):
        if "workflow_runs" in statement.lower():
            statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", count_statement)
    try:
        family = await list_run_family(
            db_session,
            run_ids[100],
            "entity-deep-family",
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", count_statement)

    assert len(family) == 200
    assert run_ids[100] in {run.id for run in family}
    assert run_ids[-1] in {run.id for run in family}
    assert family[-1].id == run_ids[-1]
    assert len(statements) <= 2
    assert all("recursive" not in statement.lower() for statement in statements)
    assert any("lineage_root_run_id" in statement for statement in statements)


@pytest.mark.asyncio
async def test_members_only_workspace_run_history_requires_read_access(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from datetime import UTC, datetime

    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.auth_service import create_access_token, hash_password

    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    owner_headers = await _auth(client, "wfprivatehistoryowner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace = (await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Private workflow history"},
    )).json()
    assert workspace["settings"]["access_mode"] == "members_only"
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={"name": "Private history workflow", "steps": _simple_steps(["work"])},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace["id"],
            "trigger_type": "manual",
        },
    )).json()
    private_run = (await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=owner_headers,
        json={"execute": False},
    )).json()
    entity_run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=owner_headers,
        json={"execute": False},
    )).json()

    outsider = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="wf-private-history-outsider@test.com",
        display_name="Private history outsider",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(outsider)
    await db_session.commit()
    outsider_headers = {
        "Authorization": f"Bearer {create_access_token(outsider.id, outsider.entity_id, outsider.role)}",
    }

    unscoped = await client.get("/api/v1/workflows/runs", headers=outsider_headers)
    assert unscoped.status_code == 200, unscoped.text
    visible_ids = {run["id"] for run in unscoped.json()}
    assert entity_run["id"] in visible_ids
    assert private_run["id"] not in visible_ids

    workflow_scoped = await client.get(
        f"/api/v1/workflows/{workflow['id']}/runs",
        headers=outsider_headers,
    )
    assert workflow_scoped.status_code == 200, workflow_scoped.text
    workflow_visible_ids = {run["id"] for run in workflow_scoped.json()}
    assert entity_run["id"] in workflow_visible_ids
    assert private_run["id"] not in workflow_visible_ids

    denied_requests = [
        await client.get(
            "/api/v1/workflows/runs",
            headers=outsider_headers,
            params={"workspace_id": workspace["id"]},
        ),
        await client.get(
            f"/api/v1/workflows/runs/{private_run['id']}",
            headers=outsider_headers,
        ),
        await client.get(
            f"/api/v1/workflows/runs/{private_run['id']}",
            headers=outsider_headers,
            params={"detail": "false"},
        ),
        await client.get(
            f"/api/v1/workflows/runs/{private_run['id']}/family",
            headers=outsider_headers,
        ),
    ]
    assert [response.status_code for response in denied_requests] == [404, 404, 404, 404]

    db_session.add(WorkspaceStaff(
        workspace_id=workspace["id"],
        user_id=outsider.id,
        role="viewer",
        added_by=owner["id"],
        added_at=datetime.now(UTC),
        status="active",
    ))
    await db_session.commit()

    allowed_requests = [
        await client.get(
            "/api/v1/workflows/runs",
            headers=outsider_headers,
            params={"workspace_id": workspace["id"]},
        ),
        await client.get(
            f"/api/v1/workflows/runs/{private_run['id']}",
            headers=outsider_headers,
        ),
        await client.get(
            f"/api/v1/workflows/runs/{private_run['id']}",
            headers=outsider_headers,
            params={"detail": "false"},
        ),
        await client.get(
            f"/api/v1/workflows/runs/{private_run['id']}/family",
            headers=outsider_headers,
        ),
    ]
    assert [response.status_code for response in allowed_requests] == [200, 200, 200, 200]
    assert private_run["id"] in {run["id"] for run in allowed_requests[0].json()}


@pytest.mark.asyncio
async def test_run_history_bulk_authorization_is_bounded_by_workspace(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from datetime import UTC, datetime, timedelta

    from apps.api.routers import workflows as workflow_routes
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.auth_service import create_access_token, hash_password

    owner_headers = await _auth(client, "wfbulkauthowner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace = (await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Bulk history authorization"},
    )).json()
    viewer = User(
        id=generate_ulid(),
        entity_id=owner["entity_id"],
        email="wf-bulk-history-viewer@test.com",
        display_name="Bulk history viewer",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    db_session.add(viewer)
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=workspace["id"],
        user_id=viewer.id,
        role="viewer",
        added_by=owner["id"],
        added_at=datetime.now(UTC),
        status="active",
    ))
    for index in range(30):
        db_session.add(WorkflowRun(
            id=generate_ulid(),
            workflow_id="workflow-bulk-history",
            entity_id=owner["entity_id"],
            workspace_id=workspace["id"],
            attempt_number=1,
            status="completed",
            variables={"large": "not needed"},
            step_results={"work": {"status": "completed"}},
            trigger_data={
                "_workflow_name": "Bulk history",
                "_workflow_node_names": {"work": "Work"},
            },
            definition_snapshot={},
            execution_trace=[],
            started_by=owner["id"],
            created_at=datetime.now(UTC) + timedelta(seconds=index),
        ))
    await db_session.commit()
    viewer_headers = {
        "Authorization": f"Bearer {create_access_token(viewer.id, viewer.entity_id, viewer.role)}",
    }

    control_checks: list[str] = []
    original_run_can_control = workflow_routes._run_can_control

    async def counted_run_can_control(db, run, user):
        control_checks.append(run.workspace_id)
        return await original_run_can_control(db, run, user)

    monkeypatch.setattr(workflow_routes, "_run_can_control", counted_run_can_control)
    response = await client.get(
        "/api/v1/workflows/runs",
        headers=viewer_headers,
        params={"workspace_id": workspace["id"], "limit": 50},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 30
    assert all(run["capabilities"] == {"can_control": False} for run in response.json())
    assert len(control_checks) <= 1


@pytest.mark.asyncio
async def test_workspace_run_controls_expose_capabilities_and_reject_viewers(
    client: AsyncClient,
    db_session,
):
    from datetime import UTC, datetime

    from packages.core.models.user import User
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.auth_service import create_access_token, hash_password

    owner_headers = await _auth(client, "wfcontrolowner")
    owner = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace = (await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Workflow controls"},
    )).json()
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={"name": "Controlled run", "steps": _simple_steps(["work"])},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace["id"],
            "trigger_type": "manual",
        },
    )).json()

    operator = User(
        entity_id=owner["entity_id"],
        email="wfcontroloperator@test.com",
        display_name="Workflow operator",
        password_hash=hash_password("pass123"),
        role="viewer",
        status="active",
    )
    viewer = User(
        entity_id=owner["entity_id"],
        email="wfcontrolviewer@test.com",
        display_name="Workflow viewer",
        password_hash=hash_password("pass123"),
        role="viewer",
        status="active",
    )
    db_session.add_all([operator, viewer])
    await db_session.flush()
    db_session.add_all([
        WorkspaceStaff(
            workspace_id=workspace["id"],
            user_id=operator.id,
            role="editor",
            added_by=owner["id"],
            added_at=datetime.now(UTC),
            status="active",
        ),
        WorkspaceStaff(
            workspace_id=workspace["id"],
            user_id=viewer.id,
            role="viewer",
            added_by=owner["id"],
            added_at=datetime.now(UTC),
            status="active",
        ),
    ])
    await db_session.commit()
    operator_headers = {
        "Authorization": f"Bearer {create_access_token(operator.id, operator.entity_id, operator.role)}"
    }
    viewer_headers = {
        "Authorization": f"Bearer {create_access_token(viewer.id, viewer.entity_id, viewer.role)}"
    }

    started = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=operator_headers,
        json={"execute": False},
    )
    assert started.status_code == 201, started.text
    run = started.json()

    operator_detail = await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=operator_headers,
    )
    viewer_detail = await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=viewer_headers,
    )
    assert operator_detail.json()["capabilities"] == {"can_control": True}
    assert viewer_detail.json()["capabilities"] == {"can_control": False}
    assert "definition_snapshot" in operator_detail.json()
    assert "execution_trace" in operator_detail.json()

    summary = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=viewer_headers,
    )
    assert summary.status_code == 200
    assert summary.json()["capabilities"] == {"can_control": False}
    for sensitive_or_heavy_key in (
        "variables",
        "step_results",
        "trigger_data",
        "definition_snapshot",
        "execution_trace",
    ):
        assert sensitive_or_heavy_key not in summary.json()

    listed = await client.get(
        f"/api/v1/workflows/runs?workspace_id={workspace['id']}",
        headers=viewer_headers,
    )
    assert listed.status_code == 200
    listed_run = next(item for item in listed.json() if item["id"] == run["id"])
    assert listed_run["capabilities"] == {"can_control": False}

    for endpoint in ("step", "cancel", "resume", "retry"):
        denied = await client.post(
            f"/api/v1/workflows/runs/{run['id']}/{endpoint}",
            headers=viewer_headers,
            json={} if endpoint in {"resume", "retry"} else None,
        )
        assert denied.status_code == 403, (endpoint, denied.text)

    unchanged = await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=operator_headers,
    )
    assert unchanged.json()["status"] == run["status"]

    cancelled = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/cancel",
        headers=operator_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_run_summary_does_not_use_the_full_detail_loader(
    client: AsyncClient,
    monkeypatch,
):
    headers = await _auth(client, "wfcompactsummary")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Compact summary", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    async def reject_full_detail_loader(*args, **kwargs):
        raise AssertionError("summary polling must not load full Run detail")

    monkeypatch.setattr(
        "apps.api.routers.workflows.svc.get_run",
        reject_full_detail_loader,
    )
    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["id"] == run["id"]
    assert [step["id"] for step in summary["workflow_steps"]] == [
        "trigger",
        "work",
        "end",
    ]
    assert [step["status"] for step in summary["workflow_steps"]] == [
        "running",
        "pending",
        "pending",
    ]
    # The transform is routing, not a stage, so this flow has no stages to count.
    assert summary["processed_count"] == 0
    assert summary["total_count"] == 0
    assert summary["business_outcome"] == "in_progress"
    assert summary["intervention"] is None
    for heavy_key in (
        "variables",
        "step_results",
        "definition_snapshot",
        "execution_trace",
    ):
        assert heavy_key not in summary


@pytest.mark.asyncio
async def test_legacy_run_summary_recovers_trace_order_and_not_reached_nodes(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wflegacysummaryorder")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Legacy summary order",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["review"]},
                {"id": "publish", "type": "agent", "next": []},
                {"id": "unused", "type": "stop", "next": []},
                {"id": "review", "type": "wait", "next": ["publish"]},
            ],
        },
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.status = "failed"
    stored.current_step_id = "publish"
    stored.step_results = {
        "start": {"status": "completed"},
        "review": {"status": "completed"},
        "publish": {"status": "failed", "error": "Upload failed"},
    }
    stored.execution_trace = [
        {"sequence": 1, "node_id": "start", "status": "completed"},
        {"sequence": 2, "node_id": "review", "status": "completed"},
        {"sequence": 3, "node_id": "publish", "status": "failed"},
    ]
    stored.trigger_data = {
        **(stored.trigger_data or {}),
        "_workflow_history_summary": {
            "business_outcome": "",
            "processed_count": 3,
            "total_count": 3,
            "artifact_count": 0,
            "blocker": "Upload failed",
        },
    }
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert [step["id"] for step in summary["workflow_steps"]] == [
        "start",
        "review",
        "publish",
        "unused",
    ]
    # Stages only: the review wait and the publish agent. The start and the stop are routing.
    assert summary["processed_count"] == 2
    assert summary["total_count"] == 2
    assert "execution_trace" not in summary


@pytest.mark.asyncio
async def test_run_detail_exposes_business_outcome(client: AsyncClient, db_session):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfdetailoutcome")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Detailed outcome", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.variables = {"project": {"state": {"business_outcome": "needs_input"}}}
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["business_outcome"] == "needs_input"


@pytest.mark.asyncio
async def test_run_progress_is_measured_against_the_longest_route_through_the_flow(client: AsyncClient, db_session):
    """Three stages in the Flow, two on its longest route. The run took the short branch,
    and the total is still the longest route, not the path taken or every stage."""
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfprogressgraph")

    def stage(step_id: str, **routing) -> dict:
        return {"id": step_id, "type": "code", "name": step_id, "config": {"code": "result = {}"}, **routing}

    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["gate"]},
        {"id": "gate", "type": "condition", "name": "Gate", "config": {"expression": "false"},
         "true_next": ["review"], "false_next": ["publish"]},
        stage("review", next=["review_more"]),
        stage("review_more", next=["end"]),
        stage("publish", next=["end"]),
        {"id": "end", "type": "end", "name": "End", "config": {}, "next": []},
    ]
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers, json={"name": "Progress graph", "steps": steps},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={"execute": False},
    )).json()
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.status = "running"
    stored.step_results = {
        "gate": {"status": "completed", "output": "false"},
        "publish": {"status": "completed", "output": "{}"},
    }
    await db_session.commit()

    # The Workspace App reads the compact run state, which carries the progress counts.
    response = await client.get(f"/api/v1/workflows/runs/{run['id']}?detail=false", headers=headers)

    assert response.status_code == 200, response.text
    assert (response.json()["processed_count"], response.json()["total_count"]) == (1, 2)


@pytest.mark.asyncio
async def test_runs_created_before_the_longest_route_keep_their_stage_total(client: AsyncClient, db_session):
    """A run started before the change keeps the whole-Flow stage count it was shown with."""
    from datetime import datetime, timezone

    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfprogressolder")

    def stage(step_id: str, **routing) -> dict:
        return {"id": step_id, "type": "code", "name": step_id, "config": {"code": "result = {}"}, **routing}

    steps = [
        {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["gate"]},
        {"id": "gate", "type": "condition", "name": "Gate", "config": {"expression": "false"},
         "true_next": ["review"], "false_next": ["publish"]},
        stage("review", next=["review_more"]),
        stage("review_more", next=["end"]),
        stage("publish", next=["end"]),
        {"id": "end", "type": "end", "name": "End", "config": {}, "next": []},
    ]
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers, json={"name": "Older progress graph", "steps": steps},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={"execute": False},
    )).json()
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stored.status = "running"
    stored.step_results = {"publish": {"status": "completed", "output": "{}"}}
    await db_session.commit()

    response = await client.get(f"/api/v1/workflows/runs/{run['id']}?detail=false", headers=headers)

    assert response.status_code == 200, response.text
    assert (response.json()["processed_count"], response.json()["total_count"]) == (1, 3)


def test_longest_route_counts_stages_through_branches_and_cuts_loops():
    from packages.core.services.workflow_run_trace import workflow_longest_path_stages

    branching = [
        {"id": "trigger", "type": "trigger", "targets": ["gate"]},
        {"id": "gate", "type": "condition", "targets": ["short", "chain"]},
        {"id": "short", "type": "tool", "targets": ["end"]},
        {"id": "chain", "type": "tool", "targets": ["middle"]},
        {"id": "middle", "type": "agent", "targets": ["end"]},
        {"id": "end", "type": "end", "targets": []},
    ]
    # The loop-back edge from "second" to "first" is cut, so the body counts once.
    looping = [
        {"id": "trigger", "type": "trigger", "targets": ["first"]},
        {"id": "first", "type": "tool", "targets": ["second"]},
        {"id": "second", "type": "tool", "targets": ["first", "end"]},
        {"id": "end", "type": "end", "targets": []},
    ]

    assert workflow_longest_path_stages(branching) == 2
    assert workflow_longest_path_stages(looping) == 2
    assert workflow_longest_path_stages([]) == 0


@pytest.mark.asyncio
async def test_list_runs_exposes_persisted_history_summary(client: AsyncClient, db_session):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wflisthistorysummary")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "History summary", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.trigger_data = {
        **(stored.trigger_data or {}),
        "_workflow_history_summary": {
            "schema_version": 2,
            "business_outcome": "needs_input",
            "processed_count": 2,
            "total_count": 3,
            "workflow_steps": [
                {"id": "start", "name": "Start", "type": "trigger", "status": "completed"},
                {"id": "review", "name": "Review", "type": "wait", "status": "completed"},
                {"id": "publish", "name": "Publish", "type": "agent", "status": "running"},
            ],
            "artifact_count": 4,
            "blocker": "Browser disconnected",
        },
    }
    await db_session.commit()

    response = await client.get("/api/v1/workflows/runs", headers=headers)

    assert response.status_code == 200, response.text
    listed = next(item for item in response.json() if item["id"] == run["id"])
    assert listed["business_outcome"] == "needs_input"
    assert listed["processed_count"] == 2
    assert listed["total_count"] == 3
    assert [step["id"] for step in listed["workflow_steps"]] == [
        "start",
        "review",
        "publish",
    ]
    assert listed["artifact_count"] == 4
    assert listed["history_blocker"] == "Browser disconnected"
    for heavy_key in ("variables", "step_results", "definition_snapshot", "execution_trace"):
        assert heavy_key not in listed


def test_legacy_history_summary_does_not_expose_incompatible_progress_counts():
    from types import SimpleNamespace

    from apps.api.routers.workflows import _persisted_workflow_history_state

    state = _persisted_workflow_history_state(SimpleNamespace(
        summary_history_state={
            "business_outcome": "needs_input",
            "processed_count": 9,
            "total_count": 13,
            "artifact_count": 1,
            "blocker": "Legacy blocker",
        },
    ))

    assert state["processed_count"] is None
    assert state["total_count"] is None
    assert state["workflow_steps"] is None
    assert state["artifact_count"] == 1


@pytest.mark.asyncio
async def test_run_summary_preserves_current_paused_step_reason(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcompactpausedreason")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Compact paused reason", "steps": _simple_steps(["handoff"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    blocker = (
        "Browser readiness issue:\n"
        "Chrome session is not paired. Start the local browser worker, then retry."
    )
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.status = "paused"
    stored.current_step_id = "handoff"
    stored.step_results = {
        "handoff": {
            "status": "paused",
            "step_id": "handoff",
            "wait_type": "event",
            "output": blocker,
        },
    }
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["intervention"] == {
        "kind": "workflow_resume",
        "workflow_run_id": run["id"],
        "workflow_binding_id": None,
        "step_id": "handoff",
        "observed_problem": blocker,
        "options": ["resume", "cancel"],
        **_RESUME_RECOMMENDED,
    }
    assert "step_results" not in summary
    assert "execution_trace" not in summary


@pytest.mark.asyncio
async def test_run_summary_exposes_same_run_resume_for_reexecute_pause(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfreexecuteintervention")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Reexecute paused step", "steps": _simple_steps(["publish"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    blocker = "Chrome control paused after the user took over the browser."
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.status = "paused"
    stored.current_step_id = "publish"
    stored.step_results = {
        "publish": {
            "status": "paused",
            "step_id": "publish",
            "resume_strategy": "reexecute",
            "error": blocker,
            "chrome_outcome": {"status": "needs_user"},
        },
    }
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["status"] == "paused"
    assert summary["intervention"] == {
        "kind": "workflow_resume",
        "workflow_run_id": run["id"],
        "workflow_binding_id": None,
        "step_id": "publish",
        "observed_problem": blocker,
        "options": ["resume", "cancel"],
        **_RESUME_RECOMMENDED,
    }
    assert "step_results" not in summary
    assert "execution_trace" not in summary


@pytest.mark.asyncio
async def test_a_run_paused_for_an_automatic_retry_asks_nobody_to_resume(client: AsyncClient, db_session):
    """A temporary problem is retried on its own: no Attention required, no Resume, and the step is running."""
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wftransientretrypause")
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers,
        json={"name": "Retrying step", "steps": _simple_steps(["plan"])},
    )).json()
    run = (await client.post(f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={"execute": False})).json()
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.status = "paused"
    stored.current_step_id = "plan"
    stored.step_results = {"plan": {
        "status": "paused", "wait_type": "transient_retry", "code": "workflow_step_transient_retry",
        "resume_strategy": "reexecute", "auto_resume_scheduled": True,
        "output": "Retrying (2/3): the credit and budget check was temporarily unavailable.",
    }}
    await db_session.commit()

    summary = (await client.get(f"/api/v1/workflows/runs/{run['id']}?detail=false", headers=headers)).json()

    assert summary["status"] == "paused" and summary["intervention"] is None
    assert next(step for step in summary["workflow_steps"] if step["id"] == "plan")["status"] == "running"


@pytest.mark.asyncio
async def test_chrome_reexecute_resume_passes_prior_outcome_to_same_run(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import func, select

    from packages.core.ai import workflow_runner as runner_module
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfchromesamerunresume")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Chrome same-run recovery",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["publish"]},
                {
                    "id": "publish",
                    "type": "agent",
                    "config": {
                        "skill": "chrome",
                        "prompt": "Publish the approved video.",
                        "output_format": "json",
                        "output_schema": {
                            "type": "object",
                            "required": ["success"],
                            "properties": {"success": {"type": "boolean"}},
                            "additionalProperties": False,
                        },
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    created = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    workflow_run_id = created["id"]
    prompts = []

    async def invoke_skill(_db, _skill, _entity_id, input_text, **_kwargs):
        prompts.append(input_text)
        if len(prompts) == 1:
            return {
                "content": "Chrome control was interrupted.",
                "stop_reason": "completed",
                "control": {},
                "chrome_outcome": {
                    "status": "needs_user",
                    "run_id": "chrome-run-1",
                    "summary": "Chrome control was interrupted.",
                    "browser_group": {"group_id": "browser-group-1"},
                    "resume_context": {"next": "reobserve_and_continue"},
                    "retryable": True,
                },
            }
        assert '"same_workflow_run": true' in input_text
        assert f'"workflow_run_id": "{workflow_run_id}"' in input_text
        assert '"group_id": "browser-group-1"' in input_text
        assert '"summary": "Chrome control was interrupted."' in input_text
        assert '"evidence"' not in input_text
        assert "First re-observe the target before any mutation" in input_text
        assert "references require current observation" in input_text
        return {
            "content": '{"success":true}',
            "stop_reason": "completed",
            "control": {},
            "chrome_outcome": {
                "status": "completed",
                "run_id": "chrome-run-1",
            },
        }

    monkeypatch.setattr(runner_module, "runtime_invoke_skill", invoke_skill)

    await WorkflowRunner().run(created["id"])
    compact = (await client.get(
        f"/api/v1/workflows/runs/{created['id']}?detail=false",
        headers=headers,
    )).json()
    assert compact["intervention"]["kind"] == "workflow_resume"

    response = await client.post(
        f"/api/v1/workflows/runs/{created['id']}/resume",
        headers=headers,
        json={},
    )

    assert response.status_code == 200, response.text
    resumed = response.json()
    assert resumed["id"] == created["id"]
    assert resumed["status"] == "running"
    await WorkflowRunner().run(created["id"])
    resumed = (await client.get(
        f"/api/v1/workflows/runs/{created['id']}",
        headers=headers,
    )).json()
    assert resumed["status"] == "completed"
    assert len(prompts) == 2
    assert await db_session.scalar(
        select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow["id"]
        )
    ) == 1


@pytest.mark.asyncio
async def test_run_summary_describes_paused_child_workflows_without_step_output(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcompactpausedchildren")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Compact paused children", "steps": _simple_steps(["collect"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    stored = await db_session.get(WorkflowRun, run["id"])
    stored.status = "paused"
    stored.current_step_id = "collect"
    child = WorkflowRun(
        id="child-paused-summary",
        workflow_id=stored.workflow_id,
        entity_id=stored.entity_id,
        workspace_id=stored.workspace_id,
        status="paused",
        current_step_id="collect",
        variables={},
        step_results={
            "collect": {
                "status": "paused",
                "resume_strategy": "reexecute",
                "error": "Chrome action approval is required.",
            },
        },
        trigger_data={
            "parent_run_id": stored.id,
            "parent_step_id": "collect",
        },
        started_by=stored.started_by,
    )
    stored.step_results = {
        "collect": {
            "status": "paused",
            "step_id": "collect",
            "subrun_id": child.id,
        },
    }
    db_session.add(child)
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["intervention"] == {
        "kind": "workflow_resume",
        "workflow_run_id": child.id,
        "workflow_binding_id": None,
        "step_id": "collect",
        "observed_problem": "Chrome action approval is required.",
        "options": ["resume", "cancel"],
        **_RESUME_RECOMMENDED,
    }

    child.trigger_data = {
        "parent_run_id": "foreign-parent",
        "parent_step_id": "collect",
    }
    await db_session.commit()
    foreign_child_response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )
    assert foreign_child_response.status_code == 200, foreign_child_response.text
    assert foreign_child_response.json()["intervention"] is None


@pytest.mark.asyncio
async def test_run_summary_offers_the_review_a_paused_child_workflow_waits_on(
    client: AsyncClient,
    db_session,
):
    # A run that makes a video in a sub-flow is paused there; its card answers the child's review.
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcompactchildreview")
    parent_flow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Parent of a review", "steps": _simple_steps(["make"]),
    })).json()
    child_flow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Child review", "steps": [
            {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["approve"]},
            {"id": "approve", "type": "wait", "name": "Review the storyboard", "next": ["end"],
             "config": {"wait_type": "approval", "message": "Check the storyboard.",
                        "options": ["accept", "revise"], "response_variable": "decision"}},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    parent = (await client.post(f"/api/v1/workflows/{parent_flow['id']}/run", headers=headers,
                                json={"execute": False})).json()
    child = (await client.post(f"/api/v1/workflows/{child_flow['id']}/run", headers=headers,
                               json={"execute": False})).json()
    stored_child = await db_session.get(WorkflowRun, child["id"])
    stored_child.status = "paused"
    stored_child.current_step_id = "approve"
    stored_child.step_results = {"approve": {"status": "paused", "output": "Check the storyboard."}}
    stored_child.trigger_data = {**(stored_child.trigger_data or {}), "parent_run_id": parent["id"],
                                 "parent_step_id": "make"}
    stored_parent = await db_session.get(WorkflowRun, parent["id"])
    stored_parent.status = "paused"
    stored_parent.current_step_id = "make"
    stored_parent.step_results = {"make": {"status": "paused", "step_id": "make", "subrun_id": child["id"]}}
    await db_session.commit()

    summary = await client.get(f"/api/v1/workflows/runs/{parent['id']}?detail=false", headers=headers)
    assert summary.status_code == 200, summary.text
    intervention = summary.json()["intervention"]
    assert intervention["kind"] == "workflow_approval"
    assert intervention["workflow_run_id"] == child["id"]
    assert intervention["options"] == ["accept", "revise"]


@pytest.mark.asyncio
async def test_run_family_does_not_use_the_full_detail_loader(
    client: AsyncClient,
    monkeypatch,
):
    headers = await _auth(client, "wfcompactfamily")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Compact family", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    async def reject_full_detail_loader(*args, **kwargs):
        raise AssertionError("family lookup must not load full Run detail")

    monkeypatch.setattr(
        "apps.api.routers.workflows.svc.get_run",
        reject_full_detail_loader,
    )
    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}/family",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [run["id"]]


@pytest.mark.asyncio
async def test_list_runs_defers_detail_payloads_without_summary_lazy_load(db_session):
    from sqlalchemy import inspect

    from apps.api.routers.workflows import _run_to_dict
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import get_run, list_runs

    run = WorkflowRun(
        id="run-deferred-detail",
        workflow_id="workflow-deferred-detail",
        entity_id="entity-deferred-detail",
        status="completed",
        variables={},
        step_results={},
        trigger_data={
            "webhook_body": "x" * 50_000,
            "_workflow_name": "Deferred detail workflow",
            "_workflow_node_names": {"start": "Start"},
            "_workflow_history_summary": {
                "business_outcome": "needs_input",
                "processed_count": 1,
                "total_count": 2,
                "artifact_count": 3,
                "blocker": "Safe persisted blocker",
            },
        },
        definition_snapshot={"workflow_id": "workflow-deferred-detail"},
        execution_trace=[{"sequence": 1, "node_id": "start"}],
        current_step_id="start",
        error=(
            "-----BEGIN " + "PRIVATE KEY-----\n"
            + ("raw-secret-error" * 1_000)
            + "\n-----END PRIVATE KEY-----"
        ),
    )
    db_session.add(run)
    await db_session.commit()
    db_session.expunge_all()

    listed = (await list_runs(
        db_session,
        "entity-deferred-detail",
        summary=True,
    ))[0]
    summary_unloaded = {
        "variables",
        "step_results",
        "trigger_data",
        "definition_snapshot",
        "execution_trace",
        "error",
    }
    assert summary_unloaded <= inspect(listed).unloaded

    summary = _run_to_dict(listed, include_detail=False, summary=True)
    assert "definition_snapshot" not in summary
    assert "execution_trace" not in summary
    assert summary["workflow_name"] == "Deferred detail workflow"
    assert summary["current_step_name"] == "Start"
    assert summary["history_blocker"] == "Safe persisted blocker"
    assert summary["error"] == "Safe persisted blocker"
    assert "BEGIN PRIVATE KEY" not in summary["error"]
    assert "raw-secret-error" not in json.dumps(summary)
    assert summary_unloaded <= inspect(listed).unloaded

    db_session.expunge(listed)
    detail = await get_run(db_session, run.id, run.entity_id)
    assert detail is not None
    assert detail.definition_snapshot == {"workflow_id": "workflow-deferred-detail"}
    assert detail.execution_trace == [{"sequence": 1, "node_id": "start"}]


@pytest.mark.asyncio
async def test_cancel_run(client: AsyncClient):
    headers = await _auth(client, "wfuser5")
    steps = _simple_steps(["c1", "c2"])
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Cancel test", "steps": steps,
    })).json()

    run = (await client.post(f"/api/v1/workflows/{wf['id']}/run", headers=headers, json={"execute": False})).json()
    assert run["status"] == "running"

    # Cancel it
    cancel_resp = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/cancel", headers=headers
    )
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    # Trying to execute a step on cancelled run should fail
    step_resp = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/step", headers=headers
    )
    data = step_resp.json()
    assert data.get("error") == "Run not active"

    # Cancellation is idempotent so stale clients can reconcile terminal state.
    cancel2 = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/cancel", headers=headers
    )
    assert cancel2.status_code == 200
    assert cancel2.json()["id"] == run["id"]
    assert cancel2.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_parent_cascades_to_active_subworkflow_descendants(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.constants.execution import ExecutionClaimKind
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution_claim import RuntimeExecutionClaim
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcancelchildren")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancel child tree", "steps": _simple_steps(["work"])},
    )).json()
    parent = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    owner = (await db_session.execute(
        select(User).where(User.email == "wfcancelchildren@test.com")
    )).scalar_one()
    parent_row = await db_session.get(WorkflowRun, parent["id"])

    def child_run(*, status: str, parent_run_id: str) -> WorkflowRun:
        run_id = generate_ulid()
        return WorkflowRun(
            id=run_id,
            workflow_id=workflow["id"],
            entity_id=owner.entity_id,
            status=status,
            current_step_id="work",
            variables={},
            step_results={},
            trigger_data={
                "parent_run_id": parent_run_id,
                "parent_step_id": "child",
            },
            execution_trace=[],
            started_by=owner.id,
            completed_at=(
                datetime.now(timezone.utc)
                if status == "completed"
                else None
            ),
        )

    pending_child = child_run(status="pending", parent_run_id=parent_row.id)
    running_child = child_run(status="running", parent_run_id=parent_row.id)
    paused_child = child_run(status="paused", parent_run_id=parent_row.id)
    completed_child = child_run(status="completed", parent_run_id=parent_row.id)
    grandchild = child_run(status="running", parent_run_id=paused_child.id)
    parent_row.current_step_id = "child"
    parent_row.step_results = {
        "child": {
            "status": "paused",
            "subrun_id": running_child.id,
        }
    }
    db_session.add_all([
        pending_child,
        running_child,
        paused_child,
        completed_child,
        grandchild,
    ])
    claimed_run_ids = {
        parent_row.id,
        pending_child.id,
        running_child.id,
        paused_child.id,
        grandchild.id,
    }
    db_session.add_all([
        RuntimeExecutionClaim(
            claim_key=f"{ExecutionClaimKind.WORKFLOW_RUN.value}:{run_id}",
            claim_token=generate_ulid(),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        for run_id in claimed_run_ids
    ])
    unrelated_claim = RuntimeExecutionClaim(
        claim_key=f"{ExecutionClaimKind.AGENT_TASK.value}:{generate_ulid()}",
        claim_token=generate_ulid(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(unrelated_claim)
    await db_session.commit()

    cancelled = await client.post(
        f"/api/v1/workflows/runs/{parent_row.id}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    for active_child in (
        pending_child,
        running_child,
        paused_child,
        grandchild,
    ):
        await db_session.refresh(active_child)
        assert active_child.status == "cancelled"
        assert (
            active_child.trigger_data["_workflow_run_control"]["cancelled_by"]
            == owner.id
        )
    await db_session.refresh(completed_child)
    assert completed_child.status == "completed"
    remaining_claims = set((await db_session.execute(
        select(RuntimeExecutionClaim.claim_key)
    )).scalars().all())
    assert not {
        f"{ExecutionClaimKind.WORKFLOW_RUN.value}:{run_id}"
        for run_id in claimed_run_ids
    } & remaining_claims
    assert unrelated_claim.claim_key in remaining_claims

    # A stale child worker may commit after cancellation. Its terminal receipt
    # must never move the cancelled parent back into a runnable state.
    running_child.status = "completed"
    running_child.completed_at = datetime.now(timezone.utc)
    await db_session.commit()
    await WorkflowRunner()._propagate_subworkflow_result(running_child, db_session)
    await db_session.refresh(parent_row)
    assert parent_row.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_run_revokes_claim_and_stops_in_flight_worker(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.services import workflow_run_execution_claim as claim_service

    headers = await _auth(client, "wfcancelworker")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancel active worker", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    work_started = asyncio.Event()
    work_cancelled = asyncio.Event()
    original_execute = WorkflowRunner._execute_step_safe

    async def blocking_work(self, step, workflow_run, db):
        if step["id"] == "work":
            work_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                work_cancelled.set()
                raise
        return await original_execute(self, step, workflow_run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", blocking_work)
    monkeypatch.setattr(
        claim_service,
        "_WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS",
        0.01,
    )

    worker = asyncio.create_task(WorkflowRunner().run(run["id"]))
    await asyncio.wait_for(work_started.wait(), timeout=2)

    cancelled = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert await asyncio.wait_for(worker, timeout=2) == "executed"
    assert work_cancelled.is_set()

    detail = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert detail["status"] == "cancelled"
    assert detail["error"] is None


@pytest.mark.asyncio
async def test_cancel_parent_returns_while_child_row_is_locked(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.constants.execution import ExecutionClaimKind
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution_claim import RuntimeExecutionClaim
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcancellockedchildreal")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancel real locked child", "steps": _simple_steps(["work"])},
    )).json()
    parent = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    owner = (await db_session.execute(
        select(User).where(User.email == "wfcancellockedchildreal@test.com")
    )).scalar_one()
    child = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow["id"],
        entity_id=owner.entity_id,
        status="running",
        current_step_id="work",
        variables={},
        step_results={},
        trigger_data={
            "parent_run_id": parent["id"],
            "parent_step_id": "child",
        },
        execution_trace=[],
        started_by=owner.id,
    )
    db_session.add(child)
    db_session.add(RuntimeExecutionClaim(
        claim_key=f"{ExecutionClaimKind.WORKFLOW_RUN.value}:{child.id}",
        claim_token=generate_ulid(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    await db_session.commit()

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with sessions() as lock_db:
        locked_child = (await lock_db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.id == child.id)
            .with_for_update()
        )).scalar_one()
        try:
            # Keep the child lock until the response; a blocking cancellation
            # still times out. Allow a loaded runner time for unrelated HTTP work.
            cancelled = await asyncio.wait_for(
                client.post(
                    f"/api/v1/workflows/runs/{parent['id']}/cancel",
                    headers=headers,
                ),
                timeout=30,
            )
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
            assert locked_child.status == "running"
        finally:
            await lock_db.rollback()

    await db_session.refresh(child)
    assert child.status == "running"
    claim = await db_session.get(
        RuntimeExecutionClaim,
        f"{ExecutionClaimKind.WORKFLOW_RUN.value}:{child.id}",
    )
    assert claim is None

    assert await WorkflowRunner().run(child.id) == "executed"
    await db_session.refresh(child)
    assert child.status == "cancelled"
    assert child.error is None
    assert child.step_results == {}
    assert child.terminal_effects_completed_at is not None


@pytest.mark.asyncio
async def test_child_run_does_not_execute_after_parent_was_cancelled(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcancelancestor")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancelled ancestor", "steps": _simple_steps(["work"])},
    )).json()
    parent = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    cancelled = await client.post(
        f"/api/v1/workflows/runs/{parent['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    owner = (await db_session.execute(
        select(User).where(User.email == "wfcancelancestor@test.com")
    )).scalar_one()
    child = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow["id"],
        entity_id=owner.entity_id,
        status="running",
        current_step_id="work",
        variables={},
        step_results={},
        trigger_data={
            "parent_run_id": parent["id"],
            "parent_step_id": "child",
        },
        execution_trace=[],
        started_by=owner.id,
    )
    db_session.add(child)
    await db_session.commit()

    assert await WorkflowRunner().run(child.id) == "executed"
    await db_session.refresh(child)
    assert child.status == "cancelled"
    assert child.error is None
    assert child.step_results == {}


@pytest.mark.asyncio
async def test_claim_loss_reports_cancelled_when_parent_was_cancelled(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfclaimlosscancelledancestor")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Claim loss cancelled ancestor", "steps": _simple_steps(["work"])},
    )).json()
    parent = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    cancelled = await client.post(
        f"/api/v1/workflows/runs/{parent['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    owner = (await db_session.execute(
        select(User).where(
            User.email == "wfclaimlosscancelledancestor@test.com",
        )
    )).scalar_one()
    child = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow["id"],
        entity_id=owner.entity_id,
        status="running",
        current_step_id="work",
        variables={},
        step_results={},
        trigger_data={
            "parent_run_id": parent["id"],
            "parent_step_id": "child",
        },
        execution_trace=[],
        started_by=owner.id,
    )
    db_session.add(child)
    await db_session.commit()

    settled_status = await WorkflowRunner._recover_lost_execution_claim(child.id)

    assert settled_status == "cancelled"
    await db_session.refresh(child)
    assert child.status == "cancelled"
    assert child.error is None


@pytest.mark.asyncio
async def test_cancelled_parent_claim_loss_settles_active_child(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.ai import workflow_runner as runner_module
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_execution_claim import (
        WorkflowRunExecutionClaimLost,
    )

    headers = await _auth(client, "wfcancelparentclaimloss")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancelled parent claim loss", "steps": _simple_steps(["work"])},
    )).json()
    parent = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    cancelled = await client.post(
        f"/api/v1/workflows/runs/{parent['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    owner = (await db_session.execute(
        select(User).where(User.email == "wfcancelparentclaimloss@test.com")
    )).scalar_one()
    child = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow["id"],
        entity_id=owner.entity_id,
        status="running",
        current_step_id="work",
        variables={},
        step_results={},
        trigger_data={
            "parent_run_id": parent["id"],
            "parent_step_id": "child",
        },
        execution_trace=[],
        started_by=owner.id,
    )
    db_session.add(child)
    await db_session.commit()

    async def lose_parent_claim(*_args, **_kwargs):
        raise WorkflowRunExecutionClaimLost("lost parent claim")

    monkeypatch.setattr(
        runner_module.WorkflowRunner,
        "_run_claimed",
        lose_parent_claim,
    )

    assert await WorkflowRunner().run(parent["id"]) == "executed"

    await db_session.refresh(child)
    assert child.status == "cancelled"
    assert child.error is None
    assert child.terminal_effects_completed_at is not None


@pytest.mark.asyncio
async def test_cancel_run_returns_committed_state_when_effects_are_deferred(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfcanceldefer")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Deferred cancel effects", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    async def fail_effects(_runner, _run, _db):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(WorkflowRunner, "_finalize_run_effects", fail_effects)

    cancelled = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/cancel",
        headers=headers,
    )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    persisted = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert persisted["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_run_accepts_completed_actionable_outcome_but_not_normal_completion(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wf_cancel_actionable")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancel actionable", "steps": _simple_steps(["work"])},
    )).json()

    actionable = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    actionable_row = await db_session.get(WorkflowRun, actionable["id"])
    actionable_row.status = "completed"
    actionable_row.variables = {
        "project": {
            "state": {
                "business_outcome": "needs_input",
                "retry_state": {"retry_from_step_id": "work"},
            },
        },
    }
    await db_session.commit()

    cancelled = await client.post(
        f"/api/v1/workflows/runs/{actionable['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    completed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    completed_row = await db_session.get(WorkflowRun, completed["id"])
    completed_row.status = "completed"
    completed_row.variables = {"project": {"state": {"business_outcome": "completed"}}}
    await db_session.commit()

    rejected = await client.post(
        f"/api/v1/workflows/runs/{completed['id']}/cancel",
        headers=headers,
    )
    assert rejected.status_code == 400
    assert "already completed" in rejected.text.lower()


@pytest.mark.asyncio
async def test_cancel_run_targets_latest_retry_attempt_from_stale_family_id(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wf_cancel_stale_attempt")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancel latest only", "steps": _simple_steps(["work"])},
    )).json()
    first = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    first_row = await db_session.get(WorkflowRun, first["id"])
    first_row.status = "failed"
    first_row.current_step_id = "work"
    await db_session.commit()

    retried = await client.post(
        f"/api/v1/workflows/runs/{first['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False},
    )
    assert retried.status_code == 201, retried.text

    cancelled = await client.post(
        f"/api/v1/workflows/runs/{first['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["id"] == retried.json()["id"]
    assert cancelled.json()["status"] == "cancelled"

    await db_session.refresh(first_row)
    retried_row = await db_session.get(WorkflowRun, retried.json()["id"])
    assert first_row.status == "failed"
    assert retried_row.status == "cancelled"


@pytest.mark.asyncio
async def test_run_detail_enriches_visible_artifact_document_names(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.document import Document
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wf_artifact_names")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Artifact names", "steps": _simple_steps(["render"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    document = Document(
        entity_id=me["entity_id"],
        name="manor-workspace-final.mp4",
        fs_path="workflow-results/manor-workspace-final.mp4",
        file_type="mp4",
        mime_type="video/mp4",
        source="agent",
        vector_status="ready",
        created_by="wf_artifact_names",
    )
    db_session.add(document)
    await db_session.flush()
    run_row = await db_session.get(WorkflowRun, run["id"])
    run_row.execution_trace = [{
        "sequence": 1,
        "node_id": "render",
        "status": "completed",
        "artifact_refs": [{
            "document_id": document.id,
            "mime_type": "video/mp4",
            "status": "ready",
        }],
    }]
    run_row.step_results = {
        "render": {
            "status": "completed",
            "artifact_refs": {"document_id": document.id},
        },
    }
    await db_session.commit()

    detail = await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )
    assert detail.status_code == 200, detail.text
    artifact = detail.json()["execution_trace"][0]["artifact_refs"][0]
    assert artifact["document_id"] == document.id
    assert artifact["name"] == "manor-workspace-final.mp4"
    result_artifact = detail.json()["step_results"]["render"]["artifact_refs"]
    assert result_artifact["name"] == "manor-workspace-final.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize("enqueue_succeeds", [True, False], ids=["queued", "broker_failure"])
async def test_manual_pause_resumes_without_consuming_current_node_as_approval(
    client: AsyncClient,
    db_session,
    monkeypatch,
    enqueue_succeeds: bool,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfmanualpause")
    wf = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Manual pause test", "steps": _simple_steps(["p1", "p2"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{wf['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    paused_response = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/pause",
        headers=headers,
    )
    assert paused_response.status_code == 200
    paused = paused_response.json()
    assert paused["status"] == "paused"
    assert paused["trigger_data"]["_workflow_run_control"]["state"] == "manual_paused"

    compact = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )).json()
    assert compact["intervention"]["kind"] == "workflow_resume"
    assert compact["intervention"]["options"] == ["resume", "cancel"]

    enqueued: list[tuple[str, str | None]] = []

    def record_enqueue(run_id, _delay_seconds, *, continuation_token=None):
        enqueued.append((run_id, continuation_token))
        return enqueue_succeeds

    async def reject_inline_run(*_args, **_kwargs):
        raise AssertionError("HTTP resume must not execute the workflow inline")

    monkeypatch.setattr(WorkflowRunner, "enqueue_resume", staticmethod(record_enqueue))
    monkeypatch.setattr(WorkflowRunner, "run", reject_inline_run)

    resumed_response = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/resume",
        headers=headers,
        json={"execute": True},
    )
    assert resumed_response.status_code == 200, resumed_response.text
    resumed = resumed_response.json()
    assert resumed["status"] == "running"
    assert "p1" not in resumed["step_results"]
    assert "p2" not in resumed["step_results"]
    assert resumed["trigger_data"]["_workflow_run_control"]["state"] == "resumed"
    assert len(enqueued) == 1
    assert enqueued[0][0] == run["id"]
    assert enqueued[0][1]

    persisted = await db_session.get(WorkflowRun, run["id"])
    assert persisted is not None
    assert persisted.continuation_token == enqueued[0][1]
    # A delivered resume must not be republished by the 15 s sweep while it
    # waits for a heavy worker; a failed publish stays due for the sweep.
    sweep_delay = (
        persisted.continuation_next_attempt_at - datetime.now(timezone.utc)
    ).total_seconds()
    if enqueue_succeeds:
        assert sweep_delay > 30
    else:
        assert sweep_delay <= 0

    duplicate = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/resume",
        headers=headers,
        json={},
    )
    assert duplicate.status_code == 400
    assert len(enqueued) == 1

    # This test intentionally leaves a recoverable intent. Remove its fixture
    # state so the durable-sweep test only observes continuations it created.
    persisted.continuation_token = None
    persisted.continuation_due_at = None
    persisted.continuation_next_attempt_at = None
    await db_session.commit()


@pytest.mark.asyncio
async def test_cancel_during_in_flight_node_prevents_following_node(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfcancelinflight")
    wf = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancel in flight", "steps": _simple_steps(["slow", "never"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{wf['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_execute = WorkflowRunner._execute_step_safe

    async def controlled_execute(self, step, current_run, db):
        if step["id"] == "slow":
            entered.set()
            await release.wait()
        return await original_execute(self, step, current_run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", controlled_execute)
    runner_task = asyncio.create_task(WorkflowRunner().run(run["id"]))
    await asyncio.wait_for(entered.wait(), timeout=3)

    cancelled_response = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/cancel",
        headers=headers,
    )
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["status"] == "cancelled"
    release.set()
    await asyncio.wait_for(runner_task, timeout=3)

    final = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert final["status"] == "cancelled"
    assert final["step_results"]["slow"]["status"] == "completed"
    assert "never" not in final["step_results"]


@pytest.mark.asyncio
async def test_pause_during_in_flight_node_freezes_then_resumes_next_node(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfpauseinflight")
    wf = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Pause in flight", "steps": _simple_steps(["slow", "later"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{wf['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_execute = WorkflowRunner._execute_step_safe

    async def controlled_execute(self, step, current_run, db):
        if step["id"] == "slow":
            entered.set()
            await release.wait()
        return await original_execute(self, step, current_run, db)

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", controlled_execute)
    runner_task = asyncio.create_task(WorkflowRunner().run(run["id"]))
    await asyncio.wait_for(entered.wait(), timeout=3)

    paused_response = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/pause",
        headers=headers,
    )
    assert paused_response.status_code == 200
    assert paused_response.json()["status"] == "paused"
    release.set()
    await asyncio.wait_for(runner_task, timeout=3)

    paused = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert paused["status"] == "paused"
    assert paused["step_results"]["slow"]["status"] == "completed"
    assert "later" not in paused["step_results"]

    resumed_response = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/resume",
        headers=headers,
        json={},
    )
    assert resumed_response.status_code == 200, resumed_response.text
    resumed = resumed_response.json()
    assert resumed["status"] == "running"
    await WorkflowRunner().run(run["id"])
    resumed = (await client.get(
        f"/api/v1/workflows/runs/{run['id']}",
        headers=headers,
    )).json()
    assert resumed["status"] == "completed"
    assert resumed["step_results"]["later"]["status"] == "completed"


@pytest.mark.asyncio
async def test_resume_continues_paused_run_to_completion(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfresume")
    steps = [
        {"id": "start", "type": "trigger", "name": "Start", "next": ["approval"]},
        {
            "id": "approval",
            "type": "wait",
            "name": "Approve",
            "config": {"wait_type": "approval", "message": "Need sign-off"},
            "next": ["finish"],
        },
        {
            "id": "finish",
            "type": "transform",
            "name": "Finish",
            "config": {"set": {"decision_recorded": "{{decision}}"}},
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "next": []},
    ]
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Approval flow", "steps": steps},
    )).json()

    paused = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert paused["status"] == "paused"
    assert paused["current_step_id"] == "approval"

    detail = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}", headers=headers,
    )).json()
    assert detail["resume_action"]["step_id"] == "approval"
    assert detail["resume_action"]["options"] == ["approve", "reject"]
    assert detail["resume_action"]["response_variable"] == "decision"
    assert detail["resume_action"]["prompt"] == "Need sign-off"
    stale_decision = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"expected_step_id": "different_wait", "variables": {"decision": "approved"}},
    )
    assert stale_decision.status_code == 409
    still_paused = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}", headers=headers,
    )).json()
    assert still_paused["status"] == "paused"
    assert "decision" not in still_paused["variables"]

    missing_decision = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"variables": {}},
    )
    assert missing_decision.status_code == 400

    response = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"expected_step_id": "approval", "variables": {"decision": "approved"}},
    )
    assert response.status_code == 200
    completed = response.json()
    assert completed["status"] == "running"
    await WorkflowRunner().run(paused["id"])
    completed = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}",
        headers=headers,
    )).json()
    assert completed["status"] == "completed"
    assert completed["variables"]["decision_recorded"] == "approved"
    assert completed["step_results"]["approval"]["resumed"] is True
    assert completed["step_results"]["approval"]["approved"] is True
    assert completed["step_results"]["approval"]["approved_by"]
    assert completed["step_results"]["finish"]["status"] == "completed"
    assert [
        entry["status"]
        for entry in completed["execution_trace"]
        if entry["node_id"] == "approval"
    ] == ["running", "paused", "completed"]

    duplicate = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={},
    )
    assert duplicate.status_code == 400


@pytest.mark.asyncio
async def test_native_final_post_review_rejects_top_level_variable_injection(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "native_final_post_injection")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Product launch post review",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["review"]},
                {
                    "id": "review", "type": "wait", "next": ["end"],
                    "config": {
                        "wait_type": "approval", "response_variable": "decision",
                        "options": ["approve", "request_revision"],
                        "review": "{{final_post_review}}",
                        "review_editable_fields": ["title", "description", "tags"],
                        "review_revision_field": "video_change_request",
                    },
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    original = {
        "title": "Product demo", "description": "Launch copy", "tags": ["product"],
        "video_change_request": "", "account": "immutable",
    }
    paused = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers,
        json={"variables": {"final_post_review": original}},
    )).json()
    assert paused["status"] == "paused"

    injected = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume", headers=headers,
        json={
            "expected_step_id": "review",
            "variables": {
                "decision": "approve",
                "final_post_review": {**original, "account": "changed"},
                "production": {"published": True},
            },
        },
    )
    assert injected.status_code == 400
    unchanged = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}", headers=headers,
    )).json()
    assert unchanged["status"] == "paused"
    assert unchanged["variables"]["final_post_review"] == original
    assert "production" not in unchanged["variables"]

    edited = {**original, "title": "New product demo", "description": "", "tags": []}
    approved = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume", headers=headers,
        json={
            "expected_step_id": "review",
            "variables": {"decision": {"choice": "approve", "review": edited}},
        },
    )
    assert approved.status_code == 200
    await WorkflowRunner().run(paused["id"])
    completed = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}", headers=headers,
    )).json()
    assert completed["status"] == "completed"
    assert completed["variables"]["final_post_review"] == edited
    assert completed["step_results"]["review"]["approved"] is True

    paused = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers,
        json={"variables": {"final_post_review": original}},
    )).json()
    assert paused["status"] == "paused"
    resume_url = f"/api/v1/workflows/runs/{paused['id']}/resume"
    missing_note = await client.post(
        resume_url, headers=headers,
        json={"expected_step_id": "review", "variables": {"decision": "request_revision"}},
    )
    assert missing_note.status_code == 400

    revision = {**edited, "video_change_request": "Shorten the introduction."}
    revised = await client.post(
        resume_url, headers=headers,
        json={"expected_step_id": "review", "variables": {
            "decision": {"choice": "request_revision", "review": revision},
        }},
    )
    assert revised.status_code == 200
    await WorkflowRunner().run(paused["id"])
    completed = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}", headers=headers,
    )).json()
    assert completed["status"] == "completed"
    assert completed["variables"]["final_post_review"] == revision
    assert completed["variables"]["decision"]["note"] == revision["video_change_request"]
    assert completed["step_results"]["review"]["decision"] == "request_revision"


@pytest.mark.asyncio
async def test_resume_continues_from_an_internal_stage_approval(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfstagresume")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Stage approval flow",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["review"]},
                {
                    "id": "review",
                    "type": "stage",
                    "config": {
                        "entry_operation_id": "prepare",
                        "operations": [
                            {
                                "id": "prepare",
                                "type": "transform",
                                "config": {"set": {"prepared": True}},
                                "next": ["approval"],
                            },
                            {
                                "id": "approval",
                                "type": "wait",
                                "config": {
                                    "wait_type": "approval",
                                    "response_variable": "decision",
                                    "options": ["approved", "revise"],
                                },
                                "next": ["finish"],
                            },
                            {
                                "id": "finish",
                                "type": "transform",
                                "config": {
                                    "set": {"decision_recorded": "{{decision}}"}
                                },
                                "next": ["done"],
                            },
                        ],
                        "routes": {"done": "end"},
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    paused = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert paused["status"] == "paused"
    assert paused["current_step_id"] == "review"
    assert paused["step_results"]["review"]["paused_operation_id"] == "approval"
    stage_state = paused["variables"]["__stage_execution"]["review"]
    assert stage_state["status"] == "paused"
    assert stage_state["operation_results"]["prepare"]["status"] == "completed"

    response = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )

    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["status"] == "running"
    await WorkflowRunner().run(paused["id"])
    completed = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}",
        headers=headers,
    )).json()
    assert completed["status"] == "completed"
    assert completed["variables"]["decision_recorded"] == "approved"
    state = completed["variables"]["__stage_execution"]["review"]
    assert state["status"] == "completed"
    assert state["operation_results"]["approval"]["resumed"] is True
    assert state["operation_results"]["approval"]["approved"] is True
    assert state["operation_results"]["finish"]["status"] == "completed"
    assert [
        entry["status"]
        for entry in completed["execution_trace"]
        if entry["node_id"] == "review.approval"
    ] == ["running", "paused", "completed"]


@pytest.mark.asyncio
async def test_resume_uses_private_snapshot_when_live_definition_changes(
    client: AsyncClient,
):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfresumesnapshotdrift")
    steps = [
        {"id": "start", "type": "trigger", "next": ["approval"]},
        {
            "id": "approval",
            "type": "wait",
            "config": {"wait_type": "approval"},
            "next": ["end"],
        },
        {"id": "end", "type": "end", "next": []},
    ]
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Paused stable definition", "steps": steps},
    )).json()
    paused = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert paused["status"] == "paused"

    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"name": "Paused changed definition"},
    )
    assert updated.status_code == 200, updated.text

    response = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    await WorkflowRunner().run(paused["id"])
    detail = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}",
        headers=headers,
    )).json()
    assert detail["status"] == "completed"
    assert detail["definition_snapshot"] == paused["definition_snapshot"]
    assert detail["step_results"]["approval"]["resumed"] is True
    assert detail["step_results"]["approval"]["approved"] is True
    assert [
        entry["status"]
        for entry in detail["execution_trace"]
        if entry["node_id"] == "approval"
    ] == ["running", "paused", "completed"]


@pytest.mark.asyncio
async def test_resume_accepts_structured_approval_choice_for_branching(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfstructuredresume")
    steps = [
        {"id": "start", "type": "trigger", "name": "Start", "next": ["approval"]},
        {
            "id": "approval",
            "type": "wait",
            "name": "Approve",
            "config": {
                "wait_type": "approval",
                "message": "Need sign-off",
                "response_variable": "plan_decision",
                "options": ["approve", "cancel"],
                "approval_values": ["approve"],
            },
            "next": ["approved"],
        },
        {
            "id": "approved",
            "type": "condition",
            "name": "Approved",
            "config": {"expression": "plan_decision.choice == 'approve'"},
            "true_next": ["finish"],
            "false_next": ["cancelled"],
            "next": [],
        },
        {
            "id": "finish",
            "type": "transform",
            "name": "Finish",
            "config": {"set": {"branch": "approved"}},
            "next": ["end"],
        },
        {"id": "cancelled", "type": "end", "name": "Cancelled", "next": []},
        {"id": "end", "type": "end", "name": "Done", "next": []},
    ]
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Structured approval flow", "steps": steps},
    )).json()

    paused = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert paused["status"] == "paused"

    response = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"variables": {"plan_decision": {"choice": "approve"}}},
    )

    assert response.status_code == 200
    completed = response.json()
    assert completed["status"] == "running"
    await WorkflowRunner().run(paused["id"])
    completed = (await client.get(
        f"/api/v1/workflows/runs/{paused['id']}",
        headers=headers,
    )).json()
    assert completed["status"] == "completed"
    assert completed["variables"]["plan_decision"] == {"choice": "approve"}
    assert completed["variables"]["branch"] == "approved"
    assert completed["step_results"]["approval"]["decision"] == "approve"
    assert completed["step_results"]["approval"]["approved"] is True


@pytest.mark.asyncio
async def test_resumed_subworkflow_automatically_continues_parent(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfsubresume")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Child approval",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["approval"]},
                {
                    "id": "approval",
                    "type": "wait",
                    "config": {"wait_type": "approval"},
                    "next": ["child_finish"],
                },
                {
                    "id": "child_finish",
                    "type": "transform",
                    "config": {"set": {"child_value": "{{decision}}"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Parent flow",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["child"]},
                {
                    "id": "child",
                    "type": "subworkflow",
                    "config": {"workflow_id": child["name"]},
                    "next": ["parent_finish"],
                },
                {
                    "id": "parent_finish",
                    "type": "transform",
                    "config": {"set": {"parent_value": "{{child.child_value}}"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    parent_run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert parent_run["status"] == "paused"
    child_run_id = parent_run["step_results"]["child"]["subrun_id"]
    child_run = (await client.get(
        f"/api/v1/workflows/runs/{child_run_id}",
        headers=headers,
    )).json()
    assert child_run["trigger_data"]["_workflow_name"] == "Child approval"
    # Steps the Flow left unnamed are shown by their ids read as words.
    assert child_run["trigger_data"]["_workflow_node_names"] == {
        "start": "Start",
        "approval": "Approval",
        "child_finish": "Child finish",
        "end": "End",
    }
    assert child_run["workflow_name"] == "Child approval"
    assert child_run["current_step_name"] == "Approval"

    blocked_parent_resume = await client.post(
        f"/api/v1/workflows/runs/{parent_run['id']}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert blocked_parent_resume.status_code == 409
    still_paused_parent = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert still_paused_parent["status"] == "paused"
    assert still_paused_parent["step_results"]["child"]["status"] == "paused"

    child_resumed = await client.post(
        f"/api/v1/workflows/runs/{child_run_id}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert child_resumed.status_code == 200
    assert child_resumed.json()["status"] == "running"
    await WorkflowRunner().run(child_run_id)

    completed_parent = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert completed_parent["status"] == "completed"
    assert completed_parent["variables"]["parent_value"] == "approved"
    assert completed_parent["step_results"]["child"]["resumed"] is True
    assert [
        entry["status"]
        for entry in completed_parent["execution_trace"]
        if entry["node_id"] == "child"
    ] == ["running", "paused", "completed"]


@pytest.mark.asyncio
async def test_failed_resumed_subworkflow_appends_parent_terminal_trace(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfsubresumefailedtrace")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Child approval then failure",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["approval"]},
                {
                    "id": "approval",
                    "type": "wait",
                    "config": {"wait_type": "approval"},
                    "next": ["fail"],
                },
                {
                    "id": "fail",
                    "type": "stop",
                    "config": {"message": "child failed after approval"},
                    "next": [],
                },
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Parent records child failure",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["child"]},
                {
                    "id": "child",
                    "type": "subworkflow",
                    "config": {"workflow_id": child["id"]},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent_run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={},
    )).json()
    child_run_id = parent_run["step_results"]["child"]["subrun_id"]

    resumed = await client.post(
        f"/api/v1/workflows/runs/{child_run_id}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "running"
    await WorkflowRunner().run(child_run_id)

    failed_parent = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert failed_parent["status"] == "failed"
    assert [
        entry["status"]
        for entry in failed_parent["execution_trace"]
        if entry["node_id"] == "child"
    ] == ["running", "paused", "failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_recovery", [False, True])
@pytest.mark.parametrize("deepest_child_is_newer", [False, True])
@pytest.mark.parametrize("retry_blocked", [False, True])
async def test_compact_root_run_retries_deepest_failed_subworkflow(
    client: AsyncClient,
    db_session,
    cancel_during_recovery: bool,
    deepest_child_is_newer: bool,
    retry_blocked: bool,
):
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcompactdeepretry")
    executor = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "YouTube executor",
            "variables": {"metadata_ready": False},
            "steps": [
                {"id": "start", "type": "trigger", "next": ["set_youtube_metadata"]},
                {
                    "id": "set_youtube_metadata",
                    "type": "condition",
                    "config": {"expression": "metadata_ready == true"},
                    "true_next": ["end"],
                    "false_next": ["youtube_metadata_blocked"],
                },
                {
                    "id": "youtube_metadata_blocked",
                    "type": "stop",
                    "config": {
                        "message": "metadata update failed",
                        "retry_from_step_id": "set_youtube_metadata",
                    },
                    "next": [],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    publisher = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Publish video",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["publish_video"]},
                {
                    "id": "publish_video",
                    "type": "subworkflow",
                    "config": {"workflow_id": executor["id"]},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    root = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Create and publish video",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["publish_video"]},
                {
                    "id": "publish_video",
                    "type": "subworkflow",
                    "config": {"workflow_id": publisher["id"]},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    root_run = (await client.post(
        f"/api/v1/workflows/{root['id']}/run",
        headers=headers,
        json={},
    )).json()
    publisher_run_id = root_run["step_results"]["publish_video"]["subrun_id"]
    publisher_run = (await client.get(
        f"/api/v1/workflows/runs/{publisher_run_id}",
        headers=headers,
    )).json()
    executor_run_id = publisher_run["step_results"]["publish_video"]["subrun_id"]

    # Workspace Chat roots do not have a retry message for nested executor
    # failures; compact state must still preserve the direct nested retry.
    root_row = await db_session.get(WorkflowRun, root_run["id"])
    executor_row = await db_session.get(WorkflowRun, executor_run_id)
    root_row.trigger_source = "workspace_chat"
    executor_row.retry_from_step_id = "set_youtube_metadata"
    executor_row.trigger_data = {
        **executor_row.trigger_data,
        "_workflow_chat_origin": {
            "enabled": True,
            "conversation_id": "missing-child-activity-conversation",
            "activity_message_id": "missing-child-activity-message",
        },
    }
    if retry_blocked:
        executor_row.step_results = {
            **executor_row.step_results,
            "youtube_metadata_blocked": {
                **executor_row.step_results["youtube_metadata_blocked"],
                "retryable": False,
                "do_not_replay": True,
                "retry_blocked": True,
            },
        }
    base_updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    root_row.updated_at = base_updated_at + timedelta(
        minutes=1 if deepest_child_is_newer else 2,
    )
    executor_row.updated_at = base_updated_at + timedelta(
        minutes=2 if deepest_child_is_newer else 1,
    )
    await db_session.commit()

    compact = (await client.get(
        f"/api/v1/workflows/runs/{root_run['id']}?detail=false",
        headers=headers,
    )).json()

    assert compact["id"] == root_run["id"]
    assert compact["status"] == "failed"
    if retry_blocked:
        assert compact["retry_from_step_id"] is None
        assert compact["intervention"] is None
        assert compact["error"] == "metadata update failed"
        assert datetime.fromisoformat(compact["updated_at"]) == max(
            root_row.updated_at,
            executor_row.updated_at,
        )
        return
    assert compact["intervention"]["kind"] == "workflow_retry"
    assert compact["intervention"]["workflow_run_id"] == root_run["id"]
    assert compact["intervention"]["step_id"] == "youtube_metadata_blocked"
    assert compact["intervention"]["retry_from_step_id"] == "set_youtube_metadata"
    assert compact["retry_from_step_id"] == "set_youtube_metadata"
    assert datetime.fromisoformat(compact["updated_at"]) == max(
        root_row.updated_at,
        executor_row.updated_at,
    )

    first_retry = await client.post(
        f"/api/v1/workflows/runs/{root_run['id']}/retry",
        headers=headers,
        json={
            "from_step_id": compact["intervention"]["retry_from_step_id"],
            "variables": {"metadata_ready": False},
            "execute": False,
        },
    )
    assert first_retry.status_code == 201, first_retry.text
    first_executor_retry = first_retry.json()
    assert first_executor_retry["workflow_id"] == executor["id"]
    assert first_executor_retry["status"] == "running"

    first_executor_retry_row = await db_session.get(
        WorkflowRun,
        first_executor_retry["id"],
    )
    assert first_executor_retry_row.trigger_data["parent_run_id"] == publisher_run_id
    assert first_executor_retry_row.trigger_data["parent_step_id"] == "publish_video"

    visible_history = (await client.get(
        "/api/v1/workflows/runs",
        headers=headers,
    )).json()
    assert [item["id"] for item in visible_history] == [root_run["id"]]

    active_root = (await client.get(
        f"/api/v1/workflows/runs/{root_run['id']}?detail=false",
        headers=headers,
    )).json()
    assert active_root["id"] == root_run["id"]
    assert active_root["status"] == "running"
    assert active_root["intervention"] is None

    if cancel_during_recovery:
        cancelled = await client.post(
            f"/api/v1/workflows/runs/{root_run['id']}/cancel",
            headers=headers,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["id"] == root_run["id"]
        assert cancelled.json()["status"] == "cancelled"

        await db_session.refresh(root_row)
        await db_session.refresh(first_executor_retry_row)
        assert root_row.status == "cancelled"
        assert first_executor_retry_row.status == "cancelled"

        cancelled_root = (await client.get(
            f"/api/v1/workflows/runs/{root_run['id']}?detail=false",
            headers=headers,
        )).json()
        assert cancelled_root["status"] == "cancelled"
        assert cancelled_root["intervention"] is None
        return

    from packages.core.ai.workflow_runner import WorkflowRunner

    await WorkflowRunner().run(first_executor_retry["id"])
    await db_session.refresh(first_executor_retry_row)
    failed_root = (await client.get(
        f"/api/v1/workflows/runs/{root_run['id']}?detail=false",
        headers=headers,
    )).json()
    assert failed_root["status"] == "failed"
    assert failed_root["intervention"]["workflow_run_id"] == root_run["id"]
    assert failed_root["intervention"]["retry_from_step_id"] == "set_youtube_metadata"

    second_retry = await client.post(
        f"/api/v1/workflows/runs/{root_run['id']}/retry",
        headers=headers,
        json={
            "from_step_id": failed_root["intervention"]["retry_from_step_id"],
            "variables": {"metadata_ready": True},
            "execute": False,
        },
    )
    assert second_retry.status_code == 201, second_retry.text
    completed_executor = await _execute_created_retry(
        client,
        headers,
        second_retry,
    )
    assert completed_executor["workflow_id"] == executor["id"]
    assert completed_executor["status"] == "completed"

    executor_attempts = (await db_session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == executor["id"])
        .order_by(WorkflowRun.attempt_number)
    )).scalars().all()
    assert [attempt.status for attempt in executor_attempts] == [
        "failed",
        "failed",
        "completed",
    ]

    parent_attempts = (await db_session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id.in_([publisher["id"], root["id"]]))
        .order_by(WorkflowRun.workflow_id, WorkflowRun.attempt_number)
    )).scalars().all()
    attempts_by_workflow = {
        workflow_id: [
            attempt
            for attempt in parent_attempts
            if attempt.workflow_id == workflow_id
        ]
        for workflow_id in (publisher["id"], root["id"])
    }
    for workflow_id in (publisher["id"], root["id"]):
        attempts = attempts_by_workflow[workflow_id]
        assert [attempt.status for attempt in attempts] == ["failed", "completed"]
        assert attempts[1].retry_of_run_id == attempts[0].id
        assert attempts[1].retry_from_step_id == "publish_video"

    completed_root = (await client.get(
        f"/api/v1/workflows/runs/{root_run['id']}?detail=false",
        headers=headers,
    )).json()
    assert completed_root["id"] == root_run["id"]
    assert completed_root["status"] == "completed"
    assert completed_root["intervention"] is None


@pytest.mark.asyncio
async def test_top_level_cancel_serializes_with_nested_retry_creation(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from sqlalchemy import func, select

    from packages.core.models.workflow import WorkflowRun
    from packages.core.services import workflow_run_control, workflow_service

    headers = await _auth(client, "wfcancelnestedretryrace")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Nested retry race child",
            "variables": {"recover": False},
            "steps": [
                {"id": "start", "type": "trigger", "next": ["recoverable"]},
                {
                    "id": "recoverable",
                    "type": "condition",
                    "config": {"expression": "recover == true"},
                    "true_next": ["end"],
                    "false_next": ["blocked"],
                },
                {
                    "id": "blocked",
                    "type": "stop",
                    "config": {
                        "message": "nested retry required",
                        "retry_from_step_id": "recoverable",
                    },
                    "next": [],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    root = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Nested retry race root",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["child"]},
                {
                    "id": "child",
                    "type": "subworkflow",
                    "config": {"workflow_id": child["id"]},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    root_run = (await client.post(
        f"/api/v1/workflows/{root['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert root_run["status"] == "failed"

    cancel_holds_owner_lock = asyncio.Event()
    release_cancel = asyncio.Event()
    retry_locked_resource = asyncio.Event()
    original_cancel_descendants = (
        workflow_run_control.cancel_active_subworkflow_descendants
    )
    original_lock_resource = workflow_service.lock_reusable_resource_reference

    async def hold_cancel_before_commit(*args, **kwargs):
        cancel_holds_owner_lock.set()
        await release_cancel.wait()
        return await original_cancel_descendants(*args, **kwargs)

    async def observe_retry_resource_lock(*args, **kwargs):
        await original_lock_resource(*args, **kwargs)
        retry_locked_resource.set()

    monkeypatch.setattr(
        workflow_run_control,
        "cancel_active_subworkflow_descendants",
        hold_cancel_before_commit,
    )
    monkeypatch.setattr(
        workflow_service,
        "lock_reusable_resource_reference",
        observe_retry_resource_lock,
    )

    cancel_task = asyncio.create_task(client.post(
        f"/api/v1/workflows/runs/{root_run['id']}/cancel",
        headers=headers,
    ))
    await asyncio.wait_for(cancel_holds_owner_lock.wait(), timeout=2)
    retry_task = asyncio.create_task(client.post(
        f"/api/v1/workflows/runs/{root_run['id']}/retry",
        headers=headers,
        json={
            "from_step_id": "recoverable",
            "variables": {"recover": True},
            "execute": False,
        },
    ))
    await asyncio.wait_for(retry_locked_resource.wait(), timeout=2)
    await asyncio.sleep(0.05)
    retry_completed_before_cancel = retry_task.done()
    release_cancel.set()

    cancelled, retried = await asyncio.gather(cancel_task, retry_task)
    assert retry_completed_before_cancel is False
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert retried.status_code == 409, retried.text

    child_attempt_count = await db_session.scalar(
        select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.workflow_id == child["id"],
        )
    )
    assert child_attempt_count == 1


@pytest.mark.asyncio
async def test_deep_retry_target_rejects_out_of_scope_or_broken_subworkflow_links(
    db_session,
):
    from apps.api.routers.workflows import _deepest_retryable_subworkflow_run
    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun

    def failed_run(
        *,
        entity_id: str,
        workspace_id: str | None,
        current_step_id: str,
        parent_run_id: str | None = None,
        parent_step_id: str | None = None,
    ) -> WorkflowRun:
        run_id = generate_ulid()
        trigger_data = {}
        if parent_run_id:
            trigger_data = {
                "parent_run_id": parent_run_id,
                "parent_step_id": parent_step_id,
            }
        return WorkflowRun(
            id=run_id,
            workflow_id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_id,
            lineage_root_run_id=run_id,
            lineage_is_legacy=False,
            status="failed",
            current_step_id=current_step_id,
            variables={},
            step_results={},
            trigger_data=trigger_data,
            definition_snapshot={},
            execution_snapshot={},
            execution_trace=[],
        )

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    root = failed_run(
        entity_id=entity_id,
        workspace_id=workspace_id,
        current_step_id="publish",
    )
    child = failed_run(
        entity_id=entity_id,
        workspace_id=workspace_id,
        current_step_id="execute",
        parent_run_id=root.id,
        parent_step_id="publish",
    )
    grandchild = failed_run(
        entity_id=entity_id,
        workspace_id=workspace_id,
        current_step_id="metadata",
        parent_run_id=child.id,
        parent_step_id="execute",
    )
    root.step_results = {
        "publish": {"status": "failed", "subrun_id": child.id},
    }
    child.step_results = {
        "execute": {"status": "failed", "subrun_id": grandchild.id},
    }
    grandchild.step_results = {"metadata": {"status": "failed"}}
    db_session.add_all([root, child, grandchild])
    await db_session.commit()

    target = await _deepest_retryable_subworkflow_run(db_session, root)
    assert target.id == grandchild.id

    child.entity_id = generate_ulid()
    await db_session.commit()
    assert (await _deepest_retryable_subworkflow_run(db_session, root)).id == root.id
    child.entity_id = entity_id

    child.workspace_id = generate_ulid()
    await db_session.commit()
    assert (await _deepest_retryable_subworkflow_run(db_session, root)).id == root.id
    child.workspace_id = workspace_id

    child.trigger_data = {"parent_run_id": root.id, "parent_step_id": "wrong-step"}
    await db_session.commit()
    assert (await _deepest_retryable_subworkflow_run(db_session, root)).id == root.id
    child.trigger_data = {"parent_run_id": root.id, "parent_step_id": "publish"}

    root.step_results = {
        "publish": {"status": "failed", "subrun_id": generate_ulid()},
    }
    await db_session.commit()
    assert (await _deepest_retryable_subworkflow_run(db_session, root)).id == root.id
    root.step_results = {
        "publish": {"status": "failed", "subrun_id": child.id},
    }

    grandchild.step_results = {
        "metadata": {"status": "failed", "subrun_id": root.id},
    }
    root.trigger_data = {
        "parent_run_id": grandchild.id,
        "parent_step_id": "metadata",
    }
    await db_session.commit()
    assert (await _deepest_retryable_subworkflow_run(db_session, root)).id == root.id


@pytest.mark.asyncio
async def test_successful_child_retry_reconciles_failed_parent_with_linked_attempt(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfsubretryreconcile")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Retryable child",
            "variables": {"recover": False},
            "steps": [
                {"id": "start", "type": "trigger", "next": ["recoverable"]},
                {
                    "id": "recoverable",
                    "type": "condition",
                    "config": {"expression": "recover == true"},
                    "true_next": ["produce"],
                    "false_next": ["fail"],
                },
                {
                    "id": "produce",
                    "type": "transform",
                    "config": {"set": {"child_value": "recovered"}},
                    "next": ["end"],
                },
                {
                    "id": "fail",
                    "type": "stop",
                    "config": {"message": "child needs retry"},
                    "next": [],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Parent reconciles child retry",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["child"]},
                {
                    "id": "child",
                    "type": "subworkflow",
                    "config": {"workflow_id": child["id"]},
                    "next": ["parent_finish"],
                },
                {
                    "id": "parent_finish",
                    "type": "transform",
                    "config": {
                        "set": {"parent_value": "{{child.child_value}}"},
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    failed_parent = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert failed_parent["status"] == "failed"
    failed_child_id = failed_parent["step_results"]["child"]["subrun_id"]

    retried_child_response = await client.post(
        f"/api/v1/workflows/runs/{failed_child_id}/retry",
        headers=headers,
        json={
            "from_step_id": "recoverable",
            "variables": {"recover": True},
            "execute": False,
        },
    )
    retried_child = await _execute_created_retry(
        client,
        headers,
        retried_child_response,
    )
    assert retried_child["status"] == "completed"

    parent_attempts = (await db_session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.lineage_root_run_id == failed_parent["id"])
        .order_by(WorkflowRun.attempt_number)
    )).scalars().all()
    assert len(parent_attempts) == 2
    recovered_parent = parent_attempts[-1]
    assert recovered_parent.retry_of_run_id == failed_parent["id"]
    assert recovered_parent.retry_from_step_id == "child"
    assert recovered_parent.status == "completed"
    assert recovered_parent.step_results["child"]["subrun_id"] == retried_child["id"]
    assert recovered_parent.variables["parent_value"] == "recovered"

    original_parent = await db_session.get(WorkflowRun, failed_parent["id"])
    assert original_parent.status == "failed"

    # Terminal-effect redelivery is at-least-once. Replaying the successful
    # child receipt must not reopen either terminal row or create attempt 3.
    retried_child_row = await db_session.get(WorkflowRun, retried_child["id"])
    await WorkflowRunner()._propagate_subworkflow_result(
        retried_child_row,
        db_session,
    )
    parent_attempts = (await db_session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.lineage_root_run_id == failed_parent["id"])
        .order_by(WorkflowRun.attempt_number)
    )).scalars().all()
    assert len(parent_attempts) == 2
    assert [attempt.status for attempt in parent_attempts] == ["failed", "completed"]

    # A process can commit the reconciled attempt before continuing it. If a
    # user then cancels that attempt, redelivery must respect the cancellation
    # rather than create and execute attempt 3.
    recovered_parent.status = "cancelled"
    recovered_parent.completed_at = datetime.now(timezone.utc)
    await db_session.commit()
    await WorkflowRunner()._propagate_subworkflow_result(
        retried_child_row,
        db_session,
    )
    parent_attempts = (await db_session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.lineage_root_run_id == failed_parent["id"])
        .order_by(WorkflowRun.attempt_number)
    )).scalars().all()
    assert len(parent_attempts) == 2
    assert [attempt.status for attempt in parent_attempts] == ["failed", "cancelled"]


@pytest.mark.asyncio
async def test_child_retry_reconciliation_rejects_mismatched_parent_target(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_trace import (
        workflow_definition_fingerprint,
    )

    headers = await _auth(client, "wfsubretrymismatch")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Bound child",
            "variables": {"recover": False},
            "steps": [
                {"id": "start", "type": "trigger", "next": ["recoverable"]},
                {
                    "id": "recoverable",
                    "type": "condition",
                    "config": {"expression": "recover == true"},
                    "true_next": ["end"],
                    "false_next": ["fail"],
                },
                {
                    "id": "fail",
                    "type": "stop",
                    "config": {"message": "child needs retry"},
                    "next": [],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Parent with corrupted binding",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["child"]},
                {
                    "id": "child",
                    "type": "subworkflow",
                    "config": {"workflow_id": child["id"]},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    failed_parent = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={},
    )).json()
    failed_child_id = failed_parent["step_results"]["child"]["subrun_id"]
    parent_row = await db_session.get(WorkflowRun, failed_parent["id"])
    corrupted_snapshot = deepcopy(parent_row.execution_snapshot)
    child_step = next(
        step for step in corrupted_snapshot["steps"] if step["id"] == "child"
    )
    child_step["config"]["workflow_id"] = parent["id"]
    frozen = SimpleNamespace(
        id=corrupted_snapshot["workflow_id"],
        version=corrupted_snapshot["version"],
        steps=corrupted_snapshot["steps"],
    )
    corrupted_snapshot["fingerprint"] = workflow_definition_fingerprint(frozen)
    parent_row.execution_snapshot = corrupted_snapshot
    await db_session.commit()

    retried_child = await client.post(
        f"/api/v1/workflows/runs/{failed_child_id}/retry",
        headers=headers,
        json={
            "from_step_id": "recoverable",
            "variables": {"recover": True},
            "execute": False,
        },
    )
    retried_child_result = await _execute_created_retry(
        client,
        headers,
        retried_child,
    )
    assert retried_child_result["status"] == "completed"

    parent_attempts = (await db_session.execute(
        select(WorkflowRun).where(
            WorkflowRun.lineage_root_run_id == failed_parent["id"]
        )
    )).scalars().all()
    assert len(parent_attempts) == 1
    assert parent_attempts[0].status == "failed"


@pytest.mark.asyncio
async def test_retry_queues_execution_and_returns_before_workflow_finishes(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretrybackground")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Background retry", "steps": _simple_steps(["work"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    await db_session.commit()

    inline_execution_started = asyncio.Event()

    async def never_finishes(*_args, **_kwargs):
        inline_execution_started.set()
        await asyncio.Event().wait()

    queued_run_ids: list[str] = []

    def enqueue(run_id: str, delay_seconds: float = 0) -> bool:
        assert delay_seconds == 0
        queued_run_ids.append(run_id)
        return True

    monkeypatch.setattr(WorkflowRunner, "run", never_finishes)
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(enqueue))

    # The inline runner never finishes, so a retry that awaited it would hang
    # until this guard. The bound is not a latency budget for the request's
    # own authorization and database work on a loaded runner.
    response = await asyncio.wait_for(
        client.post(
            f"/api/v1/workflows/runs/{failed['id']}/retry",
            headers=headers,
            json={"from_step_id": "work", "execute": True},
        ),
        timeout=LIVENESS_TIMEOUT_S,
    )

    assert response.status_code == 201, response.text
    retried = response.json()
    assert retried["status"] == "running"
    assert queued_run_ids == [retried["id"]]
    assert inline_execution_started.is_set() is False


@pytest.mark.asyncio
async def test_a_finished_run_is_revised_into_its_next_version_only_where_its_workflow_says(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfrevise")
    steps = _simple_steps(["plan", "make"])
    steps[0]["config"] = {
        "run_inputs": [{"key": "notes", "type": "string", "hidden": True, "default": ""},
                       {"key": "picked", "type": "json", "hidden": True, "default": {}},
                       {"key": "previous_artifact", "type": "json", "hidden": True, "default": {}}],
        "production": {"stages": [{"label": "Make", "steps": ["make"]}],
                       # "check" was added after the run: it starts again from the first step it reached.
                       "revise": {"from_step": ["check", "make"], "notes_input": "notes", "versions_input": "picked",
                                  "context_inputs": {"previous_artifact": "make"}}},
    }
    workflow = (await client.post("/api/v1/workflows", headers=headers,
                                  json={"name": "Revisable", "steps": steps})).json()
    plain = (await client.post("/api/v1/workflows", headers=headers,
                               json={"name": "Plain", "steps": _simple_steps(["make"])})).json()
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(lambda run_id, delay_seconds=0: True))

    async def finished(workflow_id: str) -> str:
        run = (await client.post(f"/api/v1/workflows/{workflow_id}/run", headers=headers,
                                 json={"execute": False})).json()
        row = await db_session.get(WorkflowRun, run["id"])
        row.status, row.current_step_id = "completed", "end"
        row.variables = {**(row.variables or {}), "make": {"document_id": "saved-artifact"}}
        row.step_results = {"plan": {"status": "completed", "output": {"plan": "done"}},
                            "make": {"status": "completed", "output": {"make": "done"}}}
        await db_session.commit()
        return run["id"]

    first = await finished(workflow["id"])
    # Nothing to change is not a change.
    assert (await client.post(f"/api/v1/workflows/runs/{first}/revise", headers=headers, json={})).status_code == 400
    revised = await client.post(f"/api/v1/workflows/runs/{first}/revise", headers=headers,
                                json={"notes": "Make it shorter", "unit_versions": {"s1": "job-2"}})
    assert revised.status_code == 201, revised.text
    second = revised.json()
    row = await db_session.get(WorkflowRun, second["id"])
    assert row.effective_retry_of_run_id == first
    assert row.retry_from_step_id == "make"
    # Marked as a new version, so per-attempt outputs (a take folder) are not reused.
    assert row.trigger_data["_workflow_revision"] == {"of_run_id": first}
    assert row.variables["notes"] == "Make it shorter" and row.variables["picked"] == {"s1": "job-2"}
    assert row.variables["previous_artifact"] == {"document_id": "saved-artifact"}
    assert "make" not in row.variables  # Ordinary invalidated outputs are still cleared.
    # The earlier version is kept, and only the newest version can be changed again.
    assert (await db_session.get(WorkflowRun, first)).status == "completed"
    stale = await client.post(f"/api/v1/workflows/runs/{first}/revise", headers=headers, json={"notes": "Again"})
    assert stale.status_code == 409
    # A cancelled version made nothing, so the earlier finished one can be revised again.
    row.status = "cancelled"
    await db_session.commit()
    again = await client.post(f"/api/v1/workflows/runs/{first}/revise", headers=headers, json={"notes": "Again"})
    assert again.status_code == 201, again.text
    assert (await db_session.get(WorkflowRun, again.json()["id"])).trigger_data["_workflow_revision"] == {"of_run_id": first}
    # A Workflow that does not say how its result is revised keeps finished runs as they are.
    other = await finished(plain["id"])
    refused = await client.post(f"/api/v1/workflows/runs/{other}/revise", headers=headers, json={"notes": "x"})
    assert refused.status_code == 400


@pytest.mark.asyncio
async def test_a_unit_the_run_rendered_itself_counts_as_made(client: AsyncClient, db_session, monkeypatch):
    """Production: a still end card made with still_to_video has no media job, so
    the run showed that shot as planned (10/11) and a retry could make it again.
    Its Knowledge receipt names the run and unit, and both the run view and the
    retry's progress count it, without recording it as a media job."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.document import Document
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import _retry_production_progress

    headers = await _auth(client, "wflocalclip")
    steps = _simple_steps(["make"])
    steps[0]["config"] = {"production": {
        "unit": {"name": "Shots"}, "units_from": "plan.shots", "unit_fields": {"id": "id", "title": "title"},
        "stages": [{"label": "Shots", "steps": ["make"], "counts_units": True}],
    }}
    workflow = (await client.post("/api/v1/workflows", headers=headers,
                                  json={"name": "Shots", "steps": steps})).json()
    monkeypatch.setattr(WorkflowRunner, "enqueue", staticmethod(lambda run_id, delay_seconds=0: True))
    run = (await client.post(f"/api/v1/workflows/{workflow['id']}/run", headers=headers,
                             json={"execute": False})).json()
    row = await db_session.get(WorkflowRun, run["id"])
    row.variables = {**(row.variables or {}), "plan": {"shots": [{"id": "S10", "title": "Chorus"},
                                                                 {"id": "S11", "title": "End card"}]}}

    def clip(name, generation, trashed=False):
        return Document(entity_id=row.entity_id, name=name, mime_type="video/mp4", is_trashed=trashed,
                        metadata_={"generation": {"operation": "still_to_video", **generation}})

    db_session.add_all([
        clip("song-S11-endcard.mp4", {"workflow_run_id": row.id, "unit_id": "S11"}),
        clip("song-S10.mp4", {"workflow_run_id": "another-run", "unit_id": "S10"}),
        clip("song-S10-old.mp4", {"workflow_run_id": row.id, "unit_id": "S10"}, trashed=True),
    ])
    await db_session.commit()

    detail = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert [item["unit_id"] for item in detail["production"]["units_made_locally"]] == ["S11"]
    progress = await _retry_production_progress(db_session, row, steps, "make")
    assert [unit["unit_id"] for unit in progress["units_made"]] == ["S11"]
    assert [unit["unit_id"] for unit in progress["units_to_make"]] == ["S10"]


@pytest.mark.asyncio
async def test_retry_records_queue_failure_without_leaving_a_running_attempt(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretryqueuefailure")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Failed retry dispatch", "steps": _simple_steps(["work"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    await db_session.commit()

    async def must_not_run_inline(*_args, **_kwargs):
        raise AssertionError("Retry execution must not run inside the API request")

    monkeypatch.setattr(WorkflowRunner, "run", must_not_run_inline)
    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue",
        staticmethod(lambda *_args, **_kwargs: False),
    )

    response = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": True},
    )

    assert response.status_code == 201, response.text
    retried = response.json()
    assert retried["status"] == "failed"
    assert retried["completed_at"] is not None
    assert "could not be queued" in retried["error"].lower()

    stored = await db_session.get(WorkflowRun, retried["id"])
    await db_session.refresh(stored)
    assert stored.status == "failed"
    assert stored.completed_at is not None


@pytest.mark.asyncio
async def test_retry_attempt_restarts_failed_node_with_corrected_input(
    client: AsyncClient,
    db_session,
):
    headers = await _auth(client, "wfretryattempt")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Retry failed input",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["prepare"]},
                {
                    "id": "prepare",
                    "type": "transform",
                    "config": {"set": {"prepared": True}},
                    "next": ["unstable"],
                },
                {
                    "id": "unstable",
                    "type": "transform",
                    "config": {
                        "inputs": [
                            {
                                "key": "corrected_input",
                                "value": "{{corrected_input}}",
                            }
                        ],
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "corrected_input": {
                                    "type": "string",
                                    "minLength": 1,
                                }
                            },
                            "required": ["corrected_input"],
                            "additionalProperties": True,
                        },
                        "set": {"recovered": "{{corrected_input}}"},
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
            "variables": {"corrected_input": ""},
        },
    )).json()

    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert failed["status"] == "failed"
    assert failed["current_step_id"] == "unstable"
    assert failed["workflow_name"] == "Retry failed input"
    assert failed["current_step_name"] == "Unstable"
    original_error = failed["error"]
    original_prepare = failed["step_results"]["prepare"]

    response = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={
            "from_step_id": "unstable",
            "variables": {"corrected_input": "ready"},
            "execute": False,
        },
    )

    retried = await _execute_created_retry(client, headers, response)
    assert retried["id"] != failed["id"]
    assert retried["status"] == "completed"
    assert retried["retry_of_run_id"] == failed["id"]
    assert retried["retry_from_step_id"] == "unstable"
    assert retried["attempt_number"] == 2
    assert retried["lineage_status"] == "canonical"
    assert retried["workflow_name"] == failed["workflow_name"]
    assert retried["current_step_name"] == "End"
    assert retried["trigger_data"]["_workflow_name"] == "Retry failed input"
    assert retried["trigger_data"]["_workflow_node_names"] == failed["trigger_data"]["_workflow_node_names"]
    assert retried["variables"]["corrected_input"] == "ready"
    assert retried["variables"]["recovered"] == "ready"
    assert retried["step_results"]["prepare"] == original_prepare
    assert retried["step_results"]["unstable"]["status"] == "completed"
    assert retried["definition_snapshot"] == failed["definition_snapshot"]
    assert retried["execution_trace"] != failed["execution_trace"]
    assert retried["execution_trace"][0]["sequence"] == 1
    assert {
        entry["attempt_number"] for entry in retried["execution_trace"]
    } == {2}

    from sqlalchemy import select

    from packages.core.models.workflow import WorkflowRun

    stored_runs = list((await db_session.execute(
        select(WorkflowRun).where(WorkflowRun.id.in_([failed["id"], retried["id"]]))
    )).scalars())
    stored_by_id = {stored.id: stored for stored in stored_runs}
    assert stored_by_id[failed["id"]].lineage_root_run_id == failed["id"]
    assert stored_by_id[retried["id"]].lineage_root_run_id == failed["id"]
    assert stored_by_id[retried["id"]].lineage_is_legacy is False

    unchanged = (await client.get(
        f"/api/v1/workflows/runs/{failed['id']}",
        headers=headers,
    )).json()
    assert unchanged["status"] == "failed"
    assert unchanged["error"] == original_error
    assert unchanged["step_results"]["unstable"]["status"] == "failed"


@pytest.mark.asyncio
async def test_internal_retry_reloads_cancelled_latest_attempt_before_recovery(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.models.workflow import WorkflowRun
    from packages.core.constants.task import CancelSource
    from packages.core.services.workflow_run_control import cancel_run
    from packages.core.services.workflow_service import retry_workflow_run

    headers = await _auth(client, "wfinternalretrycancelrace")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Cancelled internal recovery", "steps": _simple_steps(["work"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    stale_failed = await db_session.get(WorkflowRun, failed["id"])
    stale_failed.status = "failed"
    stale_failed.current_step_id = "work"
    await db_session.commit()

    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with sessions() as cancel_db:
        cancelled = await cancel_db.get(WorkflowRun, failed["id"])
        cancel_run(
            cancelled,
            actor_id=stale_failed.started_by,
            source=CancelSource.USER,
            allow_problem_terminal=True,
        )
        await cancel_db.commit()

    assert stale_failed.status == "failed"
    with pytest.raises(ValueError, match="newer workflow attempt"):
        await retry_workflow_run(
            db_session,
            run_id=failed["id"],
            entity_id=stale_failed.entity_id,
            started_by=stale_failed.started_by,
            from_step_id="work",
            expected_latest_run_id=failed["id"],
        )
    await db_session.rollback()

    assert await db_session.scalar(
        select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.workflow_id == workflow["id"],
        )
    ) == 1
    await db_session.refresh(stale_failed)
    assert stale_failed.status == "cancelled"


@pytest.mark.asyncio
async def test_retry_keeps_frozen_workflow_effect_deadline(db_session, client: AsyncClient):
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import retry_workflow_run

    headers = await _auth(client, "workflow_deadline_retry")
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers,
        json={"name": "Time-limited submission", "variables": {"deadline": ""}, "steps": _simple_steps(["submit"])},
    )).json()
    started = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={
            "execute": False,
            "trigger_data": {"_workflow_runtime_context": {
                "workflow_effect_deadline_at": "2030-01-01T00:00:00Z",
                "workflow_effect_deadline_action_keys": ["submission.send"],
            }},
        },
    )).json()
    prior = await db_session.get(WorkflowRun, started["id"])
    assert "_workflow_runtime_context" not in prior.trigger_data
    prior.status = "failed"
    prior.current_step_id = "submit"
    prior.trigger_data = {"_workflow_runtime_context": {
        "workflow_effect_deadline_at": "2026-10-14T00:00:00Z",
        "workflow_effect_deadline_action_keys": ["submission.send"],
    }}
    await db_session.commit()

    retried = await retry_workflow_run(
        db_session, run_id=prior.id, entity_id=prior.entity_id, started_by=prior.started_by,
        from_step_id="submit", variables={"deadline": "2030-01-01T00:00:00Z"},
    )
    assert retried.trigger_data["_workflow_runtime_context"] == prior.trigger_data["_workflow_runtime_context"]
    assert retried.trigger_data["input_patch"]["deadline"] == "2030-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_retry_rejects_completed_producer_with_invalid_contract(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretryblockedproducer")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Published once", "steps": _simple_steps(["publish"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "publish"
    failed_row.step_results = {
        "publish": {
            "status": "failed",
            "code": "output_schema_validation_failed",
            "producer_completed": True,
            "retry_blocked": True,
            "output": {"external_receipt": "receipt-1"},
        }
    }
    await db_session.commit()

    from packages.core.services.workflow_service import retry_workflow_run

    with pytest.raises(ValueError, match="producer already completed"):
        await retry_workflow_run(
            db_session,
            run_id=failed["id"],
            entity_id=failed_row.entity_id,
            started_by=failed_row.started_by,
            from_step_id="publish",
        )


@pytest.mark.asyncio
async def test_retry_rejects_step_with_uncertain_external_effect(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import retry_workflow_run

    headers = await _auth(client, "wfretryblockeduncertain")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Uncertain upload", "steps": _simple_steps(["upload"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "upload"
    failed_row.step_results = {
        "upload": {
            "status": "failed",
            "retryable": False,
            "do_not_replay": True,
            "retry_blocked": True,
        }
    }
    await db_session.commit()

    with pytest.raises(ValueError, match="Workflow retry is blocked"):
        await retry_workflow_run(
            db_session,
            run_id=failed["id"],
            entity_id=failed_row.entity_id,
            started_by=failed_row.started_by,
            from_step_id="upload",
        )


@pytest.mark.asyncio
async def test_retry_rejects_unconfirmed_failed_effect_without_recovery_checkpoint(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import retry_workflow_run

    headers = await _auth(client, "wfretryunconfirmed")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Unconfirmed upload", "steps": _simple_steps(["upload"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "upload"
    failed_row.step_results = {
        "start": {"status": "completed", "effect_state": "confirmed"},
        "upload": {"status": "failed", "effect_state": "unconfirmed", "do_not_replay": True},
    }
    await db_session.commit()

    with pytest.raises(ValueError, match="prior effect must be reconciled"):
        await retry_workflow_run(
            db_session,
            run_id=failed["id"],
            entity_id=failed_row.entity_id,
            started_by=failed_row.started_by,
            from_step_id="upload",
        )


@pytest.mark.asyncio
async def test_operator_review_retries_exact_failed_report_delivery_without_rewriting_history(
    client: AsyncClient, db_session,
):
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import retry_workflow_run

    headers = await _auth(client, "report_review")
    steps = _simple_steps(["deliver_report"])
    steps[1].update(type="agent", config={"prompt": "Deliver the approved client report."})
    response = await client.post("/api/v1/workflows", headers=headers, json={"name": "Client report delivery", "steps": steps})
    assert response.status_code == 201, response.text
    workflow = response.json()
    failed = (await client.post(f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={
        "execute": False, "trigger_data": {"_workflow_effect_review": {"decision": "not_applied"}},
    })).json()
    prior = await db_session.get(WorkflowRun, failed["id"])
    assert "_workflow_effect_review" not in prior.trigger_data
    original_result = {"status": "failed", "effect_state": "unconfirmed", "do_not_replay": True, "retry_blocked": True}
    prior.status, prior.current_step_id = "failed", "deliver_report"
    prior.step_results = {"trigger": {"status": "completed"}, "deliver_report": original_result}
    frozen_scope = {"workflow_effect_deadline_at": "2026-10-14T00:00:00Z", "workflow_effect_deadline_action_keys": ["submission.send"]}
    prior.trigger_data = {"_workflow_runtime_context": frozen_scope}
    await db_session.commit()
    path = f"/api/v1/workflows/runs/{prior.id}"
    compact = (await client.get(path + "?detail=false", headers=headers)).json()
    action = compact["intervention"]
    assert action["kind"] == "workflow_effect_review"
    assert compact["retry_from_step_id"] is None
    review = {"step_id": action["step_id"], "effect_fingerprint": action["effect_fingerprint"], "decision": "not_applied", "confirmed": True, "note": "Checked the delivery provider audit log and outbound queue: no report was sent and no operation remains pending."}
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False})).status_code == 409
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": {**review, "confirmed": False}})).status_code == 422
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": {**review, "effect_fingerprint": "0" * 64}})).status_code == 409
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review, "variables": {"deadline": "2030-01-01"}})).status_code == 409
    other_headers = await _auth(client, "report_review_other")
    assert (await client.post(path + "/retry", headers=other_headers, json={"execute": False, "effect_review": review})).status_code in {403, 404}
    # A producer receipt arriving after the review invalidates its fingerprint.
    prior.step_results = {**prior.step_results, "deliver_report": {**original_result, "effect_state": "confirmed"}}
    await db_session.commit()
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review})).status_code == 409
    assert (await client.get(path + "?detail=false", headers=headers)).json()["intervention"] is None
    prior.step_results = {**prior.step_results, "deliver_report": deepcopy(original_result)}
    await db_session.commit()
    response = await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review})
    assert response.status_code == 201, response.text
    retried = await db_session.get(WorkflowRun, response.json()["id"])
    audit = retried.trigger_data["_workflow_effect_review"]
    assert audit["reviewed_by"] == prior.started_by and audit["source_run_id"] == prior.id
    assert audit["effect_fingerprint"] == action["effect_fingerprint"] and audit["note"] == review["note"]
    assert retried.trigger_data["_workflow_runtime_context"] == frozen_scope
    assert retried.execution_snapshot == prior.execution_snapshot
    assert retried.step_results["trigger"] == {"status": "completed"}
    recovery = retried.step_results["deliver_report"]
    assert recovery["effect_state"] == "none" and recovery["do_not_replay"] is False
    assert recovery["agent_recovery"]["operator_effect_review"] == audit
    assert recovery["agent_recovery"]["attempts"][0]["result"] == original_result
    await db_session.refresh(prior)
    assert prior.step_results["deliver_report"] == original_result
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review})).status_code == 409
    # A failed dispatch of the new attempt can retry without resurrecting the
    # already reviewed ambiguity. The immutable old attempt stays unchanged.
    retried.status = "failed"
    await db_session.commit()
    continued = await retry_workflow_run(db_session, run_id=retried.id, entity_id=retried.entity_id, started_by=retried.started_by)
    assert continued.retry_of_run_id == retried.id


@pytest.mark.asyncio
async def test_operator_applied_review_continues_report_without_repeating_delivery(client, db_session, monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "report_applied")
    schema = {"type": "object", "required": ["receipt_id"], "additionalProperties": False,
              "properties": {"receipt_id": {"type": "string", "minLength": 1}}}
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Client report delivery", "steps": [
            {"id": "start", "type": "trigger", "next": ["deliver"]},
            {"id": "deliver", "type": "agent", "config": {
                "prompt": "Deliver a client report", "output_schema": schema, "output_var": "delivery",
            }, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
    })).json()
    created = (await client.post(f"/api/v1/workflows/{workflow['id']}/run", headers=headers,
                                json={"execute": False})).json()
    prior = await db_session.get(WorkflowRun, created["id"])
    prior.status, prior.current_step_id = "failed", "deliver"
    original = {"status": "failed", "effect_state": "unconfirmed", "do_not_replay": True}
    prior.step_results = {"start": {"status": "completed"}, "deliver": original}
    frozen = {"workflow_effect_deadline_at": "2020-01-01T00:00:00Z", "workflow_effect_deadline_action_keys": ["submission.send"]}
    prior.trigger_data = {"_workflow_runtime_context": frozen}
    await db_session.commit()
    path = f"/api/v1/workflows/runs/{prior.id}"
    action = (await client.get(path + "?detail=false", headers=headers)).json()["intervention"]
    assert "review_applied" in action["options"] and action["result_schema"] == schema
    review = {"step_id": "deliver", "effect_fingerprint": action["effect_fingerprint"], "decision": "applied",
              "confirmed": True, "note": "Verified the exact report and recipient in the provider delivery receipt; no delivery remains pending.",
              "result": {"receipt_id": "report-receipt-42"}}
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": {**review, "result": {}}})).status_code == 409
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": {**review, "effect_fingerprint": "0" * 64}})).status_code == 409
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review, "variables": {"deadline": "2999-01-01"}})).status_code == 409
    other = await _auth(client, "report_applied_other")
    assert (await client.post(path + "/retry", headers=other, json={"execute": False, "effect_review": review})).status_code in {403, 404}
    response = await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review})
    assert response.status_code == 201, response.text
    retried = await db_session.get(WorkflowRun, response.json()["id"])
    assert retried.step_results["deliver"]["status"] == "completed"
    assert retried.step_results["deliver"]["do_not_replay"] is True
    assert retried.variables["delivery"] == review["result"]
    assert retried.trigger_data["_workflow_runtime_context"] == frozen
    assert retried.execution_snapshot == prior.execution_snapshot
    assert retried.step_results["deliver"]["agent_recovery"]["attempts"][0]["result"] == original

    async def must_not_deliver(*args, **kwargs):
        pytest.fail("The verified delivery must not execute again")

    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", must_not_deliver)
    await WorkflowRunner().run(retried.id)
    finished = (await client.get(f"/api/v1/workflows/runs/{retried.id}", headers=headers)).json()
    assert finished["status"] == "completed", finished.get("error")
    assert finished["step_results"]["end"]["status"] == "completed"
    await db_session.refresh(prior)
    assert prior.status == "failed" and prior.step_results["deliver"] == original
    assert (await client.post(path + "/retry", headers=headers, json={"execute": False, "effect_review": review})).status_code == 409


@pytest.mark.asyncio
async def test_supplied_document_continues_failed_run_through_its_qa(client, db_session, monkeypatch):
    """A video finished in chat closes the failed run: QA judges it, the producer does not rerun."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.document import Document
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "supplied_output")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    video_schema = {"type": "object", "required": ["video_document_id"], "additionalProperties": False,
                    "properties": {"video_document_id": {"type": "string", "minLength": 1}}}
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Make a video", "steps": [
            {"id": "start", "type": "trigger", "next": ["produce"]},
            {"id": "produce", "type": "agent", "config": {
                "prompt": "Make the video", "output_schema": video_schema, "output_var": "production",
            }, "next": ["qa"]},
            {"id": "qa", "type": "agent", "config": {
                "prompt": "Check the video", "output_var": "qa", "output_schema": {
                    "type": "object", "required": ["approved"], "properties": {"approved": {"type": "boolean"}}},
            }, "next": ["qa_passed"]},
            {"id": "qa_passed", "type": "condition", "config": {"expression": "qa.approved == true"},
             "true_next": ["done"], "false_next": ["rejected"]},
            {"id": "done", "type": "end", "next": []},
            {"id": "rejected", "type": "stop", "config": {"message": "QA rejected the video"}, "next": []},
        ],
    })).json()
    created = (await client.post(f"/api/v1/workflows/{workflow['id']}/run", headers=headers,
                                json={"execute": False})).json()
    prior = await db_session.get(WorkflowRun, created["id"])
    prior.status, prior.current_step_id, prior.error = "failed", "produce", "Video render timed out"
    failed_production = {"status": "failed", "error": "Video render timed out"}
    prior.step_results = {"start": {"status": "completed"}, "produce": failed_production}
    video = Document(entity_id=me["entity_id"], owner_id=me["id"], name="final.mp4", fs_path="chat/final.mp4",
                     file_type="mp4", mime_type="video/mp4", source="agent", vector_status="ready")
    others_video = Document(entity_id="another-team", name="theirs.mp4", fs_path="theirs.mp4", file_type="mp4")
    db_session.add_all([video, others_video])
    await db_session.commit()
    path = f"/api/v1/workflows/runs/{prior.id}/retry"
    supplied = {"step_id": "produce", "output": {"video_document_id": video.id},
                "document_ids": [video.id], "note": "Rendered in chat after the run timed out."}

    async def retry(supplied_output):
        return await client.post(path, headers=headers, json={"execute": False, "supplied_output": supplied_output})

    # The output must fit the producing step's contract and name readable Documents.
    assert (await retry({**supplied, "output": {"video_document_id": video.id, "extra": 1}})).status_code == 400
    assert (await retry({**supplied, "output": {"video_document_id": "doc-elsewhere"}})).status_code == 400
    assert (await retry({**supplied, "output": {"video_document_id": others_video.id},
                         "document_ids": [others_video.id]})).status_code == 400
    assert (await retry({**supplied, "step_id": "qa_passed"})).status_code == 400

    response = await retry(supplied)
    assert response.status_code == 201, response.text
    retried = await db_session.get(WorkflowRun, response.json()["id"])
    assert retried.retry_of_run_id == prior.id
    assert retried.step_results["produce"]["output"] == supplied["output"]
    assert retried.variables["production"] == supplied["output"]
    audit = retried.trigger_data["_workflow_supplied_output"]
    assert audit["document_ids"] == [video.id] and audit["supplied_by"] == me["id"]
    assert audit["note"] == supplied["note"] and audit["source_run_id"] == prior.id

    reviewed = []

    async def agent_step(self, step, variables, *args, **kwargs):
        assert step["id"] == "qa", "The supplied production must not be made again"
        reviewed.append(variables["production"]["video_document_id"])
        return {"status": "completed", "output": {"approved": True}, "output_var": "qa"}

    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", agent_step)
    await WorkflowRunner().run(retried.id)
    finished = (await client.get(f"/api/v1/workflows/runs/{retried.id}", headers=headers)).json()
    assert finished["status"] == "completed", finished.get("error")
    assert reviewed == [video.id]
    assert finished["step_results"]["done"]["status"] == "completed"
    await db_session.refresh(prior)
    assert prior.status == "failed" and prior.step_results["produce"] == failed_production




@pytest.mark.asyncio
async def test_agent_retry_hydrates_legacy_history_then_preserves_it_on_later_failure(
    client: AsyncClient, db_session,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretryagenthistory")
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Recover existing external result", "steps": [
            {"id": "start", "type": "trigger", "next": ["work"]},
            {"id": "work", "type": "agent", "config": {
                "prompt": "Complete the requested operation"}, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
    })).json()
    original = (await client.post(f"/api/v1/workflows/{workflow['id']}/run", headers=headers,
                                  json={"execute": False})).json()
    row = await db_session.get(WorkflowRun, original["id"])
    row.status = "failed"
    row.current_step_id = "work"
    row.error = "Save response lost"
    row.step_results = {"start": {"status": "completed"}, "work": {
        "status": "failed", "output": {"resource_id": "original-resource", "success": False},
        "chrome_outcome": {"receipts": [{"operation_id": "original-save", "action_executed": True}]},
    }}
    original_result = deepcopy(row.step_results)
    await db_session.commit()

    async def retry(source_id):
        response = await client.post(f"/api/v1/workflows/runs/{source_id}/retry", headers=headers,
                                    json={"from_step_id": "work", "execute": False})
        assert response.status_code == 201, response.text
        return await db_session.get(WorkflowRun, response.json()["id"])

    legacy = await retry(row.id)
    # Emulate an already stored retry from before recovery history was retained.
    legacy.status = "failed"
    legacy.step_results = {"start": {"status": "completed"}, "work": {
        "status": "failed", "output": {"blocker": "Optional dialog still open"},
    }}
    await db_session.commit()
    recovered = await retry(legacy.id)
    attempts = recovered.step_results["work"]["agent_recovery"]["attempts"]
    assert [a["run_id"] for a in attempts] == [row.id, legacy.id]
    assert attempts[0]["result"]["output"]["resource_id"] == "original-resource"
    assert attempts[0]["result"]["chrome_outcome"]["receipts"][0]["action_executed"] is True
    assert attempts[1]["result"]["output"]["blocker"] == "Optional dialog still open"
    assert recovered.execution_snapshot == row.execution_snapshot

    WorkflowRunner()._record_step_result(
        {"id": "work", "type": "agent"}, {"status": "failed", "error": "Connection lost"}, recovered,
    )
    recovered.status = "failed"
    await db_session.commit()
    next_retry = await retry(recovered.id)
    attempts = next_retry.step_results["work"]["agent_recovery"]["attempts"]
    assert [a["run_id"] for a in attempts] == [row.id, legacy.id, recovered.id]
    await db_session.refresh(row)
    assert row.step_results == original_result
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_original_definition_retry_keeps_prior_snapshot_when_live_prompt_changes(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretrycurrentcopy")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Retry current copy", "steps": _simple_steps(["work"])},
    )).json()
    original_steps = deepcopy(workflow["steps"])
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    original_execution_snapshot = deepcopy(failed_row.execution_snapshot)
    assert original_execution_snapshot["steps"] == original_steps
    assert "execution_snapshot" not in failed
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    await db_session.commit()

    updated_steps = deepcopy(workflow["steps"])
    next(step for step in updated_steps if step["id"] == "work")["config"]["prompt"] = (
        "Use the verified external receipt even when its current label is stale."
    )
    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"steps": updated_steps},
    )
    assert updated.status_code == 200, updated.text

    retried = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False, "definition": "original"},
    )

    assert retried.status_code == 201, retried.text
    retry = retried.json()
    assert retry["retry_of_run_id"] == failed["id"]
    assert retry["definition_snapshot"] == failed["definition_snapshot"]
    assert "execution_snapshot" not in retry

    retry_row = await db_session.get(WorkflowRun, retry["id"])
    assert retry_row.execution_snapshot == original_execution_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(("definition", "expected"), [(None, "changed"), ("original", "original")])
async def test_retry_runs_current_definition_unless_original_requested(
    client: AsyncClient,
    db_session,
    definition,
    expected,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretrysnapshotexecution")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Retry immutable execution definition",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["work"]},
                {
                    "id": "work",
                    "type": "transform",
                    "config": {"set": {"result": "original"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    await db_session.commit()

    updated_steps = deepcopy(workflow["steps"])
    next(step for step in updated_steps if step["id"] == "work")["config"] = {
        "set": {"result": "changed"},
    }
    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"steps": updated_steps},
    )
    assert updated.status_code == 200, updated.text

    retried = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False, **({"definition": definition} if definition else {})},
    )

    retried_result = await _execute_created_retry(client, headers, retried)
    assert retried_result["status"] == "completed"
    assert retried_result["variables"]["result"] == expected


@pytest.mark.asyncio
async def test_original_definition_retry_keeps_prior_snapshot_when_live_graph_changes(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretrychangedgraph")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Retry changed graph", "steps": _simple_steps(["work"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    original_execution_snapshot = deepcopy(failed_row.execution_snapshot)
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    await db_session.commit()

    updated_steps = deepcopy(workflow["steps"])
    work = next(step for step in updated_steps if step["id"] == "work")
    work["next"] = ["review"]
    updated_steps.insert(-1, {
        "id": "review",
        "type": "transform",
        "config": {"set": {"reviewed": True}},
        "next": ["end"],
    })
    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"steps": updated_steps},
    )
    assert updated.status_code == 200, updated.text

    retried = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False, "definition": "original"},
    )

    assert retried.status_code == 201, retried.text
    retry = retried.json()
    assert retry["definition_snapshot"] == failed["definition_snapshot"]
    retry_row = await db_session.get(WorkflowRun, retry["id"])
    assert retry_row.execution_snapshot == original_execution_snapshot


@pytest.mark.asyncio
async def test_legacy_run_retries_only_on_current_definition_after_change(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretrylegacychanged")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Retry legacy changed", "steps": _simple_steps(["work"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    failed_row.execution_snapshot = {}
    await db_session.commit()

    updated_steps = deepcopy(workflow["steps"])
    next(step for step in updated_steps if step["id"] == "work")["config"]["prompt"] = (
        "Changed after the legacy attempt started."
    )
    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"steps": updated_steps},
    )
    assert updated.status_code == 200, updated.text

    retried = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False, "definition": "original"},
    )

    assert retried.status_code == 409
    assert "legacy run has no immutable execution snapshot" in retried.text.lower()

    retried = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False},
    )
    assert retried.status_code == 201, retried.text
    retry_row = await db_session.get(WorkflowRun, retried.json()["id"])
    assert retry_row.execution_snapshot["steps"] == updated.json()["steps"]


@pytest.mark.asyncio
async def test_retry_on_edited_workflow_resumes_at_checkpoint(
    client: AsyncClient,
    db_session,
):
    """An edited upstream node keeps its receipt, a removed one leaves no output,
    a node added before the checkpoint runs first, and the checkpoint runs as edited."""
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretryeditedcheckpoint")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Retry edited workflow", "steps": [
            {"id": "trigger", "type": "trigger", "next": ["prep"]},
            {"id": "prep", "type": "transform", "config": {"set": {"prep_value": "v1"}}, "next": ["extra"]},
            {"id": "extra", "type": "transform", "config": {"set": {"extra_value": "v1"}}, "next": ["work"]},
            {"id": "work", "type": "transform", "config": {"set": {"work_value": "v1"}}, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ]},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    source_fingerprint = failed_row.execution_snapshot["fingerprint"]
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    failed_row.variables = {**(failed_row.variables or {}), "prep_value": "v1", "extra_value": "v1"}
    failed_row.step_results = {
        "trigger": {"status": "completed", "output": {}},
        "prep": {"status": "completed", "output": {"prep_value": "v1"}},
        "extra": {"status": "completed", "output": {"extra_value": "v1"}},
        "work": {"status": "failed", "error": "Bad prompt"},
    }
    await db_session.commit()

    steps = {step["id"]: step for step in deepcopy(workflow["steps"])}
    steps["prep"]["config"] = {"set": {"prep_value": "edited"}}
    steps["prep"]["next"] = ["fresh"]
    steps["work"]["config"] = {"set": {"work_value": "fixed"}}
    fresh = {"id": "fresh", "type": "transform", "config": {"set": {"fresh_value": "added"}}, "next": ["work"]}
    updated_steps = [steps["trigger"], steps["prep"], fresh, steps["work"], steps["end"]]
    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"steps": updated_steps},
    )
    assert updated.status_code == 200, updated.text

    retried = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False},
    )
    assert retried.status_code == 201, retried.text
    retry_row = await db_session.get(WorkflowRun, retried.json()["id"])
    context = retry_row.trigger_data["_workflow_retry_context"]
    assert context["fingerprint"] == retry_row.execution_snapshot["fingerprint"] != source_fingerprint
    assert context["source_fingerprint"] == source_fingerprint
    assert context["entry_step_ids"] == ["fresh"]
    assert context["inherited_step_ids"] == ["trigger", "prep"]
    assert "extra_value" not in retry_row.variables

    result = await _execute_created_retry(client, headers, retried)
    assert result["status"] == "completed"
    assert result["variables"]["prep_value"] == "v1"
    assert result["variables"]["fresh_value"] == "added"
    assert result["variables"]["work_value"] == "fixed"
    assert "extra_value" not in result["variables"]


@pytest.mark.asyncio
async def test_retry_refuses_checkpoint_removed_from_workflow(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfretryremovedcheckpoint")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Retry removed checkpoint", "steps": _simple_steps(["prep", "work"])},
    )).json()
    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    failed_row = await db_session.get(WorkflowRun, failed["id"])
    failed_row.status = "failed"
    failed_row.current_step_id = "work"
    await db_session.commit()
    updated = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"steps": _simple_steps(["prep"])},
    )
    assert updated.status_code == 200, updated.text

    retried = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={"from_step_id": "work", "execute": False},
    )

    assert retried.status_code == 409
    assert "no longer part of the workflow" in retried.text
    assert (await db_session.get(WorkflowRun, failed["id"])).status == "failed"


@pytest.mark.asyncio
async def test_retry_attempt_can_resume_cancelled_run(client: AsyncClient):
    headers = await _auth(client, "wfretrycancelled")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Retry cancelled run",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["work"]},
                {
                    "id": "work",
                    "type": "transform",
                    "config": {"set": {"recovered": True}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    pending = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    cancelled = (await client.post(
        f"/api/v1/workflows/runs/{pending['id']}/cancel",
        headers=headers,
    )).json()
    assert cancelled["status"] == "cancelled"

    response = await client.post(
        f"/api/v1/workflows/runs/{pending['id']}/retry",
        headers=headers,
        json={"from_step_id": "start", "execute": False},
    )

    retried = await _execute_created_retry(client, headers, response)
    assert retried["status"] == "completed"
    assert retried["retry_of_run_id"] == pending["id"]
    assert retried["retry_from_step_id"] == "start"
    assert retried["attempt_number"] == 2
    assert retried["variables"]["recovered"] is True


@pytest.mark.asyncio
async def test_cancelled_agent_without_result_requires_review_before_retry(client: AsyncClient, db_session):
    headers = await _auth(client, "wfcancelledreview")
    schema = {
        "type": "object",
        "required": ["receipt_id"],
        "properties": {"receipt_id": {"type": "string", "minLength": 1}},
    }
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Cancelled delivery",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["deliver"]},
                {"id": "deliver", "type": "agent", "config": {
                    "prompt": "Deliver the approved report.",
                    "output_schema": schema,
                    "output_var": "delivery",
                }, "next": ["end"]},
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    pending = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )).json()
    from packages.core.models.workflow import WorkflowRun

    interrupted = await db_session.get(WorkflowRun, pending["id"])
    interrupted.status, interrupted.current_step_id = "running", "deliver"
    interrupted.step_results = {"start": {"status": "completed"}}
    await db_session.commit()
    cancelled = (await client.post(
        f"/api/v1/workflows/runs/{pending['id']}/cancel",
        headers=headers,
    )).json()
    assert cancelled["status"] == "cancelled"

    path = f"/api/v1/workflows/runs/{pending['id']}"
    compact = (await client.get(path + "?detail=false", headers=headers)).json()
    action = compact["intervention"]
    assert compact["retry_from_step_id"] is None
    assert action["kind"] == "workflow_effect_review"
    assert "review" in action["options"] and "review_applied" in action["options"]

    # The endpoint enforces the same boundary as the presentation layer.
    blocked = await client.post(path + "/retry", headers=headers, json={"execute": False})
    assert blocked.status_code == 409
    review = {
        "step_id": action["step_id"],
        "effect_fingerprint": action["effect_fingerprint"],
        "decision": "not_applied",
        "confirmed": True,
        "note": "Checked the delivery audit and provider log; no report was sent or queued.",
    }
    retried = await client.post(path + "/retry", headers=headers, json={
        "execute": False,
        "effect_review": review,
    })
    assert retried.status_code == 201, retried.text
    assert retried.json()["retry_from_step_id"] == "deliver"


@pytest.mark.asyncio
async def test_cancelled_agent_applied_review_skips_delivery_and_continues_downstream(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfcancelledapplied")
    schema = {
        "type": "object",
        "required": ["receipt_id"],
        "properties": {"receipt_id": {"type": "string", "minLength": 1}},
    }
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Cancelled report delivery",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["deliver"]},
                {"id": "deliver", "type": "agent", "config": {
                    "prompt": "Deliver the approved report.",
                    "output_schema": schema,
                    "output_format": "json",
                    "output_var": "delivery",
                }, "next": ["record"]},
                {"id": "record", "type": "transform", "config": {
                    "set": {"recorded_receipt": "{{delivery.receipt_id}}"},
                }, "next": ["end"]},
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    pending_response = await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )
    assert pending_response.status_code == 201, pending_response.text
    pending = pending_response.json()
    interrupted = await db_session.get(WorkflowRun, pending["id"])
    interrupted.status, interrupted.current_step_id = "running", "deliver"
    interrupted.step_results = {"start": {"status": "completed"}}
    await db_session.commit()
    cancelled = await client.post(
        f"/api/v1/workflows/runs/{pending['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    path = f"/api/v1/workflows/runs/{pending['id']}"
    action = (await client.get(path + "?detail=false", headers=headers)).json()["intervention"]
    review = {
        "step_id": "deliver",
        "effect_fingerprint": action["effect_fingerprint"],
        "decision": "applied",
        "confirmed": True,
        "note": "Verified the exact report receipt and recipient; no delivery remains pending.",
        "result": {"receipt_id": "report-receipt-42"},
    }
    response = await client.post(path + "/retry", headers=headers, json={
        "execute": False,
        "effect_review": review,
    })
    assert response.status_code == 201, response.text
    retried = await db_session.get(WorkflowRun, response.json()["id"])
    assert retried.step_results["deliver"]["status"] == "completed"
    assert retried.step_results["deliver"]["do_not_replay"] is True

    async def must_not_deliver(*args, **kwargs):
        pytest.fail("The verified delivery must not execute again")

    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", must_not_deliver)
    await WorkflowRunner().run(retried.id)
    finished = (await client.get(f"/api/v1/workflows/runs/{retried.id}", headers=headers)).json()
    assert finished["status"] == "completed", finished.get("error")
    assert finished["variables"]["recorded_receipt"] == "report-receipt-42"


@pytest.mark.asyncio
async def test_retry_attempt_resumes_failed_operation_inside_stage(client: AsyncClient):
    headers = await _auth(client, "wfstagretry")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Retry internal stage operation",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["prepare_stage"]},
                {
                    "id": "prepare_stage",
                    "type": "stage",
                    "config": {
                        "entry_operation_id": "prepare",
                        "operations": [{
                            "id": "prepare",
                            "type": "transform",
                            "config": {"set": {"prepared": True}},
                            "next": ["continue"],
                        }],
                        "routes": {"continue": "work"},
                    },
                    "next": ["work"],
                },
                {
                    "id": "work",
                    "type": "stage",
                    "config": {
                        "entry_operation_id": "collect",
                        "operations": [
                            {
                                "id": "collect",
                                "type": "transform",
                                "config": {"set": {"collected": True}},
                                "next": ["render"],
                            },
                            {
                                "id": "render",
                                "type": "transform",
                                "config": {
                                    "inputs": [{
                                        "key": "corrected_input",
                                        "value": "{{corrected_input}}",
                                    }],
                                    "input_schema": {
                                        "type": "object",
                                        "properties": {
                                            "corrected_input": {
                                                "type": "string",
                                                "minLength": 1,
                                            }
                                        },
                                        "required": ["corrected_input"],
                                        "additionalProperties": True,
                                    },
                                    "set": {"rendered": "{{corrected_input}}"},
                                },
                                "next": ["done"],
                            },
                        ],
                        "routes": {"done": "end"},
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
            "variables": {"corrected_input": ""},
        },
    )).json()

    failed = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert failed["status"] == "failed"
    assert failed["current_step_id"] == "work"
    failed_state = failed["variables"]["__stage_execution"]["work"]
    assert failed_state["failed_operation_id"] == "render"
    original_collect = failed_state["operation_results"]["collect"]

    response = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={
            "from_step_id": "work",
            "variables": {"corrected_input": "ready"},
            "execute": False,
        },
    )

    retried = await _execute_created_retry(client, headers, response)
    assert retried["status"] == "completed"
    assert retried["variables"]["rendered"] == "ready"
    assert set(retried["variables"]["__stage_execution"]) == {"work"}
    retried_state = retried["variables"]["__stage_execution"]["work"]
    assert retried_state["operation_results"]["collect"] == original_collect
    assert retried_state["operation_results"]["render"]["status"] == "completed"
    assert retried["step_results"]["prepare_stage"] == failed["step_results"][
        "prepare_stage"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("business_outcome", ["needs_input", "revision_required"])
async def test_retry_attempt_accepts_retryable_completed_business_outcome(
    client: AsyncClient,
    business_outcome: str,
):
    headers = await _auth(client, f"wfretry{business_outcome.replace('_', '')}")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": f"Retry {business_outcome}",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["prepare"]},
                {
                    "id": "prepare",
                    "type": "transform",
                    "config": {"set": {"prepared": True}},
                    "next": ["handoff"],
                },
                {
                    "id": "handoff",
                    "type": "transform",
                    "config": {
                        "set": {
                            "project": {
                                "state": {
                                    "business_outcome": business_outcome,
                                    "retry_state": {
                                        "retry_from_step_id": "handoff",
                                    },
                                }
                            }
                        }
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
            "variables": {"corrected_input": ""},
        },
    )).json()
    prior = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={},
    )).json()
    assert prior["status"] == "completed"

    response = await client.post(
        f"/api/v1/workflows/runs/{prior['id']}/retry",
        headers=headers,
        json={
            "variables": {"corrected_input": "ready"},
            "execute": False,
        },
    )

    assert response.status_code == 201, response.text
    retried = response.json()
    assert retried["status"] == "running"
    assert retried["retry_of_run_id"] == prior["id"]
    assert retried["retry_from_step_id"] == "handoff"
    assert retried["attempt_number"] == 2
    assert retried["variables"]["corrected_input"] == "ready"
    assert retried["step_results"]["prepare"] == prior["step_results"]["prepare"]
    assert "handoff" not in retried["step_results"]

    unchanged = (await client.get(
        f"/api/v1/workflows/runs/{prior['id']}",
        headers=headers,
    )).json()
    assert unchanged["status"] == "completed"


@pytest.mark.asyncio
async def test_business_retry_reexecutes_completed_stage_with_durable_project(
    client: AsyncClient,
):
    headers = await _auth(client, "wfbusinessstagereexecute")
    state_schema = {"type": "object", "additionalProperties": True}
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Retry completed browser stage",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["prepare_project"]},
                {
                    "id": "prepare_project",
                    "type": "stage",
                    "config": {
                        "entry_operation_id": "create_project",
                        "operations": [{
                            "id": "create_project",
                            "type": "workflow_project",
                            "config": {
                                "operation": "create",
                                "project_type": "retry_test",
                                "schema_version": 1,
                                "state_schema": state_schema,
                                "state": {"business_outcome": "in_progress"},
                                "output_var": "project",
                            },
                            "next": ["prepared"],
                        }],
                        "routes": {"prepared": "check_browser"},
                    },
                    "next": ["check_browser"],
                },
                {
                    "id": "check_browser",
                    "type": "stage",
                    "config": {
                        "entry_operation_id": "record_check",
                        "operations": [{
                            "id": "record_check",
                            "type": "transform",
                            "config": {"set": {"check_value": "{{corrected_input}}"}},
                            "next": ["checked"],
                        }],
                        "routes": {"checked": "mark_needs_input"},
                    },
                    "next": ["mark_needs_input"],
                },
                {
                    "id": "mark_needs_input",
                    "type": "stage",
                    "config": {
                        "entry_operation_id": "save_handoff",
                        "operations": [{
                            "id": "save_handoff",
                            "type": "workflow_project",
                            "config": {
                                "operation": "patch",
                                "project_id": "{{project.project_id}}",
                                "project_type": "retry_test",
                                "schema_version": 1,
                                "expected_revision": "{{project.revision}}",
                                "state_schema": state_schema,
                                "patch": {
                                    "business_outcome": "needs_input",
                                    "retry_state": {
                                        "retry_from_step_id": "check_browser",
                                    },
                                },
                                "output_var": "project",
                            },
                            "next": ["needs_input"],
                        }],
                        "routes": {"needs_input": None},
                    },
                    "next": [],
                },
            ],
            "variables": {"corrected_input": "initial"},
        },
    )).json()
    workspace = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Retry completed browser stage"},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": workspace["id"],
            "trigger_type": "manual",
        },
    )).json()
    prior = await _execute_queued_binding_run(
        client,
        headers,
        await client.post(
            f"/api/v1/workflows/bindings/{binding['id']}/run",
            headers=headers,
            json={},
        ),
    )

    assert prior["status"] == "completed", json.dumps(
        {
            "error": prior.get("error"),
            "step_results": prior.get("step_results"),
            "variables": prior.get("variables"),
        },
        indent=2,
        default=str,
    )
    assert prior["business_outcome"] == "needs_input"
    assert prior["variables"]["check_value"] == "initial"
    project_id = prior["variables"]["project"]["project_id"]
    prior_revision = prior["variables"]["project"]["revision"]

    response = await client.post(
        f"/api/v1/workflows/runs/{prior['id']}/retry",
        headers=headers,
        json={
            "variables": {"corrected_input": "ready"},
            "execute": False,
        },
    )

    retried = await _execute_created_retry(client, headers, response)
    assert retried["status"] == "completed"
    assert retried["retry_from_step_id"] == "check_browser"
    assert retried["variables"]["check_value"] == "ready"
    assert retried["variables"]["project"]["project_id"] == project_id
    assert retried["variables"]["project"]["revision"] > prior_revision
    assert retried["step_results"]["prepare_project"] == prior["step_results"][
        "prepare_project"
    ]
    assert retried["step_results"]["check_browser"]["status"] == "completed"
    assert retried["variables"]["__stage_execution"]["check_browser"][
        "operation_results"
    ]["record_check"]["status"] == "completed"


@pytest.mark.asyncio
async def test_retry_refuses_to_write_a_project_back_over_a_newer_run(
    client: AsyncClient,
    db_session,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowProject, WorkflowRun

    headers = await _auth(client, "wfretrystaleproject")
    state_schema = {"type": "object", "additionalProperties": True}
    project_step = {"project_type": "episode", "schema_version": 1, "state_schema": state_schema, "output_var": "project"}
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Make an episode",
        "steps": [
            {"id": "start", "type": "trigger", "next": ["open"]},
            {"id": "open", "type": "workflow_project", "next": ["make"],
             "config": {**project_step, "operation": "create", "state": {"take": ""}}},
            {"id": "make", "type": "transform", "next": ["finish"], "config": {"set": {"made": "yes"}}},
            {"id": "finish", "type": "workflow_project", "next": [],
             "config": {**project_step, "operation": "patch", "project_id": "{{project.project_id}}",
                        "expected_revision": "{{project.revision}}", "patch": {"take": "first"}}},
        ],
    })).json()
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Episodes"})).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"], "workspace_id": workspace["id"], "trigger_type": "manual",
    })).json()
    prior = await _execute_queued_binding_run(client, headers, await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run", headers=headers, json={},
    ))
    assert prior["status"] == "completed", prior.get("error")
    # Say it failed while making, before it finished the project.
    row = await db_session.get(WorkflowRun, prior["id"])
    row.status, row.current_step_id = "failed", "make"
    row.step_results = {key: value for key, value in row.step_results.items() if key not in {"make", "finish"}}
    await db_session.commit()
    project = await db_session.get(WorkflowProject, prior["variables"]["project"]["project_id"])

    def retry():
        return client.post(f"/api/v1/workflows/runs/{prior['id']}/retry", headers=headers,
                           json={"from_step_id": "make", "execute": False})

    # The run read the project before it failed; a later run made it again since.
    revision = prior["step_results"]["open"]["output"]["revision"]
    newer = await _execute_queued_binding_run(client, headers, await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run", headers=headers, json={},
    ))
    project.revision, project.last_run_id = revision + 1, newer["id"]
    await db_session.commit()
    refused = await retry()
    assert refused.status_code == 409, refused.text
    assert "newer version was made after it" in refused.text
    # Nor is going on offered: the run reads as replaced, with no retry action.
    shown = (await client.get(f"/api/v1/workflows/runs/{prior['id']}?detail=false", headers=headers)).json()
    assert shown["superseded"] is True
    assert (shown.get("intervention") or {}).get("kind") != "workflow_retry"
    # Nothing newer: the retry goes ahead.
    project.revision, project.last_run_id = revision, prior["id"]
    await db_session.commit()
    allowed = await retry()
    assert allowed.status_code == 201, allowed.text
    assert "superseded" not in (await client.get(f"/api/v1/workflows/runs/{prior['id']}", headers=headers)).json()
    # A newer run made the project while the retry ran: it closes quietly
    # instead of failing on a write that would overwrite that version.
    project.revision, project.last_run_id = revision + 1, newer["id"]
    await db_session.commit()
    await WorkflowRunner().run(allowed.json()["id"])
    closed = await db_session.get(WorkflowRun, allowed.json()["id"])
    await db_session.refresh(closed)
    assert (closed.status, closed.current_step_id) == ("cancelled", "finish")
    assert closed.error == "Replaced by a newer version"


@pytest.mark.asyncio
async def test_foreach_subworkflow_preserves_item_order(client: AsyncClient):
    headers = await _auth(client, "wfforeachorder")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Capture one scene",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["copy"]},
                {
                    "id": "copy",
                    "type": "transform",
                    "config": {"set": {"child_scene_id": "{{scene.scene_id}}"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent_response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Capture scenes",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["capture"]},
                {
                    "id": "capture",
                    "type": "foreach_subworkflow",
                    "config": {
                        "workflow_id": child["name"],
                        "over": "scenes",
                        "item_var": "scene",
                        "item_key": "scene_id",
                        "concurrency": 2,
                        "output_var": "scene_results",
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )
    assert parent_response.status_code == 201, parent_response.text
    parent = parent_response.json()

    run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={
            "variables": {
                "scenes": [
                    {"scene_id": "scene-2"},
                    {"scene_id": "scene-1"},
                    {"scene_id": "scene-3"},
                ]
            },
            "execute": True,
        },
    )).json()

    assert run["status"] == "completed", run
    assert [item["child_scene_id"] for item in run["variables"]["scene_results"]] == [
        "scene-2",
        "scene-1",
        "scene-3",
    ]
    child_run_ids = [
        item["subrun_id"]
        for item in run["step_results"]["capture"]["items"]
    ]
    child_runs = [
        (await client.get(
            f"/api/v1/workflows/runs/{child_run_id}",
            headers=headers,
        )).json()
        for child_run_id in child_run_ids
    ]
    assert all(
        child_run["trigger_data"]["_workflow_name"] == "Capture one scene"
        for child_run in child_runs
    )
    assert all(
        child_run["trigger_data"]["_workflow_node_names"] == {
            "start": "Start",
            "copy": "Copy",
            "end": "End",
        }
        for child_run in child_runs
    )
    assert all(child_run["workflow_name"] == "Capture one scene" for child_run in child_runs)
    assert all(child_run["current_step_name"] == "End" for child_run in child_runs)


@pytest.mark.asyncio
async def test_foreach_subworkflow_resumes_each_paused_child_before_parent(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfforeachresume")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Capture scene with approval",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["approval"]},
                {
                    "id": "approval",
                    "type": "wait",
                    "config": {"wait_type": "approval"},
                    "next": ["copy"],
                },
                {
                    "id": "copy",
                    "type": "transform",
                    "config": {"set": {"child_scene_id": "{{scene.scene_id}}"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Sequential scene barrier",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["capture"]},
                {
                    "id": "capture",
                    "type": "foreach_subworkflow",
                    "config": {
                        "workflow_id": child["id"],
                        "over": "scenes",
                        "item_var": "scene",
                        "item_key": "scene_id",
                        "concurrency": 1,
                        "output_var": "scene_results",
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    parent_run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={
            "variables": {
                "scenes": [{"scene_id": "scene-1"}, {"scene_id": "scene-2"}]
            }
        },
    )).json()
    assert parent_run["status"] == "paused", parent_run
    first_child_id = parent_run["step_results"]["capture"]["subrun_ids"][0]

    blocked_parent_resume = await client.post(
        f"/api/v1/workflows/runs/{parent_run['id']}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert blocked_parent_resume.status_code == 409
    still_paused_parent = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert still_paused_parent["status"] == "paused"
    assert (
        still_paused_parent["step_results"]["capture"]["status"]
        == "paused"
    )

    first_resume = await client.post(
        f"/api/v1/workflows/runs/{first_child_id}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert first_resume.status_code == 200, first_resume.text
    assert first_resume.json()["status"] == "running"
    await WorkflowRunner().run(first_child_id)

    parent_after_first = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert parent_after_first["status"] == "paused", parent_after_first
    second_child_id = parent_after_first["step_results"]["capture"]["subrun_ids"][0]
    assert second_child_id != first_child_id

    second_resume = await client.post(
        f"/api/v1/workflows/runs/{second_child_id}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert second_resume.status_code == 200, second_resume.text
    assert second_resume.json()["status"] == "running"
    await WorkflowRunner().run(second_child_id)

    completed_parent = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert completed_parent["status"] == "completed", completed_parent
    assert [
        item["child_scene_id"]
        for item in completed_parent["variables"]["scene_results"]
    ] == ["scene-1", "scene-2"]


@pytest.mark.asyncio
async def test_a_run_paused_on_a_nested_flow_offers_its_descendants_decision(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfnestedresume")
    child = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Approve the post",
        "steps": [
            {"id": "start", "type": "trigger", "next": ["approval"]},
            {"id": "approval", "type": "wait", "name": "Approve the post",
             "config": {"wait_type": "approval", "response_variable": "post_decision"}, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
    })).json()
    middle = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "One platform",
        "steps": [
            {"id": "start", "type": "trigger", "next": ["publish"]},
            {"id": "publish", "type": "subworkflow", "config": {"workflow_id": child["id"]}, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
    })).json()
    parent = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Daily batch",
        "steps": [
            {"id": "start", "type": "trigger", "next": ["platform"]},
            {"id": "platform", "type": "subworkflow", "config": {"workflow_id": middle["id"]}, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
    })).json()

    parent_run = (await client.post(f"/api/v1/workflows/{parent['id']}/run", headers=headers, json={})).json()
    assert parent_run["status"] == "paused", parent_run
    detail = (await client.get(f"/api/v1/workflows/runs/{parent_run['id']}", headers=headers)).json()
    action = detail["resume_action"]
    # The grandchild's approval, answered on that run.
    assert action["kind"] == "workflow_approval" and action["step_id"] == "approval"
    assert action["response_variable"] == "post_decision"
    assert action["workflow_run_id"] != parent_run["id"]

    resumed = await client.post(
        f"/api/v1/workflows/runs/{action['workflow_run_id']}/resume", headers=headers,
        json={"variables": {"post_decision": "approve"}, "expected_step_id": "approval"},
    )
    assert resumed.status_code == 200, resumed.text
    await WorkflowRunner().run(action["workflow_run_id"])
    finished = (await client.get(f"/api/v1/workflows/runs/{parent_run['id']}", headers=headers)).json()
    assert finished["status"] == "completed", finished
    assert finished.get("resume_action") is None


@pytest.mark.asyncio
async def test_a_chat_cancel_stops_at_a_step_that_handles_its_child_failing(client: AsyncClient, db_session):
    """Inside a batch, cancelling one item's review skips that item; the batch goes on."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.constants.task import CancelSource
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_run_control import cancel_run

    headers = await _auth(client, "wfchatcancelhandled")
    child = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Approve the post",
        "steps": [
            {"id": "start", "type": "trigger", "next": ["approval"]},
            {"id": "approval", "type": "wait", "config": {"wait_type": "approval"}, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
    })).json()
    parent = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "One platform",
        "steps": [
            {"id": "start", "type": "trigger", "next": ["publish"]},
            {"id": "publish", "type": "subworkflow", "config": {
                "workflow_id": child["id"], "output_var": "publish_run",
                "on_error": "continue_error", "error_next": ["record_skip"],
            }, "next": ["record_publish"]},
            {"id": "record_publish", "type": "transform", "config": {"set": {"outcome": "published"}}, "next": ["end"]},
            {"id": "record_skip", "type": "transform", "config": {"set": {"outcome": "skipped"}}, "next": ["end"]},
            {"id": "end", "type": "end", "next": []},
        ],
    })).json()
    parent_run = (await client.post(f"/api/v1/workflows/{parent['id']}/run", headers=headers, json={})).json()
    assert parent_run["status"] == "paused", parent_run

    # What a Cancel on the child's chat card does: cancel it and name the run to cascade to.
    child_run = await db_session.get(WorkflowRun, parent_run["step_results"]["publish"]["subrun_id"])
    cancel_run(child_run, actor_id="person", source=CancelSource.USER)
    child_run.trigger_data = {
        **child_run.trigger_data,
        "_workflow_chat_cancel_ancestors": {"host_run_id": parent_run["id"], "cancelled_by": "person"},
    }
    await db_session.commit()
    await WorkflowRunner()._propagate_subworkflow_result(child_run, db_session)

    finished = (await client.get(f"/api/v1/workflows/runs/{parent_run['id']}", headers=headers)).json()
    assert finished["status"] == "completed", finished
    assert finished["variables"]["outcome"] == "skipped"
    assert finished["variables"]["publish_run"]["error"]["cancelled"] is True


@pytest.mark.asyncio
async def test_subworkflow_continue_error_holds_when_the_child_fails_after_pausing(client: AsyncClient):
    from packages.core.ai.workflow_runner import WorkflowRunner

    headers = await _auth(client, "wfsubcontinueerror")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Approve then fail",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["approval"]},
                {"id": "approval", "type": "wait", "config": {"wait_type": "approval"}, "next": ["draft"]},
                {"id": "draft", "type": "transform", "config": {"set": {"post_text": "Shipped today."}}, "next": ["blocked"]},
                {"id": "blocked", "type": "stop", "config": {"message": "Not signed in"}, "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Record a child's failure",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["publish"]},
                {
                    "id": "publish",
                    "type": "subworkflow",
                    "config": {
                        "workflow_id": child["id"], "output_var": "publish_run",
                        "on_error": "continue_error", "error_next": ["record_failure"],
                    },
                    "next": ["record_success"],
                },
                {"id": "record_success", "type": "transform", "config": {"set": {"outcome": "published"}}, "next": ["end"]},
                {
                    "id": "record_failure",
                    "type": "transform",
                    "config": {"set": {"outcome": "failed", "reason": "{{publish_run.error.message}}"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    parent_run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run", headers=headers, json={},
    )).json()
    assert parent_run["status"] == "paused", parent_run
    child_id = parent_run["step_results"]["publish"]["subrun_id"]
    resumed = await client.post(
        f"/api/v1/workflows/runs/{child_id}/resume", headers=headers, json={"variables": {"decision": "approved"}},
    )
    assert resumed.status_code == 200, resumed.text
    await WorkflowRunner().run(child_id)

    finished = (await client.get(f"/api/v1/workflows/runs/{parent_run['id']}", headers=headers)).json()
    assert finished["status"] == "completed", finished
    assert finished["variables"]["outcome"] == "failed"
    assert "Not signed in" in finished["variables"]["reason"]
    # The error route also sees what the child made before it stopped.
    assert finished["variables"]["publish_run"]["post_text"] == "Shipped today."
    assert "record_success" not in finished["step_results"]


@pytest.mark.asyncio
async def test_foreach_subworkflow_cancelled_child_fails_parent(client: AsyncClient):
    headers = await _auth(client, "wfforeachcancel")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Cancellable child",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["approval"]},
                {
                    "id": "approval",
                    "type": "wait",
                    "config": {"wait_type": "approval"},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Cancel-aware parent",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["capture"]},
                {
                    "id": "capture",
                    "type": "foreach_subworkflow",
                    "config": {
                        "workflow_id": child["id"],
                        "over": "scenes",
                        "item_key": "scene_id",
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent_run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={
            "variables": {"scenes": [{"scene_id": "scene-1"}]},
            "execute": True,
        },
    )).json()
    assert parent_run["status"] == "paused", parent_run
    child_run_id = parent_run["step_results"]["capture"]["subrun_ids"][0]

    cancelled = await client.post(
        f"/api/v1/workflows/runs/{child_run_id}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    failed_parent = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert failed_parent["status"] == "failed", failed_parent
    assert "cancelled" in failed_parent["error"]


@pytest.mark.asyncio
async def test_foreach_subworkflow_continue_on_failure_runs_every_item(client: AsyncClient):
    headers = await _auth(client, "wfforeachcontinue")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Produce one episode",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["check"]},
                {
                    "id": "check",
                    "type": "condition",
                    "config": {"expression": "episode.id == 'EP.02'"},
                    "true_next": "broken",
                    "false_next": "produce",
                },
                {"id": "broken", "type": "stop", "config": {"message": "EP.02 script missing"}, "next": []},
                {
                    "id": "produce",
                    "type": "transform",
                    "config": {"set": {"made": "{{episode.id}}"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Produce a batch",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["each"]},
                {
                    "id": "each",
                    "type": "foreach_subworkflow",
                    "config": {
                        "workflow_id": child["id"],
                        "over": "episodes",
                        "item_var": "episode",
                        "concurrency": 1,
                        "continue_on_failure": True,
                        "output_var": "results",
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    async def run(ids: list[str]) -> dict:
        response = await client.post(
            f"/api/v1/workflows/{parent['id']}/run", headers=headers,
            json={"variables": {"episodes": [{"id": value} for value in ids]}, "execute": True},
        )
        return response.json()

    # One broken episode does not stop the episodes after it.
    batch = await run(["EP.01", "EP.02", "EP.03"])
    assert batch["status"] == "completed", batch
    results = batch["variables"]["results"]
    assert [result.get("made") for result in results] == ["EP.01", None, "EP.03"]
    assert results[1]["key"] == "EP.02" and results[1]["status"] == "failed"
    assert "EP.02 script missing" in results[1]["error"]
    assert batch["step_results"]["each"]["failed_count"] == 1

    # When nothing could be produced, the step still fails.
    only_broken = await run(["EP.02"])
    assert only_broken["status"] == "failed", only_broken


@pytest.mark.asyncio
async def test_foreach_subworkflow_rejects_parent_definition_change_before_child_merge(
    client: AsyncClient,
    db_session,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfforeachstaleparent")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Capture scene with approval",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["approval"]},
                {
                    "id": "approval",
                    "type": "wait",
                    "config": {"wait_type": "approval"},
                    "next": ["copy"],
                },
                {
                    "id": "copy",
                    "type": "transform",
                    "config": {"set": {"child_scene_id": "{{scene.scene_id}}"}},
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Stale parent barrier",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["capture"]},
                {
                    "id": "capture",
                    "type": "foreach_subworkflow",
                    "config": {
                        "workflow_id": child["id"],
                        "over": "scenes",
                        "item_var": "scene",
                        "item_key": "scene_id",
                        "concurrency": 1,
                        "output_var": "scene_results",
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    parent_run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={
            "variables": {
                "scenes": [{"scene_id": "scene-1"}, {"scene_id": "scene-2"}]
            }
        },
    )).json()
    assert parent_run["status"] == "paused", parent_run
    first_child_id = parent_run["step_results"]["capture"]["subrun_ids"][0]
    original_variables = deepcopy(parent_run["variables"])
    original_step_results = deepcopy(parent_run["step_results"])
    legacy_parent = await db_session.get(WorkflowRun, parent_run["id"])
    assert legacy_parent is not None
    legacy_parent.execution_snapshot = {}
    await db_session.commit()

    edited_steps = deepcopy(parent["steps"])
    next(step for step in edited_steps if step["id"] == "end")["name"] = "Edited end"
    update = await client.put(
        f"/api/v1/workflows/{parent['id']}",
        headers=headers,
        json={"steps": edited_steps},
    )
    assert update.status_code == 200, update.text

    child_resume = await client.post(
        f"/api/v1/workflows/runs/{first_child_id}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert child_resume.status_code == 200, child_resume.text
    assert child_resume.json()["status"] == "running"
    await WorkflowRunner().run(first_child_id)

    stale_parent = (await client.get(
        f"/api/v1/workflows/runs/{parent_run['id']}",
        headers=headers,
    )).json()
    assert stale_parent["status"] == "failed", stale_parent
    assert stale_parent["error"] == (
        "Workflow definition changed since this run started. Start a new run."
    )
    assert stale_parent["variables"] == original_variables
    assert stale_parent["step_results"] == original_step_results


@pytest.mark.asyncio
async def test_foreach_subworkflow_rejects_duplicate_item_keys(client: AsyncClient):
    headers = await _auth(client, "wfforeachduplicate")
    child = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Child", "steps": _simple_steps([])},
    )).json()
    parent = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Duplicate scene keys",
            "steps": [
                {"id": "start", "type": "trigger", "next": ["capture"]},
                {
                    "id": "capture",
                    "type": "foreach_subworkflow",
                    "config": {
                        "workflow_id": child["id"],
                        "over": "scenes",
                        "item_var": "scene",
                        "item_key": "scene_id",
                    },
                    "next": ["end"],
                },
                {"id": "end", "type": "end", "next": []},
            ],
        },
    )).json()

    run = (await client.post(
        f"/api/v1/workflows/{parent['id']}/run",
        headers=headers,
        json={
            "variables": {
                "scenes": [{"scene_id": "same"}, {"scene_id": "same"}]
            }
        },
    )).json()

    assert run["status"] == "failed"
    assert "duplicate" in run["error"].lower()


# ── Workflow import (ComfyUI / n8n / Dify) ──

_DIFY_DSL = """
app:
  name: Imported Triage
  mode: workflow
kind: app
version: 0.1.5
workflow:
  graph:
    nodes:
      - id: "start1"
        data: {type: start, title: Start}
      - id: "llm1"
        data: {type: llm, title: Draft}
      - id: "end1"
        data: {type: end, title: Done}
    edges:
      - {source: "start1", target: "llm1"}
      - {source: "llm1", target: "end1"}
"""

_DIFY_UNMAPPED_DSL = """
app:
  name: Imported Partial
  mode: workflow
kind: app
version: 0.1.5
workflow:
  graph:
    nodes:
      - id: "start1"
        data: {type: start, title: Start}
      - id: "legacy1"
        data: {type: vendor_only_node, title: Legacy}
      - id: "end1"
        data: {type: end, title: Done}
    edges:
      - {source: "start1", target: "legacy1"}
      - {source: "legacy1", target: "end1"}
"""

_N8N_JSON = {
    "name": "Imported Sync",
    "nodes": [
        {"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}},
        {"name": "HTTP", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
    ],
    "connections": {"Webhook": {"main": [[{"node": "HTTP", "type": "main", "index": 0}]]}},
}


@pytest.mark.asyncio
async def test_import_dify_workflow(client: AsyncClient):
    headers = await _auth(client, "importer1")
    resp = await client.post("/api/v1/workflows/import", headers=headers, json={
        "content": _DIFY_DSL,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["report"]["source_tool"] == "dify"
    assert data["report"]["coverage"] == 1.0
    assert data["workflow"]["name"] == "Imported Triage"
    types = {s["type"] for s in data["workflow"]["steps"]}
    assert {"trigger", "llm", "end"} <= types
    assert "imported:dify" in data["workflow"]["tags"]


@pytest.mark.asyncio
async def test_imported_unmapped_workflow_can_start_from_server_provenance(
    client: AsyncClient,
):
    headers = await _auth(client, "importerpartial")
    imported = await client.post(
        "/api/v1/workflows/import",
        headers=headers,
        json={"content": _DIFY_UNMAPPED_DSL},
    )
    assert imported.status_code == 200, imported.text
    workflow = imported.json()["workflow"]
    assert "imported:dify" in workflow["tags"]
    assert not any(
        tag.startswith("manor-import-proof:") for tag in workflow["tags"]
    )
    assert any(step["type"] == "unsupported" for step in workflow["steps"])

    started = await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )

    assert started.status_code == 201, started.text
    assert started.json()["current_step_id"] == "start1"

    cosmetic_update = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"description": "Cosmetic edit keeps importer proof valid"},
    )
    assert cosmetic_update.status_code == 200, cosmetic_update.text
    cosmetic_start = await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )
    assert cosmetic_start.status_code == 201, cosmetic_start.text

    edited_steps = cosmetic_update.json()["steps"]
    edited_steps[1] = {**edited_steps[1], "name": "User-edited legacy node"}
    graph_update = await client.put(
        f"/api/v1/workflows/{workflow['id']}",
        headers=headers,
        json={"steps": edited_steps},
    )
    assert graph_update.status_code == 200, graph_update.text
    edited_start = await client.post(
        f"/api/v1/workflows/{workflow['id']}/run",
        headers=headers,
        json={"execute": False},
    )
    assert edited_start.status_code == 409, edited_start.text
    assert "must be replaced" in edited_start.json()["detail"]


@pytest.mark.asyncio
async def test_import_dry_run_does_not_persist(client: AsyncClient):
    headers = await _auth(client, "importer2")
    resp = await client.post("/api/v1/workflows/import", headers=headers, json={
        "content": _DIFY_DSL, "dry_run": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "report" in data and "definition" in data
    assert "workflow" not in data  # not persisted
    # nothing was created
    listing = await client.get("/api/v1/workflows", headers=headers)
    assert listing.json() == []


@pytest.mark.asyncio
async def test_import_n8n_with_workspace_binding(client: AsyncClient):
    import json as _json
    headers = await _auth(client, "importer3")
    workspace_id = await _create_workspace(client, headers, "Imported workflow")
    resp = await client.post("/api/v1/workflows/import", headers=headers, json={
        "content": _json.dumps(_N8N_JSON),
        "workspace_id": workspace_id,
        "business_line": "sales",
        "create_binding": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["report"]["source_tool"] == "n8n"
    assert data["binding_id"]  # a binding was created
    assert data["workflow"]["category"] == "sales"


@pytest.mark.asyncio
async def test_import_unknown_format_returns_422(client: AsyncClient):
    headers = await _auth(client, "importer4")
    resp = await client.post("/api/v1/workflows/import", headers=headers, json={
        "content": '{"totally": "unrelated"}',
    })
    assert resp.status_code == 422


# ── Bindings + event triggers ──

@pytest.mark.asyncio
async def test_create_binding_rejects_opaque_workspace(client: AsyncClient):
    headers = await _auth(client, "binding_opaque_workspace")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Scoped binding", "steps": _simple_steps(["work"])},
    )).json()

    response = await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "workspace_id": "legacy-opaque-workspace",
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Workspace not found"


@pytest.mark.asyncio
async def test_event_trigger_starts_run_with_workspace_context(client: AsyncClient):
    headers = await _auth(client, "trig1")
    workspace_id = await _create_workspace(client, headers, "Sales workflow")
    # 1. create a workflow
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Lead pipeline", "steps": _simple_steps(["s1"]),
    })).json()

    # 2. deploy it into a workspace as an event-triggered binding
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": wf["id"],
        "workspace_id": workspace_id,
        "business_line": "sales",
        "trigger_type": "event",
        "trigger_config": {"event": "lead.created"},
    })).json()
    assert binding["workspace_id"] == workspace_id

    # 3. fire a matching event
    resp = await client.post("/api/v1/workflows/trigger", headers=headers, json={
        "trigger_type": "event",
        "event_name": "lead.created",
        "workspace_id": workspace_id,
        "trigger_data": {"lead_id": "L-9"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["started"] == 1
    run = data["runs"][0]
    assert run["workflow_id"] == wf["id"]
    assert run["status"] == "running"

    # 4. a non-matching event starts nothing
    none_resp = await client.post("/api/v1/workflows/trigger", headers=headers, json={
        "trigger_type": "event", "event_name": "other.event", "workspace_id": workspace_id,
    })
    assert none_resp.json()["started"] == 0


@pytest.mark.asyncio
async def test_list_bindings_filters_by_workspace(client: AsyncClient):
    headers = await _auth(client, "trig2")
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "WF", "steps": _simple_steps(["s1"]),
    })).json()
    workspace_a = await _create_workspace(client, headers, "Binding A")
    workspace_b = await _create_workspace(client, headers, "Binding B")
    for ws in (workspace_a, workspace_b):
        await client.post("/api/v1/workflows/bindings", headers=headers, json={
            "workflow_id": wf["id"], "workspace_id": ws,
        })
    only_a = (await client.get(
        "/api/v1/workflows/bindings",
        headers=headers,
        params={"workspace_id": workspace_a},
    )).json()
    assert len(only_a) == 1
    assert only_a[0]["workspace_id"] == workspace_a


@pytest.mark.asyncio
async def test_list_workflow_project_candidates_is_workspace_scoped_and_minimal(
    client: AsyncClient,
    db_session,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.services.workflow_project_service import create_workflow_project

    headers = await _auth(client, "workflowprojectcandidates")
    other_headers = await _auth(client, "workflowprojectcandidateother")
    workspace_id = await _create_workspace(client, headers, "Project candidates")
    other_workspace_id = await _create_workspace(client, headers, "Other candidates")
    owner = (await db_session.execute(
        select(User).where(User.email == "workflowprojectcandidates@test.com")
    )).scalar_one()

    ready = await create_workflow_project(
        db_session,
        entity_id=owner.entity_id,
        workspace_id=workspace_id,
        project_type="stickman_video",
        state={
            "publication": {"title": "Ready video"},
            "youtube": {},
            "private_runtime_value": "must-not-leak",
        },
        created_by=owner.id,
        current_stage="video_ready",
    )
    published = await create_workflow_project(
        db_session,
        entity_id=owner.entity_id,
        workspace_id=workspace_id,
        project_type="stickman_video",
        state={
            "publication": {"title": "Published video"},
            "youtube": {"video_id": "youtube-123"},
        },
        created_by=owner.id,
        current_stage="youtube_saved",
    )
    await create_workflow_project(
        db_session,
        entity_id=owner.entity_id,
        workspace_id=workspace_id,
        project_type="stickman_video",
        state={"publication": {"title": "Active upload"}},
        created_by=owner.id,
        current_stage="publishing",
    )
    # A project without publication data is still named by its own title.
    titled = await create_workflow_project(
        db_session,
        entity_id=owner.entity_id,
        workspace_id=workspace_id,
        project_type="stickman_video",
        state={"title": "Draft for review"},
        created_by=owner.id,
        current_stage="video_ready",
    )
    # A numbered project (a series episode) shows its code first.
    numbered = await create_workflow_project(
        db_session,
        entity_id=owner.entity_id,
        workspace_id=workspace_id,
        project_type="stickman_video",
        state={"title": "The City That Needed Everyone", "code": "E02"},
        created_by=owner.id,
        current_stage="video_ready",
    )
    await create_workflow_project(
        db_session,
        entity_id=owner.entity_id,
        workspace_id=other_workspace_id,
        project_type="stickman_video",
        state={"publication": {"title": "Wrong workspace"}},
        created_by=owner.id,
        current_stage="video_ready",
    )
    await create_workflow_project(
        db_session,
        entity_id=owner.entity_id,
        workspace_id=workspace_id,
        project_type="product_video",
        state={"publication": {"title": "Wrong type"}},
        created_by=owner.id,
        current_stage="video_ready",
    )
    await db_session.commit()

    response = await client.get(
        "/api/v1/workflows/projects/candidates",
        headers=headers,
        params=[
            ("workspace_id", workspace_id),
            ("project_type", "stickman_video"),
            ("stage", "video_ready"),
            ("stage", "youtube_saved"),
        ],
    )

    assert response.status_code == 200, response.text
    candidates = response.json()
    assert {item["id"] for item in candidates} == {ready.id, published.id, titled.id, numbered.id}
    assert {item["title"] for item in candidates} == {
        "Ready video",
        "Published video",
        "Draft for review",
        "E02 · The City That Needed Everyone",
    }
    published_candidate = next(
        item for item in candidates if item["id"] == published.id
    )
    assert published_candidate["youtube_video_id"] == "youtube-123"
    assert all("state" not in item for item in candidates)

    forbidden = await client.get(
        "/api/v1/workflows/projects/candidates",
        headers=other_headers,
        params={
            "workspace_id": workspace_id,
            "project_type": "stickman_video",
            "stage": "video_ready",
        },
    )
    assert forbidden.status_code == 404


@pytest.mark.asyncio
async def test_record_select_run_inputs_offer_and_enforce_workspace_ledger_records(
    client: AsyncClient, db_session, monkeypatch, tmp_path,
):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.services import relationship_ledger

    async def ledger_location(**kwargs):
        root = tmp_path / kwargs["entity_id"] / kwargs["workspace_id"] / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(storage_path="relationship-ledger", display_path="relationship-ledger"), str(root), str(tmp_path)

    async def sync_projection(**_kwargs):
        return SimpleNamespace(synced=True, document_id=None)

    monkeypatch.setattr(relationship_ledger, "ensure_ledger_location", ledger_location)
    monkeypatch.setattr(relationship_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)
    headers = await _auth(client, "workflowrecordselect")
    other_headers = await _auth(client, "workflowrecordselectother")
    workspace_id = await _create_workspace(client, headers, "Record select")
    owner = (await db_session.execute(
        select(User).where(User.email == "workflowrecordselect@test.com")
    )).scalar_one()
    configured = await client.put(
        f"/api/v1/workspaces/{workspace_id}/ledgers/configuration", headers=headers,
        json={"ledger_contracts": ["manor.relationship_ledger/v1"]},
    )
    assert configured.status_code == 200, configured.text

    async def record(identity_key, display_name, *, status="active", stage=None, subject_type="person", instagram=None):
        await relationship_ledger.record_relationship_event(
            entity_id=owner.entity_id, workspace_id=workspace_id, identity_key=identity_key,
            event="recorded", idempotency_key=f"{identity_key}:{status}", display_name=display_name,
            subject_type=subject_type, status=status, stage=stage,
            contact_points=[{"kind": "instagram", "value": instagram}] if instagram else None,
        )

    await record("creator:natgeo", "National Geographic", instagram="natgeo")
    await record("creator:nasa", "NASA", instagram="nasa")
    await record("creator:spacex", "SpaceX", status="paused", instagram="spacex")
    # An unrelated CRM-style pick from the same Ledger.
    await record("lead:acme", "Acme Corp", subject_type="lead", stage="proposal")
    await record("customer:globex", "Globex", subject_type="customer", stage="won")
    await record("lead:initech", "Initech", subject_type="lead", stage="lost")

    created = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Pick records",
        "workspace_id": workspace_id,
        "steps": [
            {"id": "start", "type": "trigger", "next": ["done"], "config": {"run_inputs": [
                {"key": "creators", "type": "json", "required": False, "schema": {
                    "type": "array", "items": {"type": "string"},
                    "x-ui": {"control": "record_select", "record_source": {
                        "ledger": "relationship_ledger",
                        "filter": {"contact_points.kind": "instagram", "status": {"$ne": "paused"}},
                    }},
                }},
                {"key": "account", "type": "string", "required": False, "schema": {
                    "type": "string",
                    "x-ui": {"control": "record_select", "record_source": {
                        "ledger": "relationship_ledger",
                        "filter": {"subject_type": {"$in": ["lead", "customer"]}, "stage": {"$ne": "lost"}},
                        "secondary_field": "stage",
                    }},
                }},
            ]}},
            {"id": "done", "type": "end", "config": {}, "next": []},
        ],
    })
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]

    async def choices(key, request_headers=headers):
        return await client.get(
            f"/api/v1/workflows/{workflow_id}/run-inputs/{key}/choices",
            headers=request_headers, params={"workspace_id": workspace_id},
        )

    creators = await choices("creators")
    assert creators.status_code == 200, creators.text
    assert {(item["value"], item["label"]) for item in creators.json()} == {
        ("creator:natgeo", "National Geographic"), ("creator:nasa", "NASA"),
    }
    accounts = (await choices("account")).json()
    assert {(item["value"], item.get("description")) for item in accounts} == {
        ("lead:acme", "proposal"), ("customer:globex", "won"),
    }
    assert (await choices("missing")).status_code == 404
    assert (await choices("creators", other_headers)).status_code == 404

    async def run(trigger_data):
        return await client.post(
            f"/api/v1/workflows/{workflow_id}/run", headers=headers,
            json={"trigger_data": trigger_data, "execute": False},
        )

    # A typed key passes like a picked one; an empty optional pick means "all matching".
    for trigger_data in ({"creators": ["creator:nasa"], "account": "lead:acme"}, {"creators": []}):
        accepted = await run(trigger_data)
        assert accepted.status_code == 201, accepted.text
    for trigger_data in ({"creators": ["creator:spacex"]}, {"account": "lead:initech"}):
        rejected = await run(trigger_data)
        assert rejected.status_code == 422, rejected.text
        assert "Not an available record" in rejected.text
    # The Workspace Run dialog starts runs through the binding, which checks too.
    binding = await client.post("/api/v1/workflows/bindings", headers=headers,
                                json={"workflow_id": workflow_id, "workspace_id": workspace_id})
    assert binding.status_code in {200, 201}, binding.text
    binding_rejected = await client.post(
        f"/api/v1/workflows/bindings/{binding.json()['id']}/run", headers=headers,
        json={"trigger_data": {"creators": ["creator:spacex"]}},
    )
    assert binding_rejected.status_code == 422, binding_rejected.text
    # The error names the valid keys, so a typed (mobile/API) value can be corrected.
    assert "lead:acme (Acme Corp)" in rejected.text and "customer:globex (Globex)" in rejected.text

    from packages.core.services import ledger_query_service, workflow_record_source

    async def slow_query(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(workflow_record_source, "RECORD_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(ledger_query_service, "query_installed_workspace_ledger", slow_query)
    unavailable = await choices("creators")
    assert unavailable.status_code == 503, unavailable.text
    assert unavailable.json()["detail"]["code"] == "record_source_unavailable"


@pytest.mark.asyncio
async def test_workflow_project_select_picks_several_workspace_records(client: AsyncClient, db_session):
    from sqlalchemy import select

    from packages.core.models.user import User
    from packages.core.services.workflow_project_service import create_workflow_project

    headers = await _auth(client, "workflowprojectmulti")
    workspace_id = await _create_workspace(client, headers, "Restaurant")
    other_workspace_id = await _create_workspace(client, headers, "Other restaurant")
    owner = (await db_session.execute(
        select(User).where(User.email == "workflowprojectmulti@test.com")
    )).scalar_one()

    async def project(name, *, project_type="dish", stage="listed", workspace=workspace_id):
        return (await create_workflow_project(
            db_session, entity_id=owner.entity_id, workspace_id=workspace, project_type=project_type,
            state={"name": name}, created_by=owner.id, current_stage=stage,
        )).id

    noodles, dumplings = await project("Noodles"), await project("Dumplings")
    retired = await project("Old soup", stage="retired")
    elsewhere = await project("Elsewhere", workspace=other_workspace_id)
    bundle = await project("Lunch bundle", project_type="bundle")
    await db_session.commit()

    # A picker without allowed_stages offers every stage, named by the record.
    every = await client.get("/api/v1/workflows/projects/candidates", headers=headers,
                             params={"workspace_id": workspace_id, "project_type": "dish"})
    assert every.status_code == 200, every.text
    assert {(item["id"], item["title"]) for item in every.json()} == {
        (noodles, "Noodles"), (dumplings, "Dumplings"), (retired, "Old soup")}

    dishes = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2,
              "x-ui": {"control": "workflow_project_select", "project_type": "dish"}}
    created = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Create promotion", "workspace_id": workspace_id,
        "steps": [
            {"id": "start", "type": "trigger", "next": ["review"], "config": {"run_inputs": [
                {"key": "dishes", "type": "json", "required": True, "schema": dishes},
            ]}},
            # An edit form opens with the current selection.
            {"id": "review", "type": "wait", "next": ["done"], "config": {
                "wait_type": "input", "message": "Adjust the dishes", "response_variable": "changes",
                "input_schema": {"type": "object", "required": ["featured"], "properties": {
                    "featured": {**dishes, "default": "{{dishes}}"}}},
            }},
            {"id": "done", "type": "end", "config": {}, "next": []},
        ],
    })
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]

    choices = await client.get(f"/api/v1/workflows/{workflow_id}/run-inputs/dishes/choices",
                               headers=headers, params={"workspace_id": workspace_id})
    assert choices.status_code == 200, choices.text
    assert {(item["value"], item["label"]) for item in choices.json()} == {
        (noodles, "Noodles"), (dumplings, "Dumplings"), (retired, "Old soup")}

    async def run(picked):
        return await client.post(f"/api/v1/workflows/{workflow_id}/run", headers=headers,
                                 json={"trigger_data": {"dishes": picked}})

    # An id of another Workspace or another type is refused like a single pick.
    for picked in ([noodles, elsewhere], [bundle]):
        rejected = await run(picked)
        assert rejected.status_code == 422, rejected.text
        assert "Not an existing dish" in rejected.text
    started = await run([noodles, dumplings])
    assert started.status_code == 201, started.text
    path = f"/api/v1/workflows/runs/{started.json()['id']}"
    paused = (await client.get(path, headers=headers)).json()
    assert paused["status"] == "paused"
    assert paused["step_results"]["review"]["input_schema"]["properties"]["featured"]["default"] == [noodles, dumplings]

    async def answer(picked):
        return await client.post(path + "/resume", headers=headers, json={
            "expected_step_id": "review", "variables": {"changes": {"featured": picked}}})

    assert (await answer([noodles, elsewhere])).status_code == 422
    assert (await answer([noodles, dumplings, retired])).status_code == 422  # maxItems
    resumed = await answer([retired])
    assert resumed.status_code == 200, resumed.text


@pytest.mark.asyncio
async def test_delete_workflow_rejects_active_binding_reference(client: AsyncClient):
    headers = await _auth(client, "wfdeletebindingref")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Still deployed", "steps": _simple_steps(["work"])},
    )).json()
    binding = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={"workflow_id": workflow["id"]},
    )).json()

    blocked = await client.delete(
        f"/api/v1/workflows/{workflow['id']}", headers=headers
    )

    assert blocked.status_code == 409, blocked.text
    assert binding["id"] in blocked.json()["detail"]["binding_ids"]
    assert (await client.get(
        f"/api/v1/workflows/{workflow['id']}", headers=headers
    )).status_code == 200
    assert (await client.get(
        "/api/v1/workflows/bindings",
        headers=headers,
        params={"workflow_id": workflow["id"]},
    )).json()[0]["id"] == binding["id"]


@pytest.mark.asyncio
async def test_delete_workflow_rejects_scheduled_job_reference(client: AsyncClient):
    headers = await _auth(client, "wfdeletescheduleref")
    workflow = (await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Still scheduled", "steps": _simple_steps(["work"])},
    )).json()
    scheduled = (await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": workflow["id"],
            "trigger_type": "schedule",
            "trigger_config": {"cron": "0 9 * * *"},
        },
    )).json()

    blocked = await client.delete(
        f"/api/v1/workflows/{workflow['id']}", headers=headers
    )

    assert blocked.status_code == 409, blocked.text
    assert scheduled["scheduled_job_id"] in blocked.json()["detail"]["scheduled_job_ids"]
    assert (await client.get(
        f"/api/v1/workflows/{workflow['id']}", headers=headers
    )).status_code == 200


@pytest.mark.asyncio
async def test_workspace_workflow_attachment_is_unique_referenced_and_lists_runs(
    client: AsyncClient,
    monkeypatch,
):
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    headers = await _auth(client, "workspacebinding")
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Workspace campaign",
        "steps": _simple_steps(["prepare"]),
    })).json()
    workspace_id = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Workspace campaign"},
    )).json()["id"]

    attached_response = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"],
        "workspace_id": workspace_id,
        "name": "Campaign workflow",
        "trigger_type": "manual",
        "variables": {"brand_voice": "clear"},
        "config": {"workspace_attached": True},
    })
    assert attached_response.status_code == 201
    attached = attached_response.json()

    duplicate = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"],
        "workspace_id": workspace_id,
        "trigger_type": "manual",
        "config": {"workspace_attached": True},
    })
    assert duplicate.status_code == 409

    automation = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"],
        "workspace_id": workspace_id,
        "name": "Run on approval",
        "trigger_type": "workspace_event",
        "trigger_config": {"event": "task.approval_decision"},
        "config": {"workspace_workflow_binding_id": attached["id"]},
    })).json()
    scheduled = (await client.post("/api/v1/jobs", headers=headers, json={
        "job_id": "workspace-campaign-daily",
        "name": "Daily campaign workflow",
        "job_type": "cron",
        "schedule_kind": "cron",
        "cron_expr": "0 9 * * *",
        "execution_type": "workflow",
        "workspace_id": workspace_id,
        "execution_target": {
            "workflow_id": workflow["id"],
            "workspace_id": workspace_id,
            "binding_id": attached["id"],
        },
    })).json()

    fired_response = await client.post("/api/v1/workflows/trigger", headers=headers, json={
        "trigger_type": "workspace_event",
        "event_name": "task.approval_decision",
        "workspace_id": workspace_id,
        "trigger_data": {"decision": "approved"},
    })
    assert fired_response.status_code == 200
    event_run = fired_response.json()["runs"][0]
    assert event_run["binding_id"] == attached["id"]
    assert event_run["variables"]["brand_voice"] == "clear"
    assert event_run["trigger_data"]["automation_binding_id"] == automation["id"]

    protected = await client.delete(
        f"/api/v1/workflows/bindings/{attached['id']}",
        headers=headers,
    )
    assert protected.status_code == 409
    assert "Remove automations" in protected.json()["detail"]

    run_response = await client.post(
        f"/api/v1/workflows/bindings/{attached['id']}/run",
        headers=headers,
        json={"execute": False, "trigger_data": {"source": "workspace"}},
    )
    assert run_response.status_code == 201
    run = run_response.json()
    assert run["workspace_id"] == workspace_id
    assert run["binding_id"] == attached["id"]
    assert queued == [event_run["id"], run["id"]]

    history = (await client.get(
        "/api/v1/workflows/runs",
        headers=headers,
        params={"workspace_id": workspace_id, "binding_id": attached["id"]},
    )).json()
    assert {item["id"] for item in history} == {event_run["id"], run["id"]}

    assert (await client.delete(
        f"/api/v1/workflows/bindings/{automation['id']}",
        headers=headers,
    )).status_code == 204
    still_protected = await client.delete(
        f"/api/v1/workflows/bindings/{attached['id']}",
        headers=headers,
    )
    assert still_protected.status_code == 409
    assert (await client.delete(
        f"/api/v1/jobs/{scheduled['id']}",
        headers=headers,
    )).status_code == 204
    assert (await client.delete(
        f"/api/v1/workflows/bindings/{attached['id']}",
        headers=headers,
    )).status_code == 204


@pytest.mark.asyncio
async def test_binding_history_can_include_only_unbound_direct_runs(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.workflow import WorkflowRun

    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *args, **kwargs: None),
    )
    headers = await _auth(client, "bindingunboundhistory")
    workspace_id = await _create_workspace(client, headers, "Campaign history")
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Campaign workflow", "workspace_id": workspace_id,
        "steps": _simple_steps(["prepare"]),
    })).json()
    target = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"], "workspace_id": workspace_id,
        "name": "Campaign action", "trigger_type": "manual",
    })).json()
    other = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"], "workspace_id": workspace_id,
        "name": "Other campaign action", "trigger_type": "workspace_event",
        "trigger_config": {"event": "campaign.changed"},
    })).json()
    target_run = (await client.post(
        f"/api/v1/workflows/bindings/{target['id']}/run", headers=headers, json={"execute": False},
    )).json()
    other_run = (await client.post(
        f"/api/v1/workflows/bindings/{other['id']}/run", headers=headers, json={"execute": False},
    )).json()
    direct_run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={"execute": False},
    )).json()
    historical_workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Historical campaign workflow", "workspace_id": workspace_id,
        "steps": _simple_steps(["prepare"]),
    })).json()
    db_session.add(WorkflowRun(
        id="scheduled-unbound-history", workflow_id=workflow["id"],
        entity_id=target_run["entity_id"], workspace_id=workspace_id,
        trigger_source="schedule", status="completed", current_step_id="end",
    ))
    db_session.add(WorkflowRun(
        id="old-bound-history", workflow_id=historical_workflow["id"],
        entity_id=target_run["entity_id"], workspace_id=workspace_id,
        binding_id=target["id"], trigger_source="manual", status="completed", current_step_id="end",
    ))
    await db_session.commit()

    default_history = (await client.get("/api/v1/workflows/runs", headers=headers, params={
        "workspace_id": workspace_id, "binding_id": target["id"],
    })).json()
    assert {row["id"] for row in default_history} == {target_run["id"], "old-bound-history"}

    history = (await client.get("/api/v1/workflows/runs", headers=headers, params={
        "workspace_id": workspace_id, "binding_id": target["id"],
        "workflow_id": workflow["id"], "include_unbound": "true",
    })).json()
    assert {row["id"] for row in history} == {target_run["id"], direct_run["id"]}
    assert other_run["id"] not in {row["id"] for row in history}
    assert "old-bound-history" not in {row["id"] for row in history}

    # Two App entries share this binding; each lists only the runs it started,
    # and the binding's own entry also keeps runs no entry claims.
    started = {}
    for key in ("action:howto", "action:tips"):
        started[key] = (await client.post(
            f"/api/v1/workflows/bindings/{target['id']}/run", headers=headers,
            json={"execute": False, "trigger_data": {"_workspace_app_action": key}},
        )).json()["id"]
    entry_history = {key: {row["id"] for row in (await client.get("/api/v1/workflows/runs", headers=headers, params={
        "workspace_id": workspace_id, "binding_id": target["id"], "workflow_id": workflow["id"],
        "include_unbound": "true", "app_action": key, **params,
    })).json()} for key, params in (("action:howto", {}), ("action:tips", {}),
                                    (f"workflow:{target['id']}", {"include_unlabelled": "true"}))}
    assert entry_history == {
        "action:howto": {started["action:howto"]},
        "action:tips": {started["action:tips"]},
        f"workflow:{target['id']}": {target_run["id"], direct_run["id"]},
    }

    invalid = await client.get("/api/v1/workflows/runs", headers=headers, params={"include_unbound": "true"})
    assert invalid.status_code == 422
    mismatched = await client.get("/api/v1/workflows/runs", headers=headers, params={
        "workspace_id": workspace_id, "binding_id": target["id"],
        "workflow_id": "another-workflow", "include_unbound": "true",
    })
    assert mismatched.status_code == 422


@pytest.mark.asyncio
async def test_binding_can_be_filtered_disabled_and_removed(client: AsyncClient):
    headers = await _auth(client, "bindingcrud")
    first = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "First", "steps": _simple_steps(["a"]),
    })).json()
    second = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Second", "steps": _simple_steps(["b"]),
    })).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": first["id"], "trigger_type": "event", "trigger_config": {"event": "x"},
    })).json()
    await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": second["id"], "trigger_type": "event", "trigger_config": {"event": "x"},
    })

    filtered = (await client.get(
        f"/api/v1/workflows/bindings?workflow_id={first['id']}",
        headers=headers,
    )).json()
    assert [item["id"] for item in filtered] == [binding["id"]]

    disabled = await client.put(
        f"/api/v1/workflows/bindings/{binding['id']}",
        headers=headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    fired = (await client.post("/api/v1/workflows/trigger", headers=headers, json={
        "trigger_type": "event", "event_name": "x",
    })).json()
    assert all(run["workflow_id"] != first["id"] for run in fired["runs"])

    removed = await client.delete(
        f"/api/v1/workflows/bindings/{binding['id']}",
        headers=headers,
    )
    assert removed.status_code == 204
    assert (await client.get(
        f"/api/v1/workflows/bindings?workflow_id={first['id']}",
        headers=headers,
    )).json() == []


@pytest.mark.asyncio
async def test_workspace_binding_can_change_workflow_and_run_manually(client: AsyncClient, monkeypatch):
    queued: list[str] = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *args, **kwargs: queued.append(run_id)),
    )
    headers = await _auth(client, "bindingrun")
    workspace_id = await _create_workspace(client, headers, "Binding run")
    first = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "First workspace flow", "steps": _simple_steps(["a"]),
    })).json()
    second = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Second workspace flow", "steps": _simple_steps(["b"]),
    })).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": first["id"],
        "workspace_id": workspace_id,
        "name": "Approval follow-up",
        "trigger_type": "workspace_event",
        "trigger_config": {"event": "task.approval_decision"},
    })).json()

    updated = await client.put(
        f"/api/v1/workflows/bindings/{binding['id']}",
        headers=headers,
        json={"workflow_id": second["id"], "trigger_config": {"event": "task.comment"}},
    )
    assert updated.status_code == 200
    assert updated.json()["workflow_id"] == second["id"]
    assert updated.json()["trigger_config"] == {"event": "task.comment"}

    started = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={"execute": False, "trigger_data": {"manual_test": True}},
    )
    assert started.status_code == 201
    run = started.json()
    assert run["workflow_id"] == second["id"]
    assert run["workspace_id"] == workspace_id
    assert run["binding_id"] == binding["id"]
    assert run["trigger_source"] == "manual"
    assert run["trigger_data"]["manual_test"] is True
    assert run["attempt_number"] == 1
    assert "attempt_number" not in run["trigger_data"]
    assert run["trigger_data"]["_workflow_definition_version"] == 1
    assert run["trigger_data"]["_workflow_definition_fingerprint"]
    assert run["definition_snapshot"]["fingerprint"] == run["trigger_data"]["_workflow_definition_fingerprint"]
    assert run["execution_trace"] == []
    assert run["status"] == "running"
    assert queued == [run["id"]]


@pytest.mark.asyncio
async def test_triggered_run_is_enqueued_for_execution(client: AsyncClient, monkeypatch):
    enqueued = []
    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda run_id, *a, **k: enqueued.append(run_id)),
    )
    headers = await _auth(client, "trig3")
    workspace_id = await _create_workspace(client, headers, "Triggered workflow")
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Auto", "steps": _simple_steps(["s1"]),
    })).json()
    await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": wf["id"], "workspace_id": workspace_id,
        "trigger_type": "event", "trigger_config": {"event": "ping"},
    })
    resp = await client.post("/api/v1/workflows/trigger", headers=headers, json={
        "trigger_type": "event", "event_name": "ping", "workspace_id": workspace_id,
    })
    run_id = resp.json()["runs"][0]["id"]
    assert enqueued == [run_id]  # triggered run was dispatched to the runner


@pytest.mark.asyncio
async def test_inbound_webhook_fires_binding(client: AsyncClient):
    headers = await _auth(client, "wh1")
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "WH flow", "steps": _simple_steps(["s1"]),
    })).json()
    workspace_id = (await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Webhook history"},
    )).json()["id"]
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": wf["id"], "workspace_id": workspace_id, "trigger_type": "webhook",
    })).json()
    token = binding["trigger_config"]["webhook_token"]
    assert token  # auto-generated

    # inbound POST is unauthenticated — the token is the secret
    resp = await client.post(f"/api/v1/workflows/webhook/{token}", json={"order_id": "O-1"})
    assert resp.status_code == 200
    assert resp.json()["started"] == 1

    # the run carries the webhook payload + workspace context
    run_id = resp.json()["run_ids"][0]
    run = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert run["workflow_id"] == wf["id"]
    assert run["trigger_data"]["order_id"] == "O-1"
    assert run["attempt_number"] == 1
    assert "attempt_number" not in run["trigger_data"]
    assert run["trigger_data"]["_workflow_definition_version"] == 1
    assert run["trigger_data"]["_workflow_definition_fingerprint"]

    # an unknown token fires nothing
    none = await client.post("/api/v1/workflows/webhook/bogus-token", json={})
    assert none.json()["started"] == 0


@pytest.mark.asyncio
async def test_run_executes_inline_by_default(client: AsyncClient):
    headers = await _auth(client, "inlinerun")
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Inline", "steps": _simple_steps(["a", "b", "c"]),
    })).json()
    # default /run (no execute flag) should run the whole workflow inline
    run = (await client.post(f"/api/v1/workflows/{wf['id']}/run", headers=headers, json={})).json()
    assert run["status"] == "completed"
    assert run["completed_at"] is not None
    assert {"a", "b", "c"} <= set(run["step_results"].keys())


@pytest.mark.asyncio
async def test_run_single_node_standalone(client: AsyncClient):
    """A node can be executed in isolation without a persisted workflow run."""
    headers = await _auth(client, "wfnode")
    # transform node exercises real handler logic and is network-free
    resp = await client.post(
        "/api/v1/workflows/run-node",
        headers=headers,
        json={
            "step": {
                "id": "n1",
                "type": "transform",
                "name": "Set",
                "config": {
                    "inputs": [{"key": "person", "value": "{{who}}", "type": "text"}],
                    "set": {"greeting": "hello {{person}}"},
                },
            },
            "variables": {"who": "Sam"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output"]["greeting"] == "hello Sam"   # templated against variables
    assert body["step_id"] == "n1"
    assert body["inputs"] == {"who": "Sam", "person": "Sam"}
    assert body["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_run_single_node_passthrough(client: AsyncClient):
    """Entry/terminal markers run standalone and complete."""
    headers = await _auth(client, "wfnode2")
    resp = await client.post(
        "/api/v1/workflows/run-node",
        headers=headers,
        json={"step": {"id": "e", "type": "end", "name": "Done"}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_schedule_trigger_creates_automation(client: AsyncClient):
    """A schedule trigger is an automation, not a binding: it returns a
    ScheduledJob (exec_type=workflow) the scheduler fires on its cron."""
    headers = await _auth(client, "wfsched")
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Cron flow", "steps": _simple_steps(["a", "b"]),
    })).json()

    resp = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": wf["id"],
        "name": "Cron flow daily",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 9 * * *"},
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "automation"        # not a binding
    assert body["scheduled_job_id"]
    assert body["cron"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_schedule_trigger_requires_cron(client: AsyncClient):
    headers = await _auth(client, "wfsched2")
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "No cron", "steps": _simple_steps(["a"]),
    })).json()
    resp = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": wf["id"], "trigger_type": "schedule", "trigger_config": {},
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_media_model_override_runs_end_to_end(client: AsyncClient, monkeypatch):
    """End-to-end through the real run() graph traversal: a trigger → image → end
    workflow where the image node carries a per-node model override (the catalog
    picker) runs inline to completion and forwards the override into generate_file."""
    from packages.core.ai.workflow_runner import WorkflowRunner

    captured: dict = {}

    async def fake_tool(self, step, variables, entity_id, user_id, runtime_context):
        captured["args"] = step["config"]["args"]
        return {"status": "completed", "output": {"image_url": "kb://out.png"}}

    monkeypatch.setattr(WorkflowRunner, "_execute_tool_step", fake_tool)

    headers = await _auth(client, "wfmedia")
    steps = [
        {"id": "t", "type": "trigger", "name": "Start", "config": {}, "next": ["img"]},
        {"id": "img", "type": "image", "name": "Render", "next": ["end"], "config": {
            "prompt": "a fox in {{season}}", "model": "openai/gpt-image-1", "size": "1536x1024",
        }},
        {"id": "end", "type": "end", "name": "End", "config": {}, "next": []},
    ]
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Media pipeline", "steps": steps,
    })).json()

    # execute=True drives the real WorkflowRunner().run() inline to completion.
    run = (await client.post(f"/api/v1/workflows/{wf['id']}/run", headers=headers, json={
        "variables": {"season": "autumn"}, "execute": True,
    })).json()

    final = (await client.get(f"/api/v1/workflows/runs/{run['id']}", headers=headers)).json()
    assert final["status"] == "completed", final
    assert "img" in final["step_results"]
    # The per-node override + controls reached generate_file, prompt templated.
    args = captured["args"]
    assert args["kind"] == "image"
    assert args["model"] == "openai/gpt-image-1"
    assert args["size"] == "1536x1024"
    assert args["prompt"] == "a fox in autumn"


@pytest.mark.asyncio
async def test_run_stream_emits_per_node_status_and_done(client: AsyncClient):
    """The canvas's Run path: POST /run-stream streams a ``run`` frame, a ``node``
    frame per step as it lights up, then a terminal ``done`` frame carrying the
    full run (status + step_results). This is the path behind the user-facing
    'see each node execute' feature, previously untested."""
    import json

    headers = await _auth(client, "wfstream")
    steps = _simple_steps(["a", "b", "c"])  # linear transform chain, no creds needed
    wf = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Stream pipeline", "steps": steps,
    })).json()

    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", f"/api/v1/workflows/{wf['id']}/run-stream",
        headers=headers, json={"variables": {}},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        event = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event:
                events.append((event, json.loads(line[5:].strip())))

    kinds = [e for e, _ in events]
    assert "run" in kinds              # opening frame with run_id
    assert kinds.count("node") >= 3    # one per step lighting up
    assert "error" not in kinds
    assert kinds[-1] == "done"         # terminal frame last
    done = dict(events[-1][1])
    assert done["status"] == "completed"
    for sid in ("a", "b", "c"):
        assert sid in done["step_results"]


@pytest.mark.asyncio
async def test_workflow_continuation_sweep_claims_persisted_intent(
    db_session,
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import packages.core.database as database
    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun
    from packages.core.tasks.ai_tasks import _claim_due_workflow_continuations

    now = datetime.now(timezone.utc)
    token = generate_ulid()
    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=generate_ulid(),
        entity_id=generate_ulid(),
        status="paused",
        continuation_token=token,
        continuation_due_at=now - timedelta(seconds=1),
        continuation_next_attempt_at=now - timedelta(seconds=1),
    )
    db_session.add(run)
    await db_session.commit()
    run_id = run.id
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: sessions)

    # A live execution owner is already continuing the run: republishing it
    # would only add a heavy-queue delivery that ends as claim_held.
    from packages.core.models.execution_claim import RuntimeExecutionClaim

    owner = RuntimeExecutionClaim(
        claim_key=f"workflow-run:{run_id}",
        claim_token=generate_ulid(),
        expires_at=now + timedelta(minutes=5),
    )
    db_session.add(owner)
    await db_session.commit()
    assert await _claim_due_workflow_continuations(limit=10) == []
    owner.expires_at = now - timedelta(seconds=1)
    await db_session.commit()

    before_claim = datetime.now(timezone.utc)
    claimed = await _claim_due_workflow_continuations(limit=10)

    assert claimed == [(run_id, token)]
    db_session.expire_all()
    persisted = await db_session.get(WorkflowRun, run_id)
    assert persisted.continuation_token == token
    assert persisted.continuation_next_attempt_at > before_claim

    # A continuation that has waited a long time is still queued, not lost;
    # the sweep backs off instead of republishing it every minute.
    persisted.continuation_due_at = now - timedelta(hours=1)
    persisted.continuation_next_attempt_at = now - timedelta(seconds=1)
    await db_session.commit()
    assert await _claim_due_workflow_continuations(limit=10) == [(run_id, token)]
    db_session.expire_all()
    persisted = await db_session.get(WorkflowRun, run_id)
    assert persisted.continuation_next_attempt_at >= now + timedelta(seconds=590)

    # A worker may crash after committing the business terminal state but
    # before clearing the token. The same sweep must still deliver cleanup.
    persisted.status = "completed"
    persisted.continuation_next_attempt_at = now - timedelta(seconds=1)
    await db_session.commit()
    assert await _claim_due_workflow_continuations(limit=10) == [(run_id, token)]

    from packages.core.ai.workflow_runner import WorkflowRunner

    outcome = await WorkflowRunner.resume(
        run_id,
        continuation_token=token,
        session_factory=sessions,
    )
    assert outcome == "not_paused"
    db_session.expire_all()
    cleaned = await db_session.get(WorkflowRun, run_id)
    assert cleaned.continuation_token is None


@pytest.mark.asyncio
async def test_workflow_terminal_effect_sweep_claims_unfinished_effects(
    db_session,
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import packages.core.database as database
    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun
    from packages.core.tasks.ai_tasks import _claim_due_workflow_terminal_effects

    now = datetime.now(timezone.utc)
    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=generate_ulid(),
        entity_id=generate_ulid(),
        status="failed",
        error="boom",
        terminal_effects_next_attempt_at=now - timedelta(seconds=1),
    )
    db_session.add(run)
    await db_session.commit()
    run_id = run.id
    sessions = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(database, "create_worker_session", lambda: sessions)

    claimed = await _claim_due_workflow_terminal_effects(limit=10)

    assert run_id in claimed
    db_session.expire_all()
    persisted = await db_session.get(WorkflowRun, run_id)
    assert persisted.terminal_effects_completed_at is None
    assert persisted.terminal_effects_next_attempt_at > now

    persisted.terminal_effects_completed_at = datetime.now(timezone.utc)
    persisted.terminal_effects_next_attempt_at = now - timedelta(seconds=1)
    await db_session.commit()
    assert run_id not in await _claim_due_workflow_terminal_effects(limit=10)


def test_workflow_terminal_effect_sweep_republishes_to_runner(monkeypatch):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.tasks import ai_tasks

    published: list[str] = []

    def run_async(awaitable):
        awaitable.close()
        return ["run-1", "run-2"]

    monkeypatch.setattr(ai_tasks, "_run_async", run_async)
    monkeypatch.setattr(
        WorkflowRunner,
        "enqueue",
        staticmethod(lambda run_id: published.append(run_id) or run_id == "run-1"),
    )

    result = ai_tasks.workflow_terminal_effect_sweep.run()

    assert result == {"claimed": 2, "published": 1}
    assert published == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_run_single_node_is_a_real_running_run_that_tools_accept(client: AsyncClient, monkeypatch):
    """Tools a node calls check that their run exists, is running, and belongs
    to the caller; a node test must pass that check (an in-memory stand-in
    run failed it with workflow_origin_not_executable) and stay out of lists."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.governance.service import workflow_run_is_executable

    seen: dict = {}

    async def fake_step(self, step, run, db):
        seen["run_id"] = run.id
        seen["executable"] = await workflow_run_is_executable(
            db,
            entity_id=run.entity_id,
            workspace_id=None,
            user_id=run.started_by,
            workflow_run_id=run.id,
        )
        return {"status": "completed", "output": {"image_url": "/api/v1/fs/e/test.png"}}

    monkeypatch.setattr(WorkflowRunner, "_execute_step_safe", fake_step)
    headers = await _auth(client, "wfnodetool")
    resp = await client.post(
        "/api/v1/workflows/run-node",
        headers=headers,
        json={"step": {"id": "img", "type": "image", "name": "Image", "config": {"prompt": "a cat"}}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert seen["executable"] is True
    assert seen["run_id"] != "adhoc"

    runs = (await client.get("/api/v1/workflows/runs", headers=headers)).json()
    rows = runs if isinstance(runs, list) else runs.get("runs") or runs.get("items") or []
    assert all(row.get("id") != seen["run_id"] for row in rows)


@pytest.mark.asyncio
async def test_a_run_its_own_flow_cannot_advance_offers_no_retry(client, db_session):
    """Without a Flow binding to start over from, a stalled run offers no Retry."""
    from packages.core.models.workflow import WorkflowRun

    headers = await _auth(client, "wfstalledretry")
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers,
        json={"name": "Stalled", "steps": _simple_steps(["work"])},
    )).json()
    run = (await client.post(
        f"/api/v1/workflows/{workflow['id']}/run", headers=headers, json={"execute": False},
    )).json()
    stalled = await db_session.get(WorkflowRun, run["id"])
    stalled.status = "failed"
    stalled.current_step_id = "work"
    stalled.error = ("No runnable steps and workflow not complete (current step: work). "
                     "Steps: trigger [trigger]: completed; work [agent]: not run")
    await db_session.commit()

    summary = (await client.get(f"/api/v1/workflows/runs/{run['id']}?detail=false", headers=headers)).json()
    assert summary["intervention"] is None
    refused = await client.post(f"/api/v1/workflows/runs/{run['id']}/retry", headers=headers, json={})
    assert refused.status_code == 409, refused.text
    # Any other failure is still retryable from the run page.
    stalled.error = "Provider timed out"
    await db_session.commit()
    summary = (await client.get(f"/api/v1/workflows/runs/{run['id']}?detail=false", headers=headers)).json()
    assert summary["intervention"]["kind"] == "workflow_retry"
