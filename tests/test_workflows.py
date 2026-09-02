"""E2E tests: workflow definitions, runs, and step execution."""
import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from types import SimpleNamespace


async def _auth(client: AsyncClient, username: str = "wfuser") -> dict:
    resp = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com",
        "password": "pass123", "entity_name": "Workflow Corp",
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
async def test_start_workflow_requires_a_real_active_workspace(
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

    with pytest.raises(ValueError, match="active Workspace"):
        await start_workflow(
            db_session,
            entity_id,
            workflow.json()["id"],
            workspace_id=workspace.json()["id"],
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
        "business_outcome": "needs_input",
        "processed_count": 1,
        "total_count": 1,
        "artifact_count": 64,
        "blocker": "Browser disconnected",
    }

    run.execution_trace = [{} for _ in range(2000)]
    append_execution_trace(run, node=node, status="failed", result={"error": "late"})
    assert len(run.execution_trace) == 2000


def test_workflow_history_summary_migration_backfills_existing_runs():
    from importlib import import_module

    migration = import_module(
        "packages.core.migrations.versions.20260731_13_workflow_history_summary"
    )
    summary = migration._history_summary({
        "definition_snapshot": {
            "nodes": [
                {"id": "start", "type": "trigger", "chat_projection": "progress"},
                {"id": "persist", "type": "transform", "chat_projection": "hidden"},
                {"id": "capture", "type": "agent", "chat_projection": "progress"},
                {"id": "end", "type": "end", "chat_projection": "progress"},
            ],
        },
        "step_results": {
            "start": {"status": "completed"},
            "persist": {"status": "completed"},
            "capture": {
                "status": "failed",
                "output": {"artifacts": [{"document_id": "nested-shot"}]},
            },
        },
        "variables": {
            "project": {
                "state": {
                    "business_outcome": "needs_input",
                    "retry_state": {
                        "observed_problem": {
                            "message": "Browser disconnected" + ("!" * 12_000),
                            "api_key": "migration-secret",
                        },
                    },
                },
            },
        },
        "execution_trace": [
            {"artifact_refs": [{"document_id": "video-1"}]},
            {"artifact_refs": [{"document_id": "video-1"}, {"document_id": "shot-1"}]},
        ],
        "error": None,
    })

    assert summary["business_outcome"] == "needs_input"
    assert summary["processed_count"] == 2
    assert summary["total_count"] == 2
    assert summary["artifact_count"] == 3
    assert summary["blocker"]["truncated"] is True
    assert len(json.dumps(summary["blocker"]).encode("utf-8")) <= 8 * 1024
    assert "migration-secret" not in json.dumps(summary["blocker"])


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


def test_workflow_history_summary_migration_overwrites_existing_summary(monkeypatch):
    from importlib import import_module

    migration = import_module(
        "packages.core.migrations.versions.20260731_13_workflow_history_summary"
    )
    row = {
        "id": "run-forged-summary",
        "definition_snapshot": {"nodes": [{"id": "start", "type": "trigger"}]},
        "step_results": {},
        "variables": {},
        "execution_trace": [],
        "error": None,
        "trigger_data": {
            "_workflow_history_summary": {
                "processed_count": 999,
                "total_count": 1,
            },
        },
    }

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class Bind:
        def __init__(self):
            self.selected = False
            self.updates = []

        def execute(self, statement, parameters):
            sql = str(statement)
            if "SELECT" in sql:
                if self.selected or "? :summary_key" in sql:
                    return Result([])
                self.selected = True
                return Result([row])
            self.updates.extend(parameters)
            return Result([])

    bind = Bind()
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    migration.upgrade()

    assert len(bind.updates) == 1
    summary = json.loads(bind.updates[0]["summary"])
    assert summary["processed_count"] == 0
    assert summary["total_count"] == 1


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
    assert b"[REDACTED]" in encoded_error
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
async def test_runner_fails_closed_when_execution_claim_heartbeat_is_lost(
    client: AsyncClient,
    monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
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

    assert await WorkflowRunner().run(started["id"]) == "executed"

    detail = (await client.get(
        f"/api/v1/workflows/runs/{started['id']}",
        headers=headers,
    )).json()
    assert detail["status"] == "failed"
    assert detail["error"] == "Workflow execution claim was lost"
    assert len(released) == 2


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
        assert await WorkflowRunner._fail_lost_execution_claim(started["id"]) is None

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
        "_fail_lost_execution_claim",
        staticmethod(successor_owns_recovery),
    )

    assert await WorkflowRunner().run("run-1") == WORKFLOW_RUN_EXECUTION_CLAIM_HELD


@pytest.mark.asyncio
async def test_execution_claim_tolerates_one_transient_postgres_renewal_outage(
    monkeypatch,
):
    from packages.core.services import workflow_run_execution_claim as claim_service

    renewal_succeeded = asyncio.Event()
    released: list[tuple[str, str]] = []
    renewal_results = iter([None, True])

    async def acquire(*_args, **_kwargs):
        return True

    async def extend(*_args, **_kwargs):
        result = next(renewal_results)
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

    async with claim_service.workflow_run_execution_claim(
        "workflow-transient-renewal"
    ) as claim:
        await asyncio.wait_for(renewal_succeeded.wait(), timeout=1)
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

    async def fake_ai_edit(prompt, current_steps, generated_entity_id):
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
    await asyncio.wait_for(work_started.wait(), timeout=2)
    try:
        manual = await client.post(
            f"/api/v1/workflows/runs/{run['id']}/step",
            headers=headers,
        )
    finally:
        finish_work.set()
        await asyncio.wait_for(full_run, timeout=2)

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
async def test_workflow_status_settlement_survives_chat_projection_failure(
    monkeypatch,
):
    from packages.core.services import proposal_workflow_runs
    from packages.core.services import scheduler_service
    from packages.core.services import workflow_chat_projection

    events = []

    class NestedTransaction:
        async def __aenter__(self):
            events.append("chat_savepoint")

        async def __aexit__(self, exc_type, *_args):
            events.append(f"chat_rollback:{exc_type.__name__}")
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

    workflow_run = SimpleNamespace(id="run-1")
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
        "chat_projection",
        "chat_rollback:RuntimeError",
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
    assert b"[REDACTED]" in encoded_error
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
            "business_outcome": "needs_input",
            "processed_count": 2,
            "total_count": 3,
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
    assert listed["artifact_count"] == 4
    assert listed["history_blocker"] == "Browser disconnected"
    for heavy_key in ("variables", "step_results", "definition_snapshot", "execution_trace"):
        assert heavy_key not in listed


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
    }
    assert "step_results" not in summary
    assert "execution_trace" not in summary


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
    stored.step_results = {
        "collect": {
            "status": "paused",
            "step_id": "collect",
            "subrun_ids": ["child-a", "child-b"],
        },
    }
    await db_session.commit()

    response = await client.get(
        f"/api/v1/workflows/runs/{run['id']}?detail=false",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["intervention"] is None


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
async def test_manual_pause_resumes_without_consuming_current_node_as_approval(
    client: AsyncClient,
):
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

    resumed_response = await client.post(
        f"/api/v1/workflows/runs/{run['id']}/resume",
        headers=headers,
        json={},
    )
    assert resumed_response.status_code == 200, resumed_response.text
    resumed = resumed_response.json()
    assert resumed["status"] == "completed"
    assert resumed["step_results"]["p1"]["status"] == "completed"
    assert resumed["step_results"]["p2"]["status"] == "completed"
    assert resumed["trigger_data"]["_workflow_run_control"]["state"] == "resumed"


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
    assert resumed["status"] == "completed"
    assert resumed["step_results"]["later"]["status"] == "completed"


@pytest.mark.asyncio
async def test_resume_continues_paused_run_to_completion(client: AsyncClient):
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

    missing_decision = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"variables": {}},
    )
    assert missing_decision.status_code == 400

    response = await client.post(
        f"/api/v1/workflows/runs/{paused['id']}/resume",
        headers=headers,
        json={"variables": {"decision": "approved"}},
    )
    assert response.status_code == 200
    completed = response.json()
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
async def test_resume_continues_from_an_internal_stage_approval(client: AsyncClient):
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
    assert response.json()["status"] == "completed"
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
    assert completed["status"] == "completed"
    assert completed["variables"]["plan_decision"] == {"choice": "approve"}
    assert completed["variables"]["branch"] == "approved"
    assert completed["step_results"]["approval"]["decision"] == "approve"
    assert completed["step_results"]["approval"]["approved"] is True


@pytest.mark.asyncio
async def test_resumed_subworkflow_automatically_continues_parent(client: AsyncClient):
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
    assert child_run["trigger_data"]["_workflow_node_names"] == {
        "start": "start",
        "approval": "approval",
        "child_finish": "child_finish",
        "end": "end",
    }
    assert child_run["workflow_name"] == "Child approval"
    assert child_run["current_step_name"] == "approval"

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
    assert child_resumed.json()["status"] == "completed"

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
    assert resumed.json()["status"] == "failed"

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
    assert failed["current_step_name"] == "unstable"
    original_error = failed["error"]
    original_prepare = failed["step_results"]["prepare"]

    response = await client.post(
        f"/api/v1/workflows/runs/{failed['id']}/retry",
        headers=headers,
        json={
            "from_step_id": "unstable",
            "variables": {"corrected_input": "ready"},
            "execute": True,
        },
    )

    assert response.status_code == 201, response.text
    retried = response.json()
    assert retried["id"] != failed["id"]
    assert retried["status"] == "completed"
    assert retried["retry_of_run_id"] == failed["id"]
    assert retried["retry_from_step_id"] == "unstable"
    assert retried["attempt_number"] == 2
    assert retried["lineage_status"] == "canonical"
    assert retried["workflow_name"] == failed["workflow_name"]
    assert retried["current_step_name"] == "end"
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
async def test_retry_uses_prior_execution_snapshot_when_live_prompt_changes(
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
        json={"from_step_id": "work", "execute": False},
    )

    assert retried.status_code == 201, retried.text
    retry = retried.json()
    assert retry["retry_of_run_id"] == failed["id"]
    assert retry["definition_snapshot"] == failed["definition_snapshot"]
    assert "execution_snapshot" not in retry

    retry_row = await db_session.get(WorkflowRun, retry["id"])
    assert retry_row.execution_snapshot == original_execution_snapshot


@pytest.mark.asyncio
async def test_retry_executes_original_execution_snapshot_not_live_definition(
    client: AsyncClient,
    db_session,
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
        json={"from_step_id": "work", "execute": True},
    )

    assert retried.status_code == 201, retried.text
    assert retried.json()["status"] == "completed"
    assert retried.json()["variables"]["result"] == "original"


@pytest.mark.asyncio
async def test_retry_uses_prior_execution_snapshot_when_live_graph_changes(
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
        json={"from_step_id": "work", "execute": False},
    )

    assert retried.status_code == 201, retried.text
    retry = retried.json()
    assert retry["definition_snapshot"] == failed["definition_snapshot"]
    retry_row = await db_session.get(WorkflowRun, retry["id"])
    assert retry_row.execution_snapshot == original_execution_snapshot


@pytest.mark.asyncio
async def test_retry_rejects_legacy_run_when_definition_changed(
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
        json={"from_step_id": "work", "execute": False},
    )

    assert retried.status_code == 409
    assert "legacy run has no immutable execution snapshot" in retried.text.lower()


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
        json={"from_step_id": "start", "execute": True},
    )

    assert response.status_code == 201, response.text
    retried = response.json()
    assert retried["status"] == "completed"
    assert retried["retry_of_run_id"] == pending["id"]
    assert retried["retry_from_step_id"] == "start"
    assert retried["attempt_number"] == 2
    assert retried["variables"]["recovered"] is True


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
            "execute": True,
        },
    )

    assert response.status_code == 201, response.text
    retried = response.json()
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
    prior = (await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run",
        headers=headers,
        json={},
    )).json()

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
            "execute": True,
        },
    )

    assert response.status_code == 201, response.text
    retried = response.json()
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
            "start": "start",
            "copy": "copy",
            "end": "end",
        }
        for child_run in child_runs
    )
    assert all(child_run["workflow_name"] == "Capture one scene" for child_run in child_runs)
    assert all(child_run["current_step_name"] == "end" for child_run in child_runs)


@pytest.mark.asyncio
async def test_foreach_subworkflow_resumes_each_paused_child_before_parent(client: AsyncClient):
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
async def test_foreach_subworkflow_rejects_parent_definition_change_before_child_merge(
    client: AsyncClient,
    db_session,
):
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

    before_claim = datetime.now(timezone.utc)
    claimed = await _claim_due_workflow_continuations(limit=10)

    assert claimed == [(run_id, token)]
    db_session.expire_all()
    persisted = await db_session.get(WorkflowRun, run_id)
    assert persisted.continuation_token == token
    assert persisted.continuation_next_attempt_at > before_claim

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
