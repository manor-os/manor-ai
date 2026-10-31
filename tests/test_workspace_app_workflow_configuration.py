"""App targets use live Workspace bindings through reviewed operations."""
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_operation_action
from packages.core.models.user import User
from packages.core.services import workspace_operation_service as operations
from packages.core.services.workspace_app_view import load_workspace_app_view, project_workspace_app_view


def workspace(target=None):
    model = {"services": [{"key": "draft", "name": "Draft content", "description": "Ask the writer"}]}
    if target:
        model["app_view"] = {"targets": {"service:draft": target}}
    return SimpleNamespace(id="workspace", entity_id="entity", name="Studio", status="active",
                           operating_model=model, primary_work="", operating_context="", cover_image_url=None)


def flow():
    return SimpleNamespace(binding_id="binding", workflow_id="workflow", title="Write an article",
                           description="Generate an article", placeholder="Topic", run_inputs=(
                               {"key": "topic", "label": "Topic", "required": True},))


def test_service_workflow_target_uses_the_flow_input_contract_and_keeps_service_identity():
    view = project_workspace_app_view(workspace("workflow:binding"), [flow()])
    action = next(row for row in view["actions"] if row["key"] == "service:draft")
    assert action["kind"] == "workflow"
    assert action["binding_id"] == "binding"
    assert action["title"] == "Draft content"
    assert action["source_description"] == "Ask the writer"
    assert action["inputs"] == list(flow().run_inputs)


def test_a_targeted_entry_carries_a_hidden_flow_input_only_when_its_preset_sets_it():
    hidden_flow = SimpleNamespace(**{**vars(flow()), "run_inputs": (
        {"key": "topic", "label": "Topic", "required": True},
        {"key": "kind", "label": "Kind", "hidden": True, "default": ""},
    )})
    ws = workspace("workflow:binding")
    ws.operating_model["app_view"]["presets"] = {"service:draft": {"kind": "pitch"}}
    action = next(row for row in project_workspace_app_view(ws, [hidden_flow])["actions"] if row["key"] == "service:draft")
    assert [(item["key"], item.get("default"), item.get("hidden", False)) for item in action["inputs"]] == [
        ("topic", None, False), ("kind", "pitch", True)]
    action = next(row for row in project_workspace_app_view(workspace("workflow:binding"), [hidden_flow])["actions"]
                  if row["key"] == "service:draft")
    assert [item["key"] for item in action["inputs"]] == ["topic"]


def test_unavailable_selected_workflow_never_falls_back_to_the_service_agent():
    agent = {"id": "agent", "name": "Writer", "service_key": "draft"}
    action = project_workspace_app_view(workspace("workflow:binding"), [], agents=[agent])["actions"][0]
    assert action["kind"] == "workflow"
    assert action["available"] is False
    assert action["binding_id"] is None
    assert "agent_id" not in action


def test_default_app_uses_a_workflow_as_primary_and_keeps_services_secondary():
    view = project_workspace_app_view(workspace(), [])
    assert view["hero"] is None
    assert view["actions"][0]["kind"] == "chat"
    ws = workspace()
    ws.operating_model["app_view"] = {"order": ["service:draft"]}
    view = project_workspace_app_view(ws, [flow()])
    assert view["actions"][0]["kind"] == "chat"
    assert view["hero"] == "workflow:binding"


def test_removed_primary_workflow_is_not_replaced_by_a_different_workflow():
    ws = workspace()
    ws.operating_model["app_view"] = {"hero": "workflow:removed"}
    view = project_workspace_app_view(ws, [flow()])
    assert view["hero"] is None
    assert view["actions"][0]["binding_id"] == "binding"


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return self


@pytest.mark.asyncio
async def test_shared_catalog_contains_agents_automations_and_only_readable_saved_workflows(monkeypatch):
    user = SimpleNamespace(id="user", entity_id="entity", role="owner")
    agent = SimpleNamespace(id="agent", name="Writer", display_name=None)
    job = SimpleNamespace(job_id="job", name="Daily", enabled=True, last_status=None,
                          execution_target={}, agent_initiative_id=None)
    saved = SimpleNamespace(id="saved", entity_id="entity", name="Saved", description="", steps=[
        {"id": "start", "type": "trigger", "targets": ["end"]},
        {"id": "end", "type": "end", "targets": []},
    ], variables={})
    private = SimpleNamespace(id="private", entity_id="entity", name="Private", description="", steps=[], variables={})
    invalid = SimpleNamespace(id="invalid", entity_id="entity", name="Invalid", description="", steps=[], variables={})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        Rows([(SimpleNamespace(service_key="draft"), agent)]), Rows([job]), Rows([[]]), Rows([]),
        Rows([saved, private, invalid]),
    ]))
    entries = AsyncMock(return_value=[flow()])
    monkeypatch.setattr("packages.core.services.workspace_workflow_router.list_workspace_chat_entrypoints", entries)
    monkeypatch.setattr("packages.core.services.resource_access.readable_resource_ids", AsyncMock(return_value={"saved", "invalid"}))
    view = await load_workspace_app_view(db, workspace(), user=user)
    assert view["agent_options"] == [{"id": "agent", "name": "Writer"}]
    assert view["automation_options"][0]["job_id"] == "job"
    assert [row["workflow_id"] for row in view["attachable_workflow_options"]] == ["saved"]
    assert entries.call_args.kwargs["include_unpublished"] is True
    assert entries.call_args.kwargs["require_control"] is True
    assert entries.call_args.kwargs["user"] is user


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["agent:agent", "workflow:binding", "automation:job"])
async def test_runtime_app_keeps_live_targets_without_querying_saved_workflow_candidates(monkeypatch, target):
    user = SimpleNamespace(id="user", entity_id="entity", role="owner")
    agent = SimpleNamespace(id="agent", name="Writer", display_name=None)
    job = SimpleNamespace(job_id="job", name="Daily", enabled=False, last_status=None,
                          execution_target={}, agent_initiative_id=None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        Rows([(SimpleNamespace(service_key="draft"), agent)]), Rows([job]), Rows([[]]), Rows([]),
    ]))
    entries = AsyncMock(return_value=[flow()])
    readable = AsyncMock()
    monkeypatch.setattr("packages.core.services.workspace_workflow_router.list_workspace_chat_entrypoints", entries)
    monkeypatch.setattr("packages.core.services.resource_access.readable_resource_ids", readable)

    view = await load_workspace_app_view(db, workspace(target), user=user, include_catalog=False)

    assert view == project_workspace_app_view(workspace(target), [flow()], [job], [
        {"id": "agent", "name": "Writer", "service_key": "draft"},
    ])
    assert db.execute.await_count == 4
    readable.assert_not_awaited()
    assert entries.call_args.kwargs["require_control"] is True
    assert entries.call_args.kwargs["user"] is user


@pytest.mark.asyncio
async def test_app_loader_calls_real_workflow_catalog_with_unpublished_bindings(monkeypatch):
    user = SimpleNamespace(id="user", entity_id="entity", role="owner")
    binding = SimpleNamespace(id="binding", workflow_id="workflow", workspace_id="workspace",
                              name=None, config={"chat_entrypoint": {"enabled": False}})
    workflow = SimpleNamespace(id="workflow", name="Article Flow", description="Generate an article",
                               steps=[], variables={})
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        Rows([(binding, workflow)]), Rows([]), Rows([]), Rows([[]]), Rows([]), Rows([]),
    ]))
    monkeypatch.setattr("packages.core.services.workspace_access.user_can_write_workspace_artifacts",
                        AsyncMock(return_value=True))
    monkeypatch.setattr("packages.core.services.resource_access.readable_resource_ids",
                        AsyncMock(return_value=set()))

    view = await load_workspace_app_view(db, workspace(), user=user)

    assert view["hero"] == "workflow:binding"
    assert view["workflow_options"][0]["title"] == "Article Flow"


@pytest.mark.asyncio
async def test_chat_get_current_returns_the_shared_actor_catalog(monkeypatch):
    user = SimpleNamespace(id="user", entity_id="entity")
    ws = workspace()
    db = SimpleNamespace(get=AsyncMock(side_effect=lambda cls, _id: user if cls is User else ws))
    expected = {"agent_options": [{"id": "agent"}], "automation_options": [{"job_id": "job"}]}
    loader = AsyncMock(return_value=expected)

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr("packages.core.database.async_session", session)
    monkeypatch.setattr(operations, "get_current_operation_state", AsyncMock(return_value={}))
    monkeypatch.setattr("packages.core.services.workspace_app_view.load_workspace_app_view", loader)
    response = json.loads(await runtime_workspace_operation_action(
        entity_id="entity", workspace_id="workspace", user_id="user", params={"action": "get_current"},
    ))
    assert response["app_view"] == expected
    loader.assert_awaited_once_with(db, ws, user=user)


def attachment_fixture(monkeypatch, *, allowed=True, active=True, binding=None):
    user = SimpleNamespace(id="user", entity_id="entity", role="owner")
    wf = SimpleNamespace(id="workflow", entity_id="entity", name="Article", is_active=active,
                         status="active", trigger_type="manual", workspace_id=None)
    db = SimpleNamespace(get=AsyncMock(side_effect=lambda cls, _id: user if cls is User else wf))
    monkeypatch.setattr("packages.core.services.resource_access.user_can_access_resource", AsyncMock(return_value=allowed))
    monkeypatch.setattr("packages.core.services.workflow_service.validate_workflow_definition", lambda _wf: {"valid": True})
    monkeypatch.setattr("packages.core.services.workflow_service.list_bindings", AsyncMock(return_value=[binding] if binding else []))
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,active", [(False, True), (True, False)])
async def test_attachment_revalidates_resource_access_and_active_definition(monkeypatch, allowed, active):
    db = attachment_fixture(monkeypatch, allowed=allowed, active=active)
    with pytest.raises(ValueError):
        await operations._resolve_workflow_attachment(
            db, {"workflow_id": "workflow"}, entity_id="entity", workspace_id="workspace", user_id="user",
        )


@pytest.mark.asyncio
async def test_attach_is_only_materialized_on_apply_and_reuses_existing_binding(monkeypatch):
    binding = SimpleNamespace(id="binding", trigger_type="manual", enabled=True, status="active")
    db = attachment_fixture(monkeypatch, binding=binding)
    create = AsyncMock()
    monkeypatch.setattr("packages.core.services.workflow_service.create_workflow_binding", create)
    ws = workspace()
    state = {"operating_model": deepcopy(ws.operating_model)}
    state = operations._apply_single_patch(state, {"op": "workflow_binding.attach", "payload": {
        "workflow_id": "workflow", "service_key": "draft",
    }})
    assert "app_view" not in ws.operating_model
    result = await operations._materialize_workflow_attachments(db, ws, state["workflow_attachments"], user_id="user")
    assert result[0]["binding_id"] == "binding"
    assert ws.operating_model["app_view"]["targets"] == {"service:draft": "workflow:binding"}
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_creates_a_manual_workspace_binding_without_running_the_workflow(monkeypatch):
    db = attachment_fixture(monkeypatch)
    binding = SimpleNamespace(id="new-binding")
    create = AsyncMock(return_value=binding)
    monkeypatch.setattr("packages.core.services.workflow_service.create_workflow_binding", create)
    ws = workspace()
    result = await operations._materialize_workflow_attachments(db, ws, [{"workflow_id": "workflow"}], user_id="user")
    assert result[0]["binding_id"] == "new-binding"
    assert create.call_args.kwargs["workspace_id"] == ws.id
    assert create.call_args.kwargs["trigger_type"] == "manual"
    assert create.call_args.kwargs["config"] == {"workspace_attached": True, "chat_entrypoint": {"enabled": True}}


@pytest.mark.asyncio
async def test_primary_workflow_generates_app_config_in_a_workspace_without_services(monkeypatch):
    db = attachment_fixture(monkeypatch)
    binding = SimpleNamespace(id="new-binding")
    monkeypatch.setattr("packages.core.services.workflow_service.create_workflow_binding", AsyncMock(return_value=binding))
    ws = workspace()
    ws.operating_model = {}
    state = operations._apply_single_patch({"operating_model": {}}, {
        "op": "workflow_binding.attach", "payload": {"workflow_id": "workflow", "primary": True},
    })
    assert ws.operating_model == {}
    await operations._materialize_workflow_attachments(db, ws, state["workflow_attachments"], user_id="user")
    assert ws.operating_model["app_view"]["hero"] == "workflow:new-binding"
    assert "services" not in ws.operating_model


def test_primary_attachment_requires_a_boolean():
    with pytest.raises(ValueError, match="primary must be boolean"):
        operations._apply_single_patch({}, {
            "op": "workflow_binding.attach", "payload": {"workflow_id": "workflow", "primary": "true"},
        })


@pytest.mark.asyncio
async def test_attach_rejects_an_inactive_existing_binding(monkeypatch):
    binding = SimpleNamespace(id="binding", trigger_type="manual", enabled=False, status="active")
    db = attachment_fixture(monkeypatch, binding=binding)
    with pytest.raises(ValueError, match="inactive"):
        await operations._resolve_workflow_attachment(
            db, {"workflow_id": "workflow"}, entity_id="entity", workspace_id="workspace", user_id="user",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, {"entity_id": "other", "deleted_at": None},
                                      {"entity_id": "entity", "deleted_at": "deleted"}])
async def test_attach_rejects_an_unavailable_source_workspace(monkeypatch, source):
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.models.workspace import Workspace

    db = attachment_fixture(monkeypatch)
    user = await db.get(User, "user")
    workflow = await db.get(WorkflowDefinition, "workflow")
    workflow.workspace_id = "source"
    rows = {User: user, WorkflowDefinition: workflow, Workspace: SimpleNamespace(**source) if source else None}
    db.get = AsyncMock(side_effect=lambda cls, _id: rows[cls])
    with pytest.raises(ValueError, match="source Workspace"):
        await operations._resolve_workflow_attachment(
            db, {"workflow_id": "workflow"}, entity_id="entity", workspace_id="workspace", user_id="user",
        )


def test_chat_tool_guidance_exposes_target_types_and_reviewed_attachment():
    from packages.core.ai.tools.manor_tool import MANOR_SCHEMA
    from packages.core.ai.runtime.workspace_operation_actions import WORKSPACE_OPERATION_CONFIGURATION_GUIDANCE
    guidance = MANOR_SCHEMA["function"]["parameters"]["properties"]["params"]["description"]
    for contract in ("entries:", "prompts", "targets:", "workflow_options", "attachable_workflow_options", "workflow_binding.attach"):
        assert contract in guidance
    assert "prompts:" in WORKSPACE_OPERATION_CONFIGURATION_GUIDANCE


@pytest.mark.asyncio
async def test_reviewed_attachment_persists_a_runnable_app_target_and_rechecks_disable(client):
    response = await client.post("/api/v1/auth/register", json={
        "username": "app_workflow_attach", "email": "app_workflow_attach@test.com",
        "password": "pass123", "entity_name": "App Workflow Tests",
    })
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    response = await client.post("/api/v1/workspaces", headers=headers, json={"name": "App Studio"})
    assert response.status_code == 201, response.text
    ws_id = response.json()["id"]
    response = await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Article Flow", "steps": [
            {"id": "start", "type": "trigger", "targets": ["end"], "config": {"run_inputs": [
                {"key": "topic", "label": "Topic", "required": True},
            ]}},
            {"id": "end", "type": "end", "targets": []},
        ],
    })
    assert response.status_code == 201, response.text
    workflow_id = response.json()["id"]
    response = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
        "patches": [
            {"op": "service_role.upsert", "payload": {"service": {"key": "article", "name": "Write Article"}}},
            {"op": "workflow_binding.attach", "payload": {
                "workflow_id": workflow_id, "service_key": "article", "slug": "article-flow",
            }},
        ],
    })
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["validation"]["valid"], draft["validation"]
    assert "workflow_attachments" in draft["diff"]["changed_keys"]
    bindings_url = f"/api/v1/workflows/bindings?workspace_id={ws_id}"
    assert (await client.get(bindings_url, headers=headers)).json() == []
    denied = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft['id']}/apply", headers=headers,
                               json={"user_confirmation": False})
    assert denied.status_code == 400, denied.text
    applied = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts/{draft['id']}/apply", headers=headers,
                                json={"user_confirmation": True})
    assert applied.status_code == 200, applied.text
    bindings = (await client.get(bindings_url, headers=headers)).json()
    assert len(bindings) == 1
    # The slug is the Workflow's portable identity (export, agent_review
    # Stats); a later attach cannot rename it.
    assert bindings[0]["config"]["workspace_blueprint_workflow_slug"] == "article-flow"
    renamed = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
        "patches": [{"op": "workflow_binding.attach", "payload": {"workflow_id": workflow_id, "slug": "renamed-flow"}}],
    })
    assert renamed.status_code == 200 and renamed.json()["validation"]["valid"] is False, renamed.text
    view = (await client.get(f"/api/v1/workspaces/{ws_id}/app-view", headers=headers)).json()
    action = next(row for row in view["actions"] if row["key"] == "service:article")
    assert action["kind"] == "workflow"
    assert action["binding_id"] == bindings[0]["id"]
    assert action["inputs"][0]["key"] == "topic"
    # Config can switch an already-connected service back to Chat, then choose
    # its existing binding without creating a second binding or losing its form.
    form = [{"key": "brief", "label": "Brief", "type": "text", "required": True}]
    for target, kind in [(None, "chat"), (f"workflow:{bindings[0]['id']}", "workflow")]:
        response = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
            "patches": [{"op": "app_view.update", "payload": {
                "targets": {"service:article": target}, "forms": {"service:article": form},
            }}],
        })
        assert response.status_code == 200, response.text
        assert response.json()["validation"]["valid"], response.text
        result = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts/{response.json()['id']}/apply",
                                   headers=headers, json={"user_confirmation": True})
        assert result.status_code == 200, result.text
        view = (await client.get(f"/api/v1/workspaces/{ws_id}/app-view", headers=headers)).json()
        action = next(row for row in view["actions"] if row["key"] == "service:article")
        assert action["kind"] == kind
        assert view["config"]["forms"]["service:article"][0]["key"] == "brief"
        assert action["inputs"][0]["key"] == ("brief" if kind == "chat" else "topic")
    # All entry sources can choose each runner, with the same reviewed save
    # path and target-specific forms after reloading the real App API.
    response = await client.post("/api/v1/jobs", headers=headers, json={
        "job_id": "app-review", "name": "Review", "workspace_id": ws_id,
        "schedule_kind": "every", "every_seconds": 3600, "payload_message": "Review articles",
    })
    assert response.status_code == 201, response.text
    response = await client.post("/api/v1/jobs/app-review/toggle", headers=headers, json={"enabled": False})
    assert response.status_code == 200 and response.json()["enabled"] is False, response.text
    entry_keys = ["service:article", f"workflow:{bindings[0]['id']}", "automation:app-review"]
    for target, kind in [("agent:default", "chat"), (f"workflow:{bindings[0]['id']}", "workflow"), ("automation:app-review", "automation")]:
        response = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
            "patches": [{"op": "app_view.update", "payload": {
                "targets": {key: target for key in entry_keys}, "forms": {key: form for key in entry_keys},
            }}],
        })
        assert response.status_code == 200, response.text
        assert response.json()["validation"]["valid"], response.text
        applied = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts/{response.json()['id']}/apply",
                                    headers=headers, json={"user_confirmation": True})
        assert applied.status_code == 200, applied.text
        view = (await client.get(f"/api/v1/workspaces/{ws_id}/app-view", headers=headers)).json()
        for key in entry_keys:
            action = next(row for row in view["actions"] if row["key"] == key)
            assert action["kind"] == kind
            assert action["available"] is True
            assert view["config"]["targets"][key] == target
            if kind == "chat":
                assert action["inputs"][0]["key"] == "brief"
            elif kind == "workflow":
                assert action["inputs"][0]["key"] == "topic"
            else:
                assert action["job_id"] == "app-review"
                assert action["instructions"] == "Review articles"
                assert action["enabled"] is False
    for target in ("agent:missing", "workflow:missing", "automation:missing"):
        response = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
            "patches": [{"op": "app_view.update", "payload": {"targets": {"automation:app-review": target}}}],
        })
        assert response.status_code == 200, response.text
        assert not response.json()["validation"]["valid"], response.text
        assert any(error.get("code") == "unavailable_app_target" for error in response.json()["validation"]["errors"])
    # Connecting a saved Workflow to an Automation entry uses action_key and
    # reuses the binding without creating or enabling a scheduled job.
    response = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
        "patches": [{"op": "workflow_binding.attach", "payload": {
            "workflow_id": workflow_id, "action_key": "automation:app-review",
        }}],
    })
    assert response.status_code == 200, response.text
    assert response.json()["validation"]["valid"], response.text
    applied = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts/{response.json()['id']}/apply",
                                headers=headers, json={"user_confirmation": True})
    assert applied.status_code == 200, applied.text
    view = (await client.get(f"/api/v1/workspaces/{ws_id}/app-view", headers=headers)).json()
    action = next(row for row in view["actions"] if row["key"] == "automation:app-review")
    assert action["kind"] == "workflow" and action["binding_id"] == bindings[0]["id"]
    for key in ("automation:missing", "workflow:missing"):
        response = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
            "patches": [{"op": "workflow_binding.attach", "payload": {"workflow_id": workflow_id, "action_key": key}}],
        })
        assert response.status_code == 200, response.text
        assert not response.json()["validation"]["valid"], response.text
    # Restore the service's Workflow for the existing disable-at-apply check.
    response = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
        "patches": [{"op": "app_view.update", "payload": {"targets": {"service:article": f"workflow:{bindings[0]['id']}"}}}],
    })
    assert response.status_code == 200 and response.json()["validation"]["valid"], response.text
    applied = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts/{response.json()['id']}/apply",
                                headers=headers, json={"user_confirmation": True})
    assert applied.status_code == 200, applied.text
    pending = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
        "patches": [{"op": "workflow_binding.attach", "payload": {"workflow_id": workflow_id, "service_key": "article"}}],
    })
    assert pending.status_code == 200, pending.text
    assert pending.json()["validation"]["valid"], pending.text
    disabled = await client.put(f"/api/v1/workflows/bindings/{bindings[0]['id']}", headers=headers, json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    rejected = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts/{pending.json()['id']}/apply",
                                 headers=headers, json={"user_confirmation": True})
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["detail"]["valid"] is False
    view = (await client.get(f"/api/v1/workspaces/{ws_id}/app-view", headers=headers)).json()
    action = next(row for row in view["actions"] if row["key"] == "service:article")
    assert action["available"] is False
    retry = await client.post(f"/api/v1/workspaces/{ws_id}/operation/drafts", headers=headers, json={
        "patches": [{"op": "workflow_binding.attach", "payload": {"workflow_id": workflow_id, "service_key": "article"}}],
    })
    assert retry.status_code == 200, retry.text
    assert retry.json()["validation"]["valid"] is False
    assert len((await client.get(bindings_url, headers=headers)).json()) == 1

    # An existing Workspace without an App or services can select the saved
    # definition directly; the approved binding becomes its primary action.
    response = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Empty Studio"})
    assert response.status_code == 201, response.text
    empty_id = response.json()["id"]
    view_url = f"/api/v1/workspaces/{empty_id}/app-view"
    view = (await client.get(view_url, headers=headers)).json()
    assert view["actions"] == []
    assert view["hero"] is None
    assert any(row["workflow_id"] == workflow_id for row in view["attachable_workflow_options"])
    response = await client.post(f"/api/v1/workspaces/{empty_id}/operation/drafts", headers=headers, json={
        "patches": [{"op": "workflow_binding.attach", "payload": {"workflow_id": workflow_id, "primary": True}}],
    })
    assert response.status_code == 200, response.text
    assert response.json()["validation"]["valid"], response.text
    result = await client.post(f"/api/v1/workspaces/{empty_id}/operation/drafts/{response.json()['id']}/apply",
                               headers=headers, json={"user_confirmation": True})
    assert result.status_code == 200, result.text
    view = (await client.get(view_url, headers=headers)).json()
    assert view["title"] == "Empty Studio"
    assert view["hero"] == f"workflow:{view['actions'][0]['binding_id']}"
    assert view["config"]["hero"] == view["hero"]
    assert view["actions"][0]["inputs"][0]["key"] == "topic"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["workflow:old-binding", "automation:review"])
async def test_saved_workflow_attachment_can_target_any_app_entry(monkeypatch, key):
    binding = SimpleNamespace(id="binding", enabled=True, status="active", trigger_type="manual")
    db = attachment_fixture(monkeypatch, binding=binding)
    ws = workspace()
    ws.operating_model["app_view"] = {"targets": {key: "automation:review"}}
    previous_model = ws.operating_model
    from copy import deepcopy
    previous_value = deepcopy(previous_model)
    state = operations._apply_single_patch({"workflow_attachments": []}, {
        "op": "workflow_binding.attach", "payload": {"workflow_id": "workflow", "action_key": key},
    })
    result = await operations._materialize_workflow_attachments(db, ws, state["workflow_attachments"], user_id="user")
    assert ws.operating_model["app_view"]["targets"][key] == "workflow:binding"
    assert result[0]["action_key"] == key
    # JSONB change tracking needs a fresh value, including when only the
    # existing target changes and the Workflow binding is reused.
    assert previous_model == previous_value
    assert ws.operating_model is not previous_model


def test_attachment_rejects_ambiguous_entry_targets_and_replaces_the_same_entry():
    with pytest.raises(ValueError, match="either action_key or service_key"):
        operations._apply_single_patch({}, {"op": "workflow_binding.attach", "payload": {
            "workflow_id": "flow", "action_key": "automation:review", "service_key": "draft",
        }})
    state = {}
    for workflow_id in ("first", "second"):
        state = operations._apply_single_patch(state, {"op": "workflow_binding.attach", "payload": {
            "workflow_id": workflow_id, "action_key": "automation:review",
        }})
    assert state["workflow_attachments"] == [{"workflow_id": "second", "action_key": "automation:review"}]
