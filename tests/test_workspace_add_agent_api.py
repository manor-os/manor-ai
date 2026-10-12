"""Adding an Agent's service and deployment uses one Workspace operation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
import pytest

from apps.api.routers import workspaces as router


@pytest.fixture
def mapping_api(monkeypatch):
    workspace = SimpleNamespace(operating_model={"services": [{"key": "content", "name": "Content"}]})
    user = SimpleNamespace(id="operator", entity_id="entity")
    existing = Mock(return_value=None)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=existing)),
        commit=AsyncMock(),
    )
    authorize = AsyncMock(return_value=workspace)
    apply = AsyncMock(return_value={"status": "applied", "workspace_revision": 2})
    monkeypatch.setattr(router, "_require_workspace_manage", authorize)
    monkeypatch.setattr(router, "_apply_workspace_operation_patches", apply)
    return SimpleNamespace(workspace=workspace, user=user, db=db, authorize=authorize, apply=apply, existing=existing)


def request(**overrides):
    return router.AgentMappingRequest(**{
        "service_key": "video", "agent_id": "video-agent", "custom_prompt": "Create marketing videos.",
        "new_service": {"key": "video", "name": "Marketing Video"}, **overrides,
    })


@pytest.mark.asyncio
async def test_new_service_and_agent_mapping_share_one_authorized_apply(mapping_api):
    api = mapping_api
    result = await router.map_agent("workspace", request(), api.user, api.db)

    api.authorize.assert_awaited_once_with(api.db, "workspace", api.user)
    api.apply.assert_awaited_once_with(
        api.db, workspace_id="workspace", entity_id="entity", user_id="operator",
        source_event_id="api_agent_map", patches=[
            {"op": "service_role.upsert", "payload": {"key": "video", "name": "Marketing Video", "description": "", "config": {}}},
            {"op": "agent_mapping.upsert", "payload": {"mapping": {
                "service_key": "video", "agent_id": "video-agent", "custom_prompt": "Create marketing videos.",
            }}},
        ],
    )
    api.db.commit.assert_awaited_once()
    assert result == {"status": "applied", "workspace_revision": 2}


@pytest.mark.asyncio
async def test_existing_service_mapping_keeps_the_existing_api_contract(mapping_api):
    api = mapping_api
    await router.map_agent("workspace", request(service_key="content", new_service=None), api.user, api.db)
    patches = api.apply.await_args.kwargs["patches"]
    assert len(patches) == 1
    assert patches[0]["op"] == "agent_mapping.upsert"
    assert patches[0]["payload"]["mapping"]["service_key"] == "content"
    api.db.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("services,subscription", [
    ([{"key": "video"}], None),
    ([{"service_key": "video"}], None),
    ([], "existing-video-deployment"),
])
async def test_new_service_never_replaces_an_existing_service_or_orphan_mapping(mapping_api, services, subscription):
    api = mapping_api
    api.workspace.operating_model = {"services": services}
    api.existing.return_value = subscription
    with pytest.raises(HTTPException) as error:
        await router.map_agent("workspace", request(), api.user, api.db)
    assert error.value.status_code == 409
    api.apply.assert_not_awaited()
    api.db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_service", [
    {"key": "other", "name": "Video"}, {"key": "video", "name": " "},
    {"key": " video ", "name": "Video"},
])
async def test_invalid_new_service_fails_before_any_operation(mapping_api, new_service):
    api = mapping_api
    with pytest.raises(HTTPException) as error:
        await router.map_agent("workspace", request(new_service=new_service), api.user, api.db)
    assert error.value.status_code == 400
    api.apply.assert_not_awaited()
    api.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mapping_failure_cannot_commit_a_service_without_its_agent(mapping_api):
    api = mapping_api
    api.apply.side_effect = HTTPException(400, "Agent is unavailable")
    with pytest.raises(HTTPException):
        await router.map_agent("workspace", request(), api.user, api.db)
    api.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_actor_cannot_create_a_service_or_mapping(mapping_api):
    api = mapping_api
    api.authorize.side_effect = HTTPException(403, "Workspace manager required")
    with pytest.raises(HTTPException):
        await router.map_agent("workspace", request(), api.user, api.db)
    api.db.execute.assert_not_awaited()
    api.apply.assert_not_awaited()
    api.db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_api_adds_fourth_deployment_preserves_three_and_rolls_back_invalid_agent(client, db_session):
    from sqlalchemy import select
    from auth_helpers import register_user_and_get_token
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    registered = await register_user_and_get_token(client, {
        "username": "addagent", "email": "addagent@example.test", "password": "pass123",
        "entity_name": "Agent Mapping Test",
    })
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    created = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Mapping Test"})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    workspace = await db_session.get(Workspace, workspace_id)
    workspace.status = "paused"
    services = [{"key": key, "name": key.title()} for key in ("research", "leads", "content")]
    workspace.operating_model = {"services": services}
    agents = [Agent(id=generate_ulid(), entity_id=me["entity_id"], name=f"Agent {index}", status="active") for index in range(4)]
    db_session.add_all(agents)
    await db_session.flush()
    original = {}
    for service, agent in zip(services, agents[:3]):
        original[service["key"]] = agent.id
        db_session.add(AgentSubscription(
            id=generate_ulid(), entity_id=me["entity_id"], workspace_id=workspace_id,
            service_key=service["key"], agent_id=agent.id, status="active",
        ))
    await db_session.commit()
    endpoint = f"/api/v1/workspaces/{workspace_id}/agents"
    payload = {"service_key": "video", "agent_id": agents[3].id, "new_service": {"key": "video", "name": "Marketing Video"}}
    added = await client.post(endpoint, headers=headers, json=payload)
    assert added.status_code == 200, added.text
    after = (await client.get(endpoint, headers=headers)).json()
    assert {row["service_key"]: row["agent_id"] for row in after} == {**original, "video": agents[3].id}
    revision = added.json()["workspace_revision"]

    duplicate = await client.post(endpoint, headers=headers, json=payload)
    assert duplicate.status_code == 409, duplicate.text
    reused = await client.post(endpoint, headers=headers, json={
        "service_key": "second_video", "agent_id": agents[3].id,
        "new_service": {"key": "second_video", "name": "Another Video"},
    })
    assert reused.status_code == 409, reused.text
    invalid = await client.post(endpoint, headers=headers, json={
        "service_key": "missing_agent", "agent_id": generate_ulid(),
        "new_service": {"key": "missing_agent", "name": "Missing Agent"},
    })
    assert invalid.status_code == 400, invalid.text
    db_session.expire_all()
    refreshed = await db_session.get(Workspace, workspace_id)
    assert {service["key"] for service in refreshed.operating_model["services"]} == {"research", "leads", "content", "video"}
    assert refreshed.operation_revision == revision
    persisted = (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.status == "active",
    ))).scalars().all()
    assert {row.service_key: row.agent_id for row in persisted} == {**original, "video": payload["agent_id"]}
