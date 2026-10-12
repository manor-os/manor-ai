import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services.workspace_app_view import (
    normalize_app_view_config,
    project_workspace_app_view,
)
from packages.core.services.workspace_operation_service import _apply_single_patch
from packages.core.ai.tools import workspace_arch_tools


def _workspace(config=None):
    model = {
        "services": [
            {"key": "customer_support", "name": "Handle customer questions", "description": "Respond with the workspace knowledge."},
        ],
    }
    if config is not None:
        model["app_view"] = config
    return SimpleNamespace(
        id="workspace-1", name="Support Desk", primary_work="Help customers",
        operating_context="", status="active", operating_model=model,
        cover_image_url="https://example.com/cover.jpg",
    )


def _flow():
    return SimpleNamespace(
        binding_id="binding-1", workflow_id="workflow-1",
        title="Triage a request", description="Review and categorize a request",
        placeholder="", run_inputs=({"key": "request", "label": "Request", "required": True},),
    )


def test_app_view_uses_live_capability_names_and_removes_missing_flows():
    initial = project_workspace_app_view(_workspace(), [_flow()])
    assert initial["hero"] == "workflow:binding-1"
    assert initial["cover_image_url"] == "https://example.com/cover.jpg"
    assert initial["cover_source"] == "workspace"
    assert initial["config"]["visual"] == "auto"
    assert initial["config"]["layout"] == "feature"
    assert [item["title"] for item in initial["actions"]] == [
        "Triage a request", "Handle customer questions",
    ]

    changed = project_workspace_app_view(_workspace({
        "version": 1,
        "hero": "service:customer_support",
        "order": ["service:customer_support"],
        "labels": {"service:customer_support": "Talk to customers"},
    }), [])
    assert changed["hero"] == "service:customer_support"
    assert changed["actions"] == [{
        "key": "service:customer_support", "kind": "chat",
        "title": "Talk to customers",
        "source_title": "Handle customer questions",
        "source_description": "Respond with the workspace knowledge.",
        "description": "Respond with the workspace knowledge.",
        "prompt": "Handle customer questions",
        "inputs": [],
    }]


def test_app_view_uses_starter_cover_only_when_workspace_has_none():
    workspace = _workspace()
    workspace.cover_image_url = None
    view = project_workspace_app_view(workspace, [], starter_cover_url="/blueprints/covers/starter.webp")
    assert view["cover_image_url"] == "/blueprints/covers/starter.webp"
    assert view["cover_source"] == "starter"


def test_motion_cover_keeps_its_source_image_binding():
    view = project_workspace_app_view(_workspace({
        "visual": "motion",
        "motion_cover_url": "/media/animated-cover.webm",
        "motion_cover_source_url": "https://example.com/cover.jpg",
    }), [])
    assert view["config"]["motion_cover_url"] == "/media/animated-cover.webm"
    assert view["config"]["motion_cover_source_url"] == view["cover_image_url"]


def test_motion_cover_update_persists_video_and_source_together():
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update",
        "payload": {
            "visual": "motion",
            "motion_cover_url": "/media/animated-cover.webm",
            "motion_cover_source_url": "https://example.com/cover.jpg",
        },
    })
    config = updated["operating_model"]["app_view"]
    assert config["motion_cover_url"] == "/media/animated-cover.webm"
    assert config["motion_cover_source_url"] == "https://example.com/cover.jpg"


def test_service_form_is_saved_and_projected_as_guided_inputs():
    fields = [
        {"key": "song", "label": "Upload a song", "type": "file", "required": True},
        {"key": "release_date", "label": "Release date", "type": "text", "required": True,
         "placeholder": "YYYY-MM-DD"},
    ]
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update",
        "payload": {"forms": {"service:customer_support": fields}},
    })
    view = project_workspace_app_view(
        SimpleNamespace(**{**vars(_workspace()), "operating_model": updated["operating_model"]}), [],
    )
    assert [field["key"] for field in view["actions"][0]["inputs"]] == ["song", "release_date"]
    assert view["actions"][0]["inputs"][0]["type"] == "file"

    removed = _apply_single_patch(updated, {
        "op": "app_view.update", "payload": {"forms": {"service:customer_support": None}},
    })
    assert removed["operating_model"]["app_view"]["forms"] == {}


def test_app_view_copy_and_input_contract_follow_saved_presentation_and_live_flow():
    flow = _flow()
    flow.run_inputs = ({"key": "request", "label": "Request", "type": "json",
                        "required": True, "placeholder": "Paste a request",
                        "schema": {"type": "object", "properties": {"goal": {"type": "string"}}}},)
    view = project_workspace_app_view(_workspace({
        "eyebrow": "YOUR STUDIO",
        "title": "Release studio",
        "description": "Prepare and review a release.",
        "hero": "workflow:binding-1",
        "descriptions": {"workflow:binding-1": "Start with a request."},
    }), [flow])
    assert view["title"] == "Release studio"
    assert view["eyebrow"] == "YOUR STUDIO"
    assert view["description"] == "Prepare and review a release."
    assert view["actions"][0]["description"] == "Start with a request."
    assert view["actions"][0]["inputs"] == [
        {"key": "request", "label": "Request", "type": "json",
         "required": True, "placeholder": "Paste a request",
         "schema": {"type": "object", "properties": {"goal": {"type": "string"}}}},
    ]
    assert view["actions"][0]["binding_id"] == "binding-1"


def test_app_view_targets_live_flow_agent_and_automation():
    alternate = SimpleNamespace(
        binding_id="binding-2", workflow_id="workflow-2", title="Publish a release",
        description="Prepare the final package", placeholder="", run_inputs=(),
    )
    job = SimpleNamespace(job_id="job-1", name="Weekly release check", enabled=True, last_status="succeeded")
    view = project_workspace_app_view(_workspace({
        "targets": {
            "workflow:binding-1": "binding-2",
            "service:customer_support": "agent-2",
            "automation:job-1": "job-1",
        },
    }), [_flow(), alternate], [job], [
        {"id": "agent-1", "name": "Default helper", "service_key": "customer_support"},
        {"id": "agent-2", "name": "Release advisor", "service_key": ""},
    ])
    assert view["actions"][0]["binding_id"] == "binding-2"
    assert view["actions"][0]["inputs"] == []
    assert [option["binding_id"] for option in view["workflow_options"]] == ["binding-1", "binding-2"]
    assert view["actions"][2]["agent_id"] == "agent-2"
    assert view["actions"][2]["agent_name"] == "Release advisor"
    assert view["actions"][2]["default_agent_id"] == "agent-1"
    assert view["actions"][3]["kind"] == "automation"
    assert view["actions"][3]["job_id"] == "job-1"


def test_automation_explains_its_live_process_and_schedule():
    job = SimpleNamespace(
        job_id="job-1", name="Weekly request review", enabled=False,
        last_status=None, job_type="interval", schedule_kind="every",
        every_seconds=604800, run_at=None, cron_expr=None, timezone="UTC",
        payload_message=None,
        execution_target={"binding_id": "binding-1", "workflow_id": "workflow-1"},
    )
    view = project_workspace_app_view(_workspace(), [_flow()], [job])
    action = next(item for item in view["actions"] if item["kind"] == "automation")
    assert action["description"] == "Review and categorize a request"
    assert action["target_title"] == "Triage a request"
    assert action["target_workflow_id"] == "workflow-1"
    assert action["every_seconds"] == 604800
    assert action["enabled"] is False


def test_app_view_target_patch_can_be_removed_without_changing_service():
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update", "payload": {"targets": {"service:customer_support": "agent-2"}},
    })
    assert updated["operating_model"]["app_view"]["targets"] == {"service:customer_support": "agent-2"}
    assert updated["operating_model"]["services"] == state["operating_model"]["services"]
    removed = _apply_single_patch(updated, {
        "op": "app_view.update", "payload": {"targets": {"service:customer_support": None}},
    })
    assert removed["operating_model"]["app_view"]["targets"] == {}


def test_app_view_operation_changes_presentation_without_changing_service():
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update",
        "payload": {"title": "Help desk", "description": "Ask a question.",
                    "labels": {"service:customer_support": "Customer help"},
                    "descriptions": {"service:customer_support": "Get an answer."},
                    "visual": "motion", "layout": "cover"},
    })
    assert updated["operating_model"]["services"][0]["name"] == "Handle customer questions"
    assert updated["operating_model"]["app_view"]["labels"] == {
        "service:customer_support": "Customer help",
    }
    assert updated["operating_model"]["app_view"]["visual"] == "motion"
    assert updated["operating_model"]["app_view"]["layout"] == "cover"
    assert updated["operating_model"]["app_view"]["title"] == "Help desk"
    assert updated["operating_model"]["app_view"]["descriptions"] == {
        "service:customer_support": "Get an answer.",
    }
    assert project_workspace_app_view(
        SimpleNamespace(**{**vars(_workspace()), "operating_model": updated["operating_model"]}),
        [],
    )["actions"][0]["title"] == "Customer help"

    restored = _apply_single_patch(updated, {
        "op": "app_view.update",
        "payload": {"labels": {"service:customer_support": None}},
    })
    assert project_workspace_app_view(
        SimpleNamespace(**{**vars(_workspace()), "operating_model": restored["operating_model"]}),
        [],
    )["actions"][0]["title"] == "Handle customer questions"


@pytest.mark.asyncio
async def test_architect_can_choose_a_real_service_as_the_initial_app_entry(monkeypatch):
    draft = SimpleNamespace(fields={"services": [
        {"service_key": "research", "name": "Research"},
        {"service_key": "writing", "name": "Writing"},
    ]})
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", AsyncMock(return_value=draft))
    persist = AsyncMock()
    monkeypatch.setattr(workspace_arch_tools, "_persist", persist)

    result = await workspace_arch_tools._configure_app_view(
        None, entity_id="entity-1", draft_id="draft-1",
        hero_service_key="writing", service_order=["writing", "research"],
        page_layout="list",
        labels=[{"service_key": "writing", "label": "Start a draft"}],
        forms=[{"service_key": "writing", "fields": [
            {"key": "source", "label": "Upload source", "type": "file", "required": True},
        ]}],
    )

    assert json.loads(result)["ok"] is True
    assert draft.fields["app_view"]["hero"] == "service:writing"
    assert draft.fields["app_view"]["layout"] == "list"
    assert draft.fields["app_view"]["labels"] == {"service:writing": "Start a draft"}
    assert draft.fields["app_view"]["forms"]["service:writing"][0]["type"] == "file"
    persist.assert_awaited_once_with(None, draft)


@pytest.mark.parametrize("config", [
    {"hero": "https://example.com"},
    {"order": ["service:one", "service:one"]},
    {"labels": {"service:one": ""}},
    {"title": ""},
    {"eyebrow": "x" * 61},
    {"description": "x" * 601},
    {"descriptions": {"service:one": "x" * 241}},
    {"forms": {"unknown:one": [{"key": "x", "label": "X", "type": "text"}]}},
    {"forms": {"service:one": [{"key": "x", "label": "X", "type": "file"}, {"key": "x", "label": "Y", "type": "text"}]}},
    {"forms": {"service:one": [{"key": "x", "label": "X", "type": "select"}]}},
    {"visual": "video"},
    {"motion_cover_url": "https://example.com/animated.mp4"},
    {"motion_cover_url": "javascript:alert(1)", "motion_cover_source_url": "/cover.png"},
    {"motion_cover_url": 42, "motion_cover_source_url": "/cover.png"},
    {"layout": "dashboard"},
    {"targets": {"service:one": "../other-workspace"}},
    {"components": "arbitrary UI"},
])
def test_app_view_rejects_untrusted_layout_or_action_references(config):
    with pytest.raises(ValueError):
        normalize_app_view_config(config)


@pytest.mark.parametrize("key", ["service:customer_support", "workflow:binding-1", "automation:job"])
@pytest.mark.parametrize("target,kind", [("agent:agent", "chat"), ("agent:default", "chat"),
                                        ("workflow:binding-1", "workflow"), ("automation:job", "automation")])
def test_every_app_entry_can_choose_each_runner_and_uses_its_input_contract(key, target, kind):
    fields = [{"key": "brief", "label": "Brief", "type": "text"}]
    job = SimpleNamespace(job_id="job", name="Review", enabled=False, last_status="succeeded",
                          payload_message="Review the release", schedule_kind="cron", cron_expr="0 9 * * *")
    config = {"targets": {key: target}, "forms": {key: fields}, "labels": {key: "Custom entry"}}
    view = project_workspace_app_view(_workspace(config), [_flow()], [job], [{"id": "agent", "name": "Writer"}])
    action = next(row for row in view["actions"] if row["key"] == key)
    assert action["kind"] == kind
    assert action["title"] == "Custom entry"
    assert action["available"] is True
    if kind == "chat":
        assert action["inputs"][0]["key"] == "brief"
        assert action.get("agent_id") == ("agent" if target == "agent:agent" else None)
        assert not action.get("binding_id") and not action.get("job_id")
    elif kind == "workflow":
        assert action["binding_id"] == "binding-1"
        assert action["inputs"][0]["key"] == "request"
        assert not action.get("agent_id") and not action.get("job_id")
    else:
        assert action["job_id"] == "job"
        assert action["enabled"] is False
        assert action["instructions"] == "Review the release"
        assert action["cron_expr"] == "0 9 * * *"
        assert action["inputs"] == []
        assert not action.get("agent_id") and not action.get("binding_id")


@pytest.mark.parametrize("target,kind", [("agent:missing", "chat"), ("workflow:missing", "workflow"),
                                        ("automation:missing", "automation")])
def test_missing_typed_targets_never_fall_back_to_the_original_automation(target, kind):
    job = SimpleNamespace(job_id="job", name="Review", enabled=True, last_status=None)
    view = project_workspace_app_view(_workspace({"targets": {"automation:job": target}}), [_flow()], [job])
    action = next(row for row in view["actions"] if row["key"] == "automation:job")
    assert action["kind"] == kind
    assert action["available"] is False
    assert not action.get("agent_id") and not action.get("binding_id") and not action.get("job_id")
def test_list_workspace_chat_entrypoints_accepts_unpublished_flag():
    import inspect
    from packages.core.services.workspace_workflow_router import list_workspace_chat_entrypoints

    params = inspect.signature(list_workspace_chat_entrypoints).parameters
    assert params["include_unpublished"].default is False


async def _seed_chat_entrypoint(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    title: str,
    binding_status: str = "active",
    enabled: bool = True,
    definition_status: str = "active",
    is_active: bool = True,
):
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=title,
        steps=[],
        is_active=is_active,
        status=definition_status,
    )
    db.add(workflow)
    await db.flush()
    binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=workflow.id,
        workspace_id=workspace_id,
        name=title,
        trigger_type="manual",
        enabled=enabled,
        status=binding_status,
        config={"chat_entrypoint": {"enabled": True, "title": title}},
    )
    db.add(binding)
    await db.flush()
    return binding


@pytest.mark.asyncio
async def test_unpublished_chat_entrypoints_are_opt_in_and_never_launchable(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_workflow_router import (
        get_workspace_chat_entrypoint,
        list_workspace_chat_entrypoints,
    )

    entity_id = generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="App View Flows", status="active")
    db_session.add(workspace)
    await db_session.flush()

    published = await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id, title="Published flow",
    )
    paused = await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Paused flow", binding_status="paused",
    )
    draft = await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Draft flow", binding_status="draft",
    )
    await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Retired flow", binding_status="inactive",
    )
    await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Disabled flow", enabled=False,
    )
    await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Inactive definition", is_active=False, definition_status="inactive",
    )
    await db_session.commit()

    published_ids = {
        item.binding_id
        for item in await list_workspace_chat_entrypoints(
            db_session, entity_id=entity_id, workspace_id=workspace.id,
        )
    }
    unpublished_ids = {
        item.binding_id
        for item in await list_workspace_chat_entrypoints(
            db_session, entity_id=entity_id, workspace_id=workspace.id,
            include_unpublished=True,
        )
    }

    assert published_ids == {published.id}
    assert unpublished_ids == {published.id, paused.id, draft.id}
    assert await get_workspace_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id, binding_id=paused.id,
    ) is None
    assert await get_workspace_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id, binding_id=published.id,
    ) is not None


@pytest.mark.asyncio
async def test_workspace_app_view_endpoint_lists_without_500(client):
    registration = (await client.post("/api/v1/auth/register", json={
        "username": "app_view_unpublished_listing",
        "email": "app_view_unpublished_listing@test.com",
        "password": "pass123",
        "entity_name": "App View Unpublished Listing",
    })).json()
    headers = {"Authorization": f"Bearer {registration['access_token']}"}
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={
        "name": "App View Workspace",
    })).json()
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Draft app action",
        "steps": [
            {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["end"]},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    created = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"],
        "workspace_id": workspace["id"],
        "trigger_type": "manual",
        "config": {"chat_entrypoint": {"enabled": True, "title": "Draft app action"}},
    })
    assert created.status_code == 201, created.text
    updated = await client.put(
        f"/api/v1/workflows/bindings/{created.json()['id']}",
        headers=headers,
        json={"status": "paused"},
    )
    assert updated.status_code == 200, updated.text
    binding = updated.json()

    response = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/app-view",
        headers=headers,
    )
    listed = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    action_keys = {action["key"] for action in response.json()["actions"]}
    assert f"workflow:{binding['id']}" in action_keys
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
