"""Declared entry-node inputs are enforced on direct and scheduled starts."""
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from packages.core.services.workspace_workflow_router import (
    WorkflowRunInputError,
    check_declared_workflow_run_inputs,
    required_workflow_run_inputs_without_default,
)

BRIEF_SCHEMA = {
    "type": "object",
    "required": ["topic_id", "thesis"],
    "properties": {
        "topic_id": {"type": "string", "minLength": 1},
        "thesis": {"type": "string", "minLength": 1},
    },
}


def _steps(run_inputs: list[dict]) -> list[dict]:
    return [
        {
            "id": "start",
            "type": "trigger",
            "name": "Start",
            "config": {"run_inputs": run_inputs},
            "next": ["end"],
        },
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]


BRIEF_INPUTS = [
    {"key": "topic_brief", "label": "Approved Topic Brief", "type": "json",
     "required": True, "schema": BRIEF_SCHEMA},
    {"key": "target_length", "label": "Target length", "type": "string",
     "required": True, "default": "1200 words"},
    {"key": "run_context", "type": "json", "hidden": True, "required": True},
    {"key": "note", "type": "string", "required": False},
]


def _workflow(run_inputs: list[dict], variables: dict | None = None):
    return SimpleNamespace(steps=_steps(run_inputs), variables=variables or {})


def test_missing_required_input_is_rejected_with_its_label():
    with pytest.raises(WorkflowRunInputError) as exc:
        check_declared_workflow_run_inputs(
            _workflow(BRIEF_INPUTS), variables=None, trigger_data={},
        )
    assert exc.value.errors == {"topic_brief": "This input is required."}
    assert "Approved Topic Brief" in str(exc.value)


def test_schema_violations_name_the_missing_field():
    with pytest.raises(WorkflowRunInputError) as exc:
        check_declared_workflow_run_inputs(
            _workflow(BRIEF_INPUTS),
            variables=None,
            trigger_data={"topic_brief": {"topic_id": "t1"}},
        )
    assert list(exc.value.errors) == ["topic_brief.thesis"]
    assert "Approved Topic Brief (thesis)" in str(exc.value)


def test_each_missing_schema_field_keeps_its_own_message():
    with pytest.raises(WorkflowRunInputError) as exc:
        check_declared_workflow_run_inputs(
            _workflow(BRIEF_INPUTS), variables=None, trigger_data={"topic_brief": {}},
        )
    assert exc.value.errors == {
        "topic_brief.thesis": "'thesis' is a required property",
        "topic_brief.topic_id": "'topic_id' is a required property",
    }


def test_values_from_trigger_data_or_variable_targets_are_accepted():
    brief = {"topic_id": "t1", "thesis": "Small shops need AI"}
    check_declared_workflow_run_inputs(
        _workflow(BRIEF_INPUTS), variables=None, trigger_data={"topic_brief": brief},
    )
    targeted = [dict(BRIEF_INPUTS[0], target="inputs.brief")]
    check_declared_workflow_run_inputs(
        _workflow(targeted), variables={"inputs": {"brief": brief}}, trigger_data=None,
    )


def test_unattended_triggers_list_only_visible_required_inputs_without_default():
    assert required_workflow_run_inputs_without_default(
        _workflow(BRIEF_INPUTS),
    ) == ["Approved Topic Brief"]
    assert required_workflow_run_inputs_without_default(_workflow([])) == []


async def _auth(client: AsyncClient, username: str) -> dict:
    resp = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com",
        "password": "pass123", "entity_name": "Input Corp",
    })
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create(client: AsyncClient, headers: dict, run_inputs: list[dict]) -> str:
    resp = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Brief flow", "steps": _steps(run_inputs)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_run_endpoints_return_422_before_creating_a_run(client: AsyncClient):
    headers = await _auth(client, "wfinputs422")
    workflow_id = await _create(client, headers, BRIEF_INPUTS)

    for path in ("run", "run-stream"):
        resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/{path}",
            headers=headers,
            json={"trigger_data": {"topic_brief": {"topic_id": "t1"}}},
        )
        assert resp.status_code == 422, resp.text
        assert "Approved Topic Brief (thesis)" in resp.json()["detail"]

    runs = await client.get(f"/api/v1/workflows/{workflow_id}/runs", headers=headers)
    assert runs.json() == []

    ok = await client.post(
        f"/api/v1/workflows/{workflow_id}/run",
        headers=headers,
        json={
            "trigger_data": {"topic_brief": {"topic_id": "t1", "thesis": "x"}},
            "execute": False,
        },
    )
    assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_schedule_deploy_requires_defaults_for_required_inputs(client: AsyncClient):
    headers = await _auth(client, "wfinputsched")
    blocked_id = await _create(client, headers, BRIEF_INPUTS)
    resp = await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": blocked_id,
            "trigger_type": "schedule",
            "trigger_config": {"cron": "0 9 * * *"},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "Approved Topic Brief" in resp.json()["detail"]

    defaulted_id = await _create(client, headers, [BRIEF_INPUTS[1], BRIEF_INPUTS[3]])
    resp = await client.post(
        "/api/v1/workflows/bindings",
        headers=headers,
        json={
            "workflow_id": defaulted_id,
            "trigger_type": "schedule",
            "trigger_config": {"cron": "0 9 * * *"},
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "automation"


@pytest.mark.asyncio
async def test_run_dialog_values_override_workflow_variable_defaults(client: AsyncClient):
    # Blueprint Flows declare input defaults in `variables`; the Run dialog
    # sends the operator's values as trigger_data. The operator must win.
    headers = await _auth(client, "wfinputdefaults")
    resp = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Defaults flow",
            "steps": _steps([
                {"key": "video_duration_seconds", "type": "integer", "required": False},
                {"key": "include_x", "type": "boolean", "required": False},
                {"key": "brief", "type": "string", "required": False, "target": "request.brief"},
                {"key": "tone", "type": "string", "required": False},
            ]),
            "variables": {"video_duration_seconds": 8, "include_x": True, "tone": "warm", "request": {"brief": "default"}},
        },
    )
    assert resp.status_code == 201, resp.text
    workflow_id = resp.json()["id"]

    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/run",
        headers=headers,
        json={
            "trigger_data": {"video_duration_seconds": 5, "include_x": False, "brief": "Brunch launch", "tone": "ignored"},
            "variables": {"tone": "explicit"},
            "execute": False,
        },
    )
    assert run.status_code == 201, run.text
    variables = run.json()["variables"]
    assert variables["video_duration_seconds"] == 5
    assert variables["include_x"] is False
    assert variables["request"]["brief"] == "Brunch launch"
    # A value the caller passed explicitly in `variables` still wins.
    assert variables["tone"] == "explicit"
