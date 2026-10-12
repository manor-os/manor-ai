"""App entries reference runners without creating Workspace Services."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.core.services.workspace_app_view import normalize_app_view_config, project_workspace_app_view
from packages.core.services.workspace_operation_service import _apply_single_patch


def workspace(config):
    return SimpleNamespace(id="space", name="Studio", status="active", primary_work="",
                           operating_context="", operating_model={"app_view": config, "services": []})


@pytest.mark.parametrize("target,kind", [("agent:writer", "chat"), ("agent:default", "chat"),
                                       ("workflow:binding", "workflow"), ("automation:review", "automation")])
def test_direct_entry_resolves_each_runner_without_services(target, kind):
    config = {"entries": {"action:create": {"title": "Create"}}, "targets": {"action:create": target},
              "prompts": {"action:create": "Write a concise draft"},
              "forms": {"action:create": [{"key": "brief", "label": "Brief", "type": "text"}]}}
    ws = workspace(config)
    before = deepcopy(ws.operating_model)
    flow = SimpleNamespace(binding_id="binding", workflow_id="flow", title="Flow", description="",
                           placeholder="", run_inputs=[{"key": "topic", "label": "Topic"}])
    job = SimpleNamespace(job_id="review", name="Review", enabled=False, last_status=None, execution_target={})
    view = project_workspace_app_view(ws, [flow], [job], [{"id": "writer", "name": "Writer"}])
    entry = next(row for row in view["actions"] if row["key"] == "action:create")
    assert entry["kind"] == kind and entry["available"] is True
    assert entry["source_title"] == "Create"
    if kind == "chat":
        assert entry["prompt"] == "Write a concise draft"
        assert entry["inputs"][0]["key"] == "brief"
    elif kind == "workflow":
        assert entry["binding_id"] == "binding"
        assert entry["inputs"][0]["key"] == "topic"
        assert "prompt" not in entry
    else:
        assert entry["job_id"] == "review" and entry["enabled"] is False
        assert entry["inputs"] == [] and "prompt" not in entry
    assert ws.operating_model == before


def test_same_agent_has_independent_commands_and_missing_runner_is_unavailable():
    config = {"entries": {f"action:{key}": {"title": key} for key in ("draft", "review", "missing")},
              "targets": {"action:draft": "agent:writer", "action:review": "agent:writer"},
              "prompts": {"action:draft": "Write a draft", "action:review": "Review the draft"}}
    view = project_workspace_app_view(workspace(config), [], agents=[{"id": "writer", "name": "Writer"}])
    draft, review, missing = view["actions"]
    assert draft["agent_id"] == review["agent_id"] == "writer"
    assert draft["prompt"] != review["prompt"]
    assert missing["available"] is False
    for target in ("agent:removed", "workflow:removed", "automation:removed"):
        config["targets"]["action:missing"] = target
        entry = project_workspace_app_view(workspace(config), [])["actions"][-1]
        assert entry["available"] is False


def test_remove_custom_entry_cleans_only_its_display_settings():
    key = "action:create"
    config = {"entries": {key: {"title": "Create"}, "action:keep": {"title": "Keep"}},
              "targets": {key: "workflow:binding", "action:keep": "agent:default"},
              "prompts": {key: "Create"}, "forms": {key: []}, "labels": {key: "Custom"},
              "descriptions": {key: "Description"}, "order": [key, "action:keep"], "hidden": [key], "hero": key}
    state = {"operating_model": {"app_view": config, "services": [{"key": "legacy", "name": "Legacy"}]}}
    result = _apply_single_patch(deepcopy(state), {"op": "app_view.update", "payload": {"entries": {key: None}}})
    actual = result["operating_model"]["app_view"]
    for field in ("entries", "targets", "prompts", "forms", "labels", "descriptions", "order", "hidden"):
        assert key not in actual[field]
    assert actual["hero"] is None
    assert actual["entries"]["action:keep"]["title"] == "Keep"
    assert result["operating_model"]["services"] == state["operating_model"]["services"]


@pytest.mark.parametrize("config", [
    {"entries": {"service:legacy": {"title": "Invalid"}}},
    {"entries": {"action:x": {"title": " "}}},
    {"entries": {"action:x": {"title": "Name", "agent_id": "writer"}}},
    {"entries": {"action:x": {"title": "Name"}}, "targets": {"action:x": "writer"}},
    {"prompts": {"action:x": "x" * 8001}},
])
def test_direct_entry_schema_rejects_unsupported_shapes(config):
    with pytest.raises(ValueError):
        normalize_app_view_config(config)


def test_namespaced_legacy_service_is_preserved():
    ws = workspace({})
    ws.operating_model["services"] = [{"key": "stickman.production", "name": "Produce"}]
    action = project_workspace_app_view(ws, [], agents=[
        {"id": "writer", "name": "Writer", "service_key": "stickman.production"},
    ])["actions"][0]
    assert action["key"] == "service:stickman.production"
    assert action["agent_id"] == "writer"


@pytest.mark.asyncio
async def test_reviewed_api_adds_switches_and_removes_entries_without_services(client):
    response = await client.post("/api/v1/auth/register", json={
        "username": "app_direct", "email": "app_direct@test.com", "password": "pass123", "entity_name": "App Direct",
    })
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    response = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Service-free App"})
    assert response.status_code == 201, response.text
    base = f"/api/v1/workspaces/{response.json()['id']}"
    ws_id = response.json()["id"]

    async def draft(patches, *, valid=True):
        created = await client.post(f"{base}/operation/drafts", headers=headers, json={"patches": patches})
        assert created.status_code == 200, created.text
        assert created.json()["validation"]["valid"] is valid, created.text
        if valid:
            applied = await client.post(f"{base}/operation/drafts/{created.json()['id']}/apply",
                                        headers=headers, json={"user_confirmation": True})
            assert applied.status_code == 200, applied.text
        return created.json()

    await draft([{"op": "app_view.update", "payload": {
        "entries": {"action:draft": {"title": "Draft"}, "action:review": {"title": "Review"}},
        "targets": {"action:draft": "agent:default", "action:review": "agent:default"},
        "prompts": {"action:draft": "Write a draft", "action:review": "Review the draft"},
    }}])
    view = (await client.get(f"{base}/app-view", headers=headers)).json()
    assert [row["prompt"] for row in view["actions"]] == ["Write a draft", "Review the draft"]
    missing = await draft([{"op": "app_view.update", "payload": {
        "entries": {"action:missing": {"title": "Missing"}},
    }}], valid=False)
    assert any(row.get("code") == "missing_app_target" for row in missing["validation"]["errors"])
    for target in ("agent:foreign", "workflow:foreign", "automation:foreign"):
        await draft([{"op": "app_view.update", "payload": {"targets": {"action:draft": target}}}], valid=False)

    response = await client.post("/api/v1/workflows", headers=headers, json={"name": "App Flow", "steps": [
        {"id": "start", "type": "trigger", "targets": ["end"]},
        {"id": "end", "type": "end", "targets": []},
    ]})
    assert response.status_code == 201, response.text
    flow_id = response.json()["id"]
    await draft([
        {"op": "app_view.update", "payload": {"entries": {"action:flow": {"title": "Flow"}}}},
        {"op": "workflow_binding.attach", "payload": {"workflow_id": flow_id, "action_key": "action:flow"}},
    ])
    view = (await client.get(f"{base}/app-view", headers=headers)).json()
    selected = next(row for row in view["actions"] if row["key"] == "action:flow")
    assert selected["workflow_id"] == flow_id and selected["available"] is True
    binding_id = selected["binding_id"]
    response = await client.post("/api/v1/jobs", headers=headers, json={
        "job_id": "direct-review", "name": "Review", "workspace_id": ws_id,
        "schedule_kind": "every", "every_seconds": 3600, "payload_message": "Review",
    })
    assert response.status_code == 201, response.text
    await client.post("/api/v1/jobs/direct-review/toggle", headers=headers, json={"enabled": False})
    for target, kind in ((f"workflow:{binding_id}", "workflow"), ("automation:direct-review", "automation"), ("agent:default", "chat")):
        await draft([{"op": "app_view.update", "payload": {"targets": {"action:draft": target}}}])
        view = (await client.get(f"{base}/app-view", headers=headers)).json()
        entry = next(row for row in view["actions"] if row["key"] == "action:draft")
        assert entry["kind"] == kind and entry["available"] is True

    await draft([{"op": "app_view.update", "payload": {"entries": {"action:flow": None, "action:draft": None}}}])
    view = (await client.get(f"{base}/app-view", headers=headers)).json()
    assert set(view["config"]["entries"]) == {"action:review"}
    assert any(row["binding_id"] == binding_id for row in view["workflow_options"])
    assert any(row["job_id"] == "direct-review" for row in view["automation_options"])
    ws = (await client.get(base, headers=headers)).json()
    assert not ws["operating_model"].get("services")
