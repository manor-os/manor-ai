"""Invalid configuration remains an editable draft or a client error, never a 500."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio

from apps.api.routers import workspaces as router
from packages.core.ai.runtime.workspace_operation_actions import _operation_review_or_needs_fix
from packages.core.ai.runtime.tool_error_contract import model_tool_error_result
from packages.core.services import workspace_operation_service as operations


def test_invalid_draft_returns_only_factual_diagnostics_and_no_review_card():
    errors = [{"path": "capability_bindings[0].owner_scope", "message": "unsupported owner scope"}]
    draft = {"id": "draft-1", "validation": {"valid": False, "errors": errors}, "current_state": {"large": "state"}}
    original = deepcopy(draft)
    result = _operation_review_or_needs_fix(draft, prompt="Apply?", content={})
    assert result["status"] == "needs_fix"
    assert result["error"] == "operation_validation_failed"
    assert result["details"] == {"draft_id": "draft-1", "validation_errors": errors}
    assert "__hitl__" not in result
    assert "draft" not in result
    assert "patch_draft" not in json.dumps(result)
    assert draft == original

    projected = json.loads(model_tool_error_result("workspace_operation", result, max_chars=4000))
    assert projected["contract"] == "tool_error"
    assert projected["status"] == "error"
    assert projected["error"] == "operation_validation_failed"
    assert projected["details"]["validation_errors"] == errors
    assert projected["details"]["source_status"] == "needs_fix"
    assert "__hitl__" not in projected


def test_valid_draft_still_uses_the_existing_approval_boundary():
    result = _operation_review_or_needs_fix(
        {"id": "draft-1", "validation": {"valid": True, "errors": []}}, prompt="Apply?", content={},
    )
    assert result["__hitl__"] is True
    assert result.get("contract") != "tool_error"


@pytest_asyncio.fixture
async def draft_api(monkeypatch):
    workspace = SimpleNamespace(id="workspace", entity_id="entity", operation_revision=0)
    state = {
        "operating_model": {}, "services": [{"key": "content", "name": "Content"}],
        "agent_mappings": [], "capability_bindings": [], "skill_bindings": [],
        "goals": [], "rules": [], "governance_policy": {}, "budget_policy": {},
        "heartbeat_policy": {}, "knowledge_policy": {}, "channel_config": {},
        "immediate_tasks": [],
    }
    drafts = {}
    db = SimpleNamespace(
        add=Mock(side_effect=lambda draft: drafts.setdefault(draft.id, draft)),
        flush=AsyncMock(), commit=AsyncMock(),
        execute=AsyncMock(side_effect=AssertionError("this draft has no database references")),
    )
    monkeypatch.setattr(operations, "_load_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(operations, "_snapshot_workspace_state", AsyncMock(side_effect=lambda *_a, **_k: deepcopy(state)))
    monkeypatch.setattr(operations, "get_operation_draft", AsyncMock(side_effect=lambda _db, draft_id, *_a, **_k: drafts.get(draft_id)))
    monkeypatch.setattr(router, "_require_workspace_manage", AsyncMock(return_value=workspace))
    app = FastAPI()
    app.include_router(router.router)
    app.dependency_overrides[router.get_current_user] = lambda: SimpleNamespace(id="user", entity_id="entity")
    app.dependency_overrides[router.get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, db=db, drafts=drafts, endpoint="/api/v1/workspaces/workspace/operation/drafts")


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {},
    {"op": "rule.add", "payload": "invalid"},
    {"op": "operating_model.update", "payload": {"services": {"video": {"agent": "Video"}}}},
    {"op": "service_role.upsert", "payload": {"service": {"key": "video", "skills": ["video"]}}},
])
async def test_draft_create_and_patch_shape_errors_are_http_400(draft_api, patch):
    api = draft_api
    rejected = await api.client.post(api.endpoint, json={"patches": [patch]})
    assert rejected.status_code == 400, rejected.text
    assert not api.drafts
    api.db.commit.assert_not_awaited()

    created = await api.client.post(api.endpoint, json={"patches": [
        {"op": "service_role.upsert", "payload": {"key": "video", "name": "Video"}},
    ]})
    assert created.status_code == 200, created.text
    draft_id = created.json()["id"]
    original = deepcopy(api.drafts[draft_id].current_state)
    api.db.commit.reset_mock()
    rejected = await api.client.patch(f"{api.endpoint}/{draft_id}", json={"patches": [patch]})
    assert rejected.status_code == 400, rejected.text
    assert api.drafts[draft_id].current_state == original
    api.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["create", "patch"])
async def test_invalid_domain_create_or_patch_keeps_real_preview_editable(draft_api, entrypoint):
    api = draft_api
    invalid = {"op": "governance.update", "payload": {"max_risk_level": []}}
    if entrypoint == "create":
        response = await api.client.post(api.endpoint, json={"patches": [invalid]})
    else:
        created = await api.client.post(api.endpoint, json={"patches": [
            {"op": "service_role.upsert", "payload": {"key": "video", "name": "Video"}},
        ]})
        assert created.status_code == 200, created.text
        response = await api.client.patch(f"{api.endpoint}/{created.json()['id']}", json={"patches": [invalid]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["validation"]["valid"] is False
    assert any(error["path"] == "governance_policy" for error in payload["validation"]["errors"])
    assert payload["diff"]["changes"]["governance_policy"]["after"]["max_risk_level"] == []
    review = _operation_review_or_needs_fix(payload, prompt="Apply?", content={})
    assert review["status"] == "needs_fix"
    assert "__hitl__" not in review

    # Legacy stored validation may be stale; the diff endpoint must still
    # show the requested values without invoking the typed policy resolver.
    draft_id = payload["id"]
    api.drafts[draft_id].validation = {"valid": True, "errors": []}
    preview = await api.client.get(f"{api.endpoint}/{draft_id}/diff")
    assert preview.status_code == 200, preview.text
    assert preview.json()["changes"]["governance_policy"]["after"]["max_risk_level"] == []

    corrected = await api.client.patch(f"{api.endpoint}/{draft_id}", json={"patches": [
        {"op": "governance.update", "payload": {"max_risk_level": "low"}},
    ]})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["validation"]["valid"] is True
    assert corrected.json()["diff"]["changes"]["governance_policy"]["after"]["max_risk_level"] == "low"


@pytest.mark.asyncio
async def test_internal_writer_maps_real_create_shape_error_to_400(draft_api):
    with pytest.raises(HTTPException) as caught:
        await router._apply_workspace_operation_patches(
            draft_api.db, workspace_id="workspace", entity_id="entity", user_id="user",
            source_event_id="api_test", patches=[
                {"op": "operating_model.update", "payload": {"services": {"video": {}}}},
            ],
        )
    assert caught.value.status_code == 400
    assert "array of objects" in caught.value.detail
    assert not draft_api.drafts


@pytest.mark.asyncio
async def test_marketplace_install_identity_conflict_is_400_and_rolls_back(client, db_session):
    from sqlalchemy import select
    from auth_helpers import register_user_and_get_token
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    registered = await register_user_and_get_token(client, {
        "username": "templateconflict", "email": "templateconflict@example.test", "password": "pass123",
        "entity_name": "Template Conflict Test",
    })
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    created = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Template Conflict Test"})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    workspace = await db_session.get(Workspace, workspace_id)
    workspace.status = "paused"
    template_id = f"tmpl_{generate_ulid()[5:]}"
    template = Agent(
        id=template_id, name="Video Specialist", slug=f"video-{generate_ulid()}",
        system_prompt="Create marketing videos.", config={}, is_template=True,
        is_public=True, source="marketplace", status="active",
    )
    db_session.add(template)
    await db_session.commit()
    endpoint = f"/api/v1/workspaces/{workspace_id}"
    first = await client.post(f"{endpoint}/agents", headers=headers, json={
        "service_key": "video", "agent_id": template_id,
        "new_service": {"key": "video", "name": "Video"},
    })
    assert first.status_code == 200, first.text
    revision = first.json()["workspace_revision"]
    repeated = await client.post(f"{endpoint}/agents", headers=headers, json={
        "service_key": "second_video", "agent_id": template_id,
        "new_service": {"key": "second_video", "name": "Second Video"},
    })
    assert repeated.status_code == 400, repeated.text
    assert "already mapped" in repeated.json()["detail"]

    # The explicit draft/apply route has the same materialization boundary.
    draft = await client.post(f"{endpoint}/operation/drafts", headers=headers, json={"patches": [
        {"op": "service_role.upsert", "payload": {"key": "second_video", "name": "Second Video"}},
        {"op": "agent_mapping.upsert", "payload": {"service_key": "second_video", "agent_id": template_id}},
    ]})
    assert draft.status_code == 200, draft.text
    applied = await client.post(f"{endpoint}/operation/drafts/{draft.json()['id']}/apply", headers=headers, json={})
    assert applied.status_code == 400, applied.text
    assert "already mapped" in applied.json()["detail"]
    db_session.expire_all()
    refreshed = await db_session.get(Workspace, workspace_id)
    assert refreshed.operation_revision == revision
    assert {row["key"] for row in refreshed.operating_model["services"]} == {"video"}
    subscriptions = (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.status == "active",
    ))).scalars().all()
    assert len(subscriptions) == 1
    assert subscriptions[0].service_key == "video"
    assert subscriptions[0].agent_id != template_id
