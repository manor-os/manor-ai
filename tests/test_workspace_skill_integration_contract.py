"""Published operation schemas and guides must lead to durable Workspace edits."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
import pytest
from sqlalchemy import select

from packages.core.ai.runtime import workspace_operation_skills as guides
from packages.core.ai.runtime.manor_actions import runtime_manor_update_workspace
from packages.core.ai.runtime.tool_input_validation import validate_runtime_tool_arguments
from packages.core.ai.runtime.tool_registry import runtime_tool_schema
from packages.core.ai.tools.manor_tool import _manor_handler
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace, WorkspaceOperationDraft
from packages.core.services.hitl_requests import hitl_requests_from_data, workspace_operation_pending_action_from_data
from packages.core.services import workspace_operation_service as operations


def _call(surface, params):
    if surface == "workspace_operation":
        return params
    wrapped = {"action": "operation", "params": params}
    return {"action": "workspace", "params": wrapped} if surface == "manor" else wrapped


def test_guide_projects_real_operation_constraints_and_validated_manor_actions():
    config = guides.workspace_operation_guide_configs()["workspace-configuration"]
    contract = guides.workspace_operation_tool_contract(config)
    live = runtime_tool_schema("workspace_operation")["function"]["parameters"]
    assert contract["operation_parameters"] == live
    text, _ = guides.load_workspace_operation_guide("workspace-configuration")
    catalog = json.loads(text.split("execution):\n", 1)[1])
    assert catalog["operation_parameters"] == guides._operation_parameter_constraints(contract)
    assert set(catalog["manor_actions"]) == {"get_workspace", "update_workspace", "list_integrations", "list_ready_integrations"}
    for name, manifest in catalog["manor_actions"].items():
        assert manifest["invoke_with"] == {"tool": "manor", "arguments": {"action": name}}
    for op in ("integration.remove", "integration.enable", "integration_scope.update"):
        schema = catalog["patch_ops"][op]["payload_schema"]
        assert schema == operations.operation_patch_payload_schema(op)
        Draft202012Validator.check_schema(schema)
    # Removing schema descriptions must preserve a field *named* description.
    assert guides._schema_constraints({"description": "annotation", "properties": {"description": {"type": "string"}}}) == {
        "properties": {"description": {"type": "string"}},
    }


def test_guide_cannot_invent_a_manor_action():
    with pytest.raises(ValueError, match="Unknown Manor actions"):
        guides.workspace_operation_tool_contract({"workspace_operations": {"manor_actions": ["connect_integration"]}})


@pytest.mark.parametrize("message", ["Change workspace integrations", "调整workspaces，包括改workspace integration", "修改工作空间配置并恢复连接"])
def test_integration_requests_load_the_configuration_guide(message):
    assert guides.select_workspace_operation_guides(message, guides.workspace_operation_guide_configs()) == ("workspace-configuration",)


def test_operation_schema_change_invalidates_dependent_guides(monkeypatch):
    from packages.core.ai.runtime import tool_registry

    before = {slug: guides.load_workspace_operation_guide(slug)[1] for slug in ("workspace-app", "workspace-configuration", "workspace-workflows")}
    original = tool_registry.runtime_tool_schema

    def schema(name):
        value = original(name)
        if name == "workspace_operation":
            value = deepcopy(value)
            value["function"]["parameters"]["properties"]["draft_id"]["minLength"] = 1
        return value

    monkeypatch.setattr(tool_registry, "runtime_tool_schema", schema)
    for slug in ("workspace-app", "workspace-configuration"):
        assert guides.load_workspace_operation_guide(slug)[1] != before[slug]
    assert guides.load_workspace_operation_guide("workspace-workflows")[1] == before["workspace-workflows"]


@pytest.mark.parametrize("surface", ["manor", "workspace_agent", "workspace_operation"])
@pytest.mark.parametrize("op,payload", [
    ("integration.remove", {"provider": "slack"}),
    ("integration.remove", {"provider": "slack", "scope": " workspace "}),
    ("integration.remove", {"provider": "Twitter", "integration_key": "x"}),
    ("integration.enable", {"integration_key": "slack"}),
    ("integration_scope.update", {"disabled_providers": ["slack"]}),
    ("integration.update", {"disabled_providers": []}),
])
def test_documented_integration_shapes_validate_and_dispatch(surface, op, payload):
    params = {"action": "create_draft", "patches": [{"op": op, "payload": payload}]}
    assert validate_runtime_tool_arguments(arguments=_call(surface, params), tool_schema=runtime_tool_schema(surface)) is None
    state = operations._sync_operating_model_from_state({
        "operating_model": {}, "integration_scope": {"disabled_providers": ["slack"]},
    })
    changed = operations._apply_single_patch(state, params["patches"][0])
    assert "integration_scope" in changed


@pytest.mark.parametrize("surface", ["manor", "workspace_agent", "workspace_operation"])
@pytest.mark.parametrize("op,payload", [
    ("integration.remove", {}),
    ("integration.remove", {"provider": " "}),
    ("integration.remove", {"provider": ["slack"]}),
    ("integration.enable", {"provider": "slack", "service_key": "content"}),
    ("integration_scope.update", {"disabled_providers": "slack"}),
    ("integration.update", {"disabled_providers": [None]}),
    ("integration_scope.update", {"credentials": {"token": "synthetic"}}),
])
def test_invalid_integration_shapes_fail_before_authorization_or_draft(surface, op, payload):
    params = {"action": "create_draft", "patches": [{"op": op, "payload": payload}]}
    failure = validate_runtime_tool_arguments(arguments=_call(surface, params), tool_schema=runtime_tool_schema(surface))
    assert failure and failure.code == "tool_input_validation_failed"
    assert ".payload" in failure.path
    assert "synthetic" not in failure.message
    state = {"operating_model": {}, "integration_scope": {"disabled_providers": ["slack"]}}
    before = deepcopy(state)
    with pytest.raises(ValueError):
        operations._apply_single_patch(state, params["patches"][0])
    assert state == before


@pytest.mark.parametrize("field", sorted({
    "operating_model", "monthly_budget_usd",
    "auto_pause_on_budget", "heartbeat_enabled", "heartbeat_cadence",
}))
@pytest.mark.asyncio
async def test_metadata_editor_cannot_apply_runtime_configuration_without_review(monkeypatch, field):
    update = AsyncMock(side_effect=AssertionError("reviewed configuration must not reach the metadata writer"))
    monkeypatch.setattr("packages.core.services.entity_service.update_workspace", update)
    result = json.loads(await runtime_manor_update_workspace(
        object(), entity_id="entity", params={"workspace_id": "workspace", "name": "Unapplied", field: {}},
    ))
    assert result["fields"] == [field]
    assert "operation draft" in result["error"]
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_editor_still_updates_workspace_name(monkeypatch):
    update = AsyncMock(return_value=SimpleNamespace(id="workspace", name="New name"))
    monkeypatch.setattr("packages.core.services.entity_service.update_workspace", update)
    db = SimpleNamespace(commit=AsyncMock())
    result = json.loads(await runtime_manor_update_workspace(
        db, entity_id="entity", runtime_workspace_id="workspace",
        params={"workspace_id": "workspace", "name": "New name", "description": "Updated"},
    ))
    assert result["updated"] is True
    update.assert_awaited_once_with(db, "workspace", "entity", name="New name", description="Updated")


@pytest.mark.parametrize("runtime_workspace_id", [None, "other-workspace"])
@pytest.mark.asyncio
async def test_metadata_editor_does_not_infer_scope_from_business_target(monkeypatch, runtime_workspace_id):
    update = AsyncMock(side_effect=AssertionError("an unscoped caller must not write metadata"))
    monkeypatch.setattr("packages.core.services.entity_service.update_workspace", update)
    db = SimpleNamespace(commit=AsyncMock())
    result = json.loads(await runtime_manor_update_workspace(
        db, entity_id="entity", runtime_workspace_id=runtime_workspace_id,
        params={"workspace_id": "workspace", "name": "Unapplied"},
    ))
    assert result == {"error": "Workspace not found or manage permission is required"}
    update.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_gateway_add_remove_restore_and_revoked_connection_are_durable(client, db_session):
    username = f"skill_integration_{generate_ulid().lower()}"
    registered = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@example.test", "password": "pass123", "entity_name": "Skill integration test",
    })
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    user = (await db_session.execute(select(User).where(User.email == f"{username}@example.test"))).scalar_one()
    entity_id, user_id = user.entity_id, user.id
    created = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Integration contract"})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    workspace = await db_session.get(Workspace, workspace_id)
    workspace.status = "paused"
    connection = await client.post("/api/v1/integrations", headers=headers, json={
        "provider": "linkedin", "credentials": {"access_token": "synthetic-local-test"},
    })
    assert connection.status_code in {200, 201}, connection.text
    integration_id = connection.json()["id"]
    conversation_id = generate_ulid()
    db_session.add(Conversation(id=conversation_id, entity_id=entity_id, user_id=user_id,
                               workspace_id=workspace_id, channel="workspace", scope="workspace_main"))
    await db_session.commit()

    async def gateway(params):
        args = {"action": "workspace", "params": {"action": "operation", "params": params}}
        assert validate_runtime_tool_arguments(arguments=args, tool_schema=runtime_tool_schema("manor")) is None
        return json.loads(await _manor_handler(entity_id=entity_id, workspace_id=workspace_id,
            conversation_id=conversation_id, _user_id_from_context=user_id, **args))

    async def approve(review):
        message_id = generate_ulid()
        db_session.add(Message(id=message_id, conversation_id=conversation_id, role="assistant", content="Review changes",
            author_kind="agent", message_kind="hitl_request", created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            pending_action=workspace_operation_pending_action_from_data(review), meta={"hitl_requests": hitl_requests_from_data(review)}))
        db_session.add(Message(conversation_id=conversation_id, role="user", content="Approve these changes",
            author_kind="user", message_kind="text", meta={"author_user_id": user_id}))
        await db_session.commit()
        return await client.post(f"/api/v1/workspaces/{workspace_id}/chat/messages/{message_id}/resolve",
                                 headers=headers, json={"choice": "approve"})

    patches = [
        {"op": "capability_binding.upsert", "payload": {"binding": {
            "owner_scope": "workspace_agent", "capability_type": "mcp", "integration_key": "linkedin",
        }}},
        {"op": "integration.remove", "payload": {"provider": "linkedin"}},
        {"op": "integration.enable", "payload": {"provider": "linkedin"}},
    ]
    for revision, patch in enumerate(patches, start=1):
        review = await gateway({"action": "create_draft", "patches": [patch]})
        assert review.get("__hitl__") is True, review
        before = await gateway({"action": "get_current"})
        assert before["state"]["operation_revision"] == revision - 1
        applied = await approve(review)
        assert applied.status_code == 200, applied.text
        current = await gateway({"action": "get_current"})
        assert current["state"]["operation_revision"] == revision
        assert current["state"]["capability_bindings"][0]["integration_key"] == "linkedin"
        assert current["state"]["integration_scope"].get("disabled_providers", []) == (["linkedin"] if revision == 2 else [])
        db_session.expire_all()
        saved = await db_session.get(Workspace, workspace_id)
        assert saved.operation_revision == revision
        assert (await db_session.get(Integration, integration_id)).status == "active"

    # A new binding was reviewed while connected, then the actor revoked the
    # account. The exact durable review must fail rather than apply stale grants.
    review = await gateway({"action": "create_draft", "patches": [{
        "op": "capability_binding.upsert", "payload": {"binding": {
            "binding_key": "narrowed_linkedin", "owner_scope": "workspace_agent", "capability_type": "mcp",
            "integration_key": "linkedin", "allowed_tools": ["mcp__linkedin__get_profile"],
        }},
    }]})
    assert review.get("__hitl__") is True, review
    disconnected = await client.delete(f"/api/v1/integrations/{integration_id}", headers=headers)
    assert disconnected.status_code in {200, 204}, disconnected.text
    rejected = await approve(review)
    assert rejected.status_code in {400, 409}, rejected.text
    db_session.expire_all()
    assert (await db_session.get(Workspace, workspace_id)).operation_revision == 3
    draft_id = review["operation"]["draft_id"]
    assert (await db_session.get(WorkspaceOperationDraft, draft_id)).status == "open"


@pytest.mark.parametrize("payload,valid", [
    ({"workflow_id": "saved-flow", "primary": True}, True),
    ({"workflow_id": "saved-flow", "action_key": "service:content", "service_key": None}, True),
    ({"workflow_id": "saved-flow", "service_key": "content"}, True),
    ({"workflow_id": "saved-flow", "binding_key": "invented"}, False),
    ({"workflow_id": "saved-flow", "service_key": "content", "action_key": "service:content"}, False),
    ({"workflow_id": "saved-flow", "primary": "true"}, False),
    ({"workflow_id": "saved-flow", "action_key": "invalid"}, False),
])
def test_app_attachment_schema_matches_handler_fields_and_target_exclusivity(payload, valid):
    params = {"action": "create_draft", "patches": [{"op": "workflow_binding.attach", "payload": payload}]}
    failure = validate_runtime_tool_arguments(arguments=_call("manor", params), tool_schema=runtime_tool_schema("manor"))
    assert (failure is None) is valid
    state = {"workflow_attachments": []}
    if valid:
        assert operations._apply_single_patch(state, params["patches"][0])["workflow_attachments"][0]["workflow_id"] == "saved-flow"
    else:
        with pytest.raises(ValueError):
            operations._apply_single_patch(state, params["patches"][0])
    guide, _ = guides.load_workspace_operation_guide("workspace-app")
    assert "`binding_key`" not in guide
