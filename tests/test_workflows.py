"""E2E tests: workflow definitions, runs, and step execution."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "wfuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Workflow Corp",
        },
    )
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


def _simple_steps(step_ids: list[str]) -> list[dict]:
    """Build a linear chain of transform steps for testing."""
    steps = []
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
            step["next"] = []
        steps.append(step)
    return steps


@pytest.mark.asyncio
async def test_create_workflow(client: AsyncClient):
    headers = await _auth(client)
    steps = _simple_steps(["s1", "s2"])
    resp = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "My Pipeline",
            "steps": steps,
            "description": "A test workflow",
            "category": "ops",
            "tags": ["test"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Pipeline"
    assert data["description"] == "A test workflow"
    assert len(data["steps"]) == 2
    assert data["status"] == "active"
    assert data["is_active"] is True
    assert data["category"] == "ops"
    assert data["tags"] == ["test"]

    # Verify we can GET it back
    wf_id = data["id"]
    get_resp = await client.get(f"/api/v1/workflows/{wf_id}", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["name"] == "My Pipeline"


@pytest.mark.asyncio
async def test_start_and_execute(client: AsyncClient):
    headers = await _auth(client, "wfuser2")
    steps = _simple_steps(["a", "b"])
    wf = (
        await client.post(
            "/api/v1/workflows",
            headers=headers,
            json={
                "name": "Two-step",
                "steps": steps,
            },
        )
    ).json()
    wf_id = wf["id"]

    # Start a run
    run_resp = await client.post(
        f"/api/v1/workflows/{wf_id}/run",
        headers=headers,
        json={
            "variables": {"input": "hello"},
        },
    )
    assert run_resp.status_code == 201
    run = run_resp.json()
    assert run["status"] == "running"
    assert run["current_step_id"] == "a"
    run_id = run["id"]

    # Execute step a (transform)
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

    # Run should now be completed
    final = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert final["status"] == "completed"
    assert final["completed_at"] is not None
    # Both steps recorded in step_results
    assert "a" in final["step_results"]
    assert "b" in final["step_results"]


@pytest.mark.asyncio
async def test_condition_step(client: AsyncClient):
    headers = await _auth(client, "wfuser3")
    steps = [
        {
            "id": "check",
            "type": "condition",
            "name": "Score check",
            "config": {"expression": "score > 0.7"},
            "true_next": ["pass"],
            "false_next": ["fail"],
        },
        {"id": "pass", "type": "transform", "name": "Pass", "config": {"set": {"result": "passed"}}, "next": []},
        {"id": "fail", "type": "transform", "name": "Fail", "config": {"set": {"result": "failed"}}, "next": []},
    ]
    wf = (
        await client.post(
            "/api/v1/workflows",
            headers=headers,
            json={
                "name": "Condition test",
                "steps": steps,
            },
        )
    ).json()

    # Run with score=0.9 -> should take true_next -> "pass"
    run1 = (
        await client.post(
            f"/api/v1/workflows/{wf['id']}/run",
            headers=headers,
            json={
                "variables": {"score": 0.9},
            },
        )
    ).json()
    step_result = (await client.post(f"/api/v1/workflows/runs/{run1['id']}/step", headers=headers)).json()
    assert step_result["output"] is True
    run1_state = (await client.get(f"/api/v1/workflows/runs/{run1['id']}", headers=headers)).json()
    assert run1_state["current_step_id"] == "pass"

    # Run with score=0.3 -> should take false_next -> "fail"
    run2 = (
        await client.post(
            f"/api/v1/workflows/{wf['id']}/run",
            headers=headers,
            json={
                "variables": {"score": 0.3},
            },
        )
    ).json()
    step_result2 = (await client.post(f"/api/v1/workflows/runs/{run2['id']}/step", headers=headers)).json()
    assert step_result2["output"] is False
    run2_state = (await client.get(f"/api/v1/workflows/runs/{run2['id']}", headers=headers)).json()
    assert run2_state["current_step_id"] == "fail"


@pytest.mark.asyncio
async def test_list_runs(client: AsyncClient):
    headers = await _auth(client, "wfuser4")
    steps = _simple_steps(["x"])
    wf = (
        await client.post(
            "/api/v1/workflows",
            headers=headers,
            json={
                "name": "Multi-run",
                "steps": steps,
            },
        )
    ).json()
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


@pytest.mark.asyncio
async def test_cancel_run(client: AsyncClient):
    headers = await _auth(client, "wfuser5")
    steps = _simple_steps(["c1", "c2"])
    wf = (
        await client.post(
            "/api/v1/workflows",
            headers=headers,
            json={
                "name": "Cancel test",
                "steps": steps,
            },
        )
    ).json()

    run = (await client.post(f"/api/v1/workflows/{wf['id']}/run", headers=headers, json={})).json()
    assert run["status"] == "running"

    # Cancel it
    cancel_resp = await client.post(f"/api/v1/workflows/runs/{run['id']}/cancel", headers=headers)
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "cancelled"

    # Trying to execute a step on cancelled run should fail
    step_resp = await client.post(f"/api/v1/workflows/runs/{run['id']}/step", headers=headers)
    data = step_resp.json()
    assert data.get("error") == "Run not active"

    # Cancelling again should return 400
    cancel2 = await client.post(f"/api/v1/workflows/runs/{run['id']}/cancel", headers=headers)
    assert cancel2.status_code == 400
